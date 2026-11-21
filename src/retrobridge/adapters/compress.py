import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

class CompressionError(Exception):
    pass

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
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0:
        raise CompressionError(f"chdman conversion failed: {res.stderr or res.stdout}")

    # Check
    verify_res = subprocess.run([chdman, "verify", "-i", str(output_chd_path)], capture_output=True, text=True)
    if verify_res.returncode != 0:
        if output_chd_path.exists():
            output_chd_path.unlink()
        raise CompressionError(f"chdman verify check failed: {verify_res.stderr or verify_res.stdout}")

    return output_chd_path

def convert_to_rvz(source_path: Path, output_rvz_path: Path) -> Path:
    """Converts a GameCube/Wii image (iso) to RVZ via dolphin-tool."""
    dolphin_tool = shutil.which("dolphin-tool")
    if not dolphin_tool:
        raise CompressionError("dolphin-tool is not installed on the system.")

    output_rvz_path.parent.mkdir(parents=True, exist_ok=True)
    args = [dolphin_tool, "convert", "-i", str(source_path), "-o", str(output_rvz_path), "--format=rvz"]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0:
        raise CompressionError(f"dolphin-tool convert failed: {res.stderr or res.stdout}")

    return output_rvz_path
