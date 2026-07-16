"""Best-effort, read-only source identity discovery across mount-path changes."""

import json
import platform
import plistlib
import subprocess
from pathlib import Path


def discover_source_identity(root: Path) -> tuple[str | None, str | None, dict[str, object]]:
    stat = root.stat()
    metadata: dict[str, object] = {"device_id": stat.st_dev, "root_inode": getattr(stat, "st_ino", None), "platform": platform.system()}
    filesystem_uuid: str | None = None
    label: str | None = None
    if platform.system() == "Darwin":
        try:
            macos_result = subprocess.run(["diskutil", "info", "-plist", str(root)], text=False, capture_output=True, timeout=3, check=False)
            if macos_result.returncode == 0:
                info = plistlib.loads(macos_result.stdout)
                raw_uuid = info.get("VolumeUUID") or info.get("APFSVolumeUUID")
                raw_label = info.get("VolumeName")
                filesystem_uuid = str(raw_uuid) if raw_uuid else None
                label = str(raw_label) if raw_label else None
                metadata["filesystem_type"] = info.get("FilesystemType")
        except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException):
            pass
    elif platform.system() == "Windows":
        # The device/inode fallback remains stable for the current mount.  Avoid shelling out
        # to PowerShell and never write a marker merely to improve identity.
        metadata["drive"] = root.drive
    else:
        try:
            linux_result = subprocess.run(["findmnt", "-no", "UUID,LABEL,FSTYPE", "--target", str(root)], text=True, capture_output=True, timeout=3, check=False)
            if linux_result.returncode == 0 and linux_result.stdout.strip():
                uuid, label_value, filesystem_type = (linux_result.stdout.strip().split(maxsplit=2) + ["", "", ""])[:3]
                filesystem_uuid = uuid or None
                label = label_value or None
                metadata["filesystem_type"] = filesystem_type
        except (OSError, subprocess.TimeoutExpired):
            pass
    return filesystem_uuid, label, metadata


def metadata_json(root: Path) -> str:
    return json.dumps(discover_source_identity(root)[2], sort_keys=True)
