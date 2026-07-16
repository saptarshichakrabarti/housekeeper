UNCHANGED = "UNCHANGED"
METADATA_CHANGED = "METADATA_CHANGED"
CONTENT_POSSIBLY_CHANGED = "CONTENT_POSSIBLY_CHANGED"
MOVED_OR_RENAMED_CANDIDATE = "MOVED_OR_RENAMED_CANDIDATE"
NEW = "NEW"
MISSING = "MISSING"
ERROR = "ERROR"


def classify_stat_change(previous, current) -> str:
    if previous is None:
        return NEW
    if (
        previous["size_bytes"] == current.size_bytes
        and previous["modified_at"] == current.modified_at
        and previous["device_id"] == current.device_id
        and previous["inode_or_file_id"] == current.inode_or_file_id
    ):
        return UNCHANGED
    if (
        previous["size_bytes"] == current.size_bytes
        and previous["modified_at"] != current.modified_at
    ):
        return METADATA_CHANGED
    return CONTENT_POSSIBLY_CHANGED
