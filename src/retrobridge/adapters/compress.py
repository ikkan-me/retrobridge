import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

class CompressionError(Exception):
    pass


# A truncated or malformed disc image can make chdman loop forever instead of
# failing: never let a single conversion block the whole export.
CONVERSION_TIMEOUT_S = 900


def _summarize_tool_output(text: str, limit: int = 240) -> str:
    """Keeps the meaningful lines of a chdman/dolphin-tool output.

    These tools print one progress line per step ("Compressing, 13.0%
    complete..."); the actual cause is on the ERROR/Fatal line. Reporting the
    whole log buries the message and floods the terminal.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "no output"
    important = [
        line for line in lines
        if re.search(r"error|fatal|fail|invalid|cannot|unable|missing|quota|denied", line, re.IGNORECASE)
    ]
    picked = important[-3:] if important else lines[-2:]
    summary = " | ".join(picked)
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"


def _run_conversion(args: list[str], what: str, source: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise CompressionError(
            f"{what} timed out after {CONVERSION_TIMEOUT_S}s on {source.name} "
            "(corrupt or truncated image?)"
        )


def get_staging_dir(custom_path: Optional[Path] = None) -> Path:
    p = custom_path or Path("/tmp/retrobridge/staging")
    p.mkdir(parents=True, exist_ok=True)
    return p

def extract_from_archive(archive_path: Path, dest_dir: Path, target_extensions: Optional[list] = None) -> Path:
    """Extracts the game file from a zip archive into a staging directory."""
    if not zipfile.is_zipfile(archive_path):
        raise CompressionError(f"File is not a valid zip: {archive_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, 'r') as zf:
        members = zf.namelist()
        selected_member = None
        if target_extensions:
            for m in members:
                if any(m.lower().endswith(ext.lower()) for ext in target_extensions):
                    selected_member = m
                    break
        if not selected_member and members:
            # First non-directory file
            for m in members:
                if not m.endswith('/'):
                    selected_member = m
                    break

        if not selected_member:
            raise CompressionError(f"No compatible file found in {archive_path.name}")

        extracted_path = dest_dir / Path(selected_member).name
        with zf.open(selected_member) as src, open(extracted_path, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        return extracted_path


# Members of a single disc image. A `.cue` or `.gdi` is useless without the
# track files it references, so the whole set must be extracted together.
DISC_SET_MEMBERS = (
    ".cue", ".bin", ".img", ".sub", ".ccd", ".gdi",
    ".iso", ".chd", ".m3u", ".pbp",
)

# When a set offers several entry points, prefer the richest container.
DISC_ENTRY_PRIORITY = (".m3u", ".cue", ".gdi", ".chd", ".iso", ".img", ".bin", ".pbp")


def _safe_member_path(root: Path, member: str) -> Optional[Path]:
    """Resolves an archive member under `root`, rejecting path traversal."""
    target = (root / member).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def extract_disc_set(archive_path: Path, dest_dir: Path) -> Path:
    """Extracts a whole disc image (cue + bin tracks, gdi + tracks, iso...).

    Returns the file chdman should convert (the `.cue`/`.gdi`/`.iso`...).
    """
    if not zipfile.is_zipfile(archive_path):
        raise CompressionError(f"File is not a valid zip: {archive_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(archive_path, 'r') as zf:
        for member in zf.namelist():
            if member.endswith('/'):
                continue
            if Path(member).suffix.lower() not in DISC_SET_MEMBERS:
                continue
            target = _safe_member_path(dest_dir, member)
            if target is None:
                continue
            # Keep the archive's relative layout: a `.cue` references its
            # tracks by the same relative names.
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)

    if not extracted:
        raise CompressionError(f"No disc image found in {archive_path.name}")

    def rank(path: Path) -> int:
        suffix = path.suffix.lower()
        return DISC_ENTRY_PRIORITY.index(suffix) if suffix in DISC_ENTRY_PRIORITY else len(DISC_ENTRY_PRIORITY)

    return sorted(extracted, key=rank)[0]


def convert_to_chd(source_path: Path, output_chd_path: Path, is_dvd: bool = False) -> Path:
    """Converts a disc image (cue/bin, iso, gdi) to CHD via chdman in the working directory."""
    chdman = shutil.which("chdman")
    if not chdman:
        raise CompressionError("chdman is not installed on the system.")

    cmd_type = "createdvd" if is_dvd else "createcd"
    args = [chdman, cmd_type, "-i", str(source_path), "-o", str(output_chd_path)]
    if is_dvd:
        args.extend(["-hs", "2048"])

    output_chd_path.parent.mkdir(parents=True, exist_ok=True)
    res = _run_conversion(args, "chdman conversion", source_path)
    if res.returncode != 0:
        raise CompressionError(
            "chdman conversion failed: "
            + _summarize_tool_output(res.stderr + "\n" + res.stdout)
        )

    # Check
    verify_res = _run_conversion(
        [chdman, "verify", "-i", str(output_chd_path)], "chdman verify", source_path
    )
    if verify_res.returncode != 0:
        if output_chd_path.exists():
            output_chd_path.unlink()
        raise CompressionError(
            "chdman verify check failed: "
            + _summarize_tool_output(verify_res.stderr + "\n" + verify_res.stdout)
        )

    return output_chd_path

def convert_to_rvz(source_path: Path, output_rvz_path: Path) -> Path:
    """Converts a GameCube/Wii image (iso) to RVZ via dolphin-tool."""
    dolphin_tool = shutil.which("dolphin-tool")
    if not dolphin_tool:
        raise CompressionError("dolphin-tool is not installed on the system.")

    output_rvz_path.parent.mkdir(parents=True, exist_ok=True)
    args = [dolphin_tool, "convert", "-i", str(source_path), "-o", str(output_rvz_path), "--format=rvz"]
    res = _run_conversion(args, "dolphin-tool conversion", source_path)
    if res.returncode != 0:
        raise CompressionError(
            "dolphin-tool convert failed: "
            + _summarize_tool_output(res.stderr + "\n" + res.stdout)
        )

    return output_rvz_path
