import re
from difflib import SequenceMatcher
from collections import defaultdict

from ..relationships import replace_relationship_group, upsert_relationship
from .scope import AnalyzerScope, scoped_entry_ids
from ..jobs import check_cancelled, update_job


def extract_version_tokens(filename: str) -> list[str]:
    return re.findall(r"(?:final|draft|revised|copy|backup|v\d+|\(\d+\))", filename.lower())


def normalize_version_filename(filename: str) -> str:
    return re.sub(
        r"(?:[_ -]*(?:final|draft|revised|copy|backup|v\d+|\(\d+\)))+",
        "",
        filename.lower(),
    ).strip(" ._-")


def calculate_filename_similarity(a: str, b: str) -> float:
    return SequenceMatcher(
        None, normalize_version_filename(a), normalize_version_filename(b)
    ).ratio()


def run_document_version_analysis(
    database, config, scope: AnalyzerScope | None = None, job_id: int | None = None
):
    rows = database.fetch_all("""SELECT e.id,e.name,l.content_object_id FROM filesystem_entries e JOIN entry_content_links l ON l.entry_id=e.id
        WHERE e.entry_type='file' AND e.suffix IN ('.txt','.md','.doc','.docx','.pdf','.rtf') ORDER BY e.id""")
    allowed = scoped_entry_ids(database, scope) if scope else None
    if allowed is not None:
        rows = [row for row in rows if int(row["id"]) in allowed]
    buckets = defaultdict(list)
    for row in rows:
        # This funnel is deliberately cheap and deterministic; costly similarity is only
        # evaluated within files sharing a normalized filename stem.
        buckets[normalize_version_filename(row["name"])].append(row)
    for index, (key, members) in enumerate(buckets.items(), start=1):
        if job_id:
            check_cancelled(database, job_id)
        unique_content_ids = sorted({int(member["content_object_id"]) for member in members})
        if len(unique_content_ids) > 1:
            replace_relationship_group(
                database,
                "DOCUMENT_FAMILY",
                key,
                unique_content_ids,
                {"normalized_filename": key, "member_count": len(unique_content_ids)},
                "2",
            )
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                similarity = calculate_filename_similarity(left["name"], right["name"])
                if similarity >= 0.86 and left["content_object_id"] != right["content_object_id"]:
                    upsert_relationship(
                        database,
                        "CONTENT_OBJECT",
                        left["content_object_id"],
                        "CONTENT_OBJECT",
                        right["content_object_id"],
                        "LIKELY_VERSION_OF",
                        similarity,
                        {
                            "filename_similarity": similarity,
                            "left": left["name"],
                            "right": right["name"],
                        },
                        "1",
                    )
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=index,
                checkpoint={"last_family_key": key},
            )
