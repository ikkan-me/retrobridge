import json
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

TARGET_ID_FILENAME = ".retrobridge-target"

def read_target_metadata(mount_path: Path) -> Optional[Dict[str, Any]]:
    id_file = mount_path / TARGET_ID_FILENAME
    if not id_file.is_file():
        return None
    try:
        content = id_file.read_text(encoding="utf-8").strip()
        # Supports JSON or a raw UUID string
        if content.startswith("{"):
            return json.loads(content)
        return {"uuid": content}
    except Exception:
        return None

def write_target_metadata(mount_path: Path, target_uuid: str, name: str = "", profile: str = "") -> Dict[str, Any]:
    mount_path.mkdir(parents=True, exist_ok=True)
    meta = {
        "uuid": target_uuid or str(uuid.uuid4()),
        "name": name,
        "profile": profile
    }
    id_file = mount_path / TARGET_ID_FILENAME
    id_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta

def ensure_target_uuid(mount_path: Path, configured_uuid: str = "", name: str = "", profile: str = "") -> str:
    meta = read_target_metadata(mount_path)
    if meta and meta.get("uuid"):
        return meta["uuid"]
    new_uuid = configured_uuid or str(uuid.uuid4())
    write_target_metadata(mount_path, new_uuid, name=name, profile=profile)
    return new_uuid
