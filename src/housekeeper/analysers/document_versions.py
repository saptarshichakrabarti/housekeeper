import re
from collections import defaultdict
from difflib import SequenceMatcher

from ..jobs import check_cancelled, checkpoint
from ..relationships import replace_relationship_group, upsert_relationship
from .scope import AnalyserScope, resolve_scope


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
    database, config, scope: AnalyserScope | None = None, job_id: int | None = None
):
    entry_sql, params = resolve_scope(database, scope).entry_id_sql()
    rows = database.fetch_all(
        f"""SELECT e.id,e.name,l.content_object_id FROM filesystem_entries e
           JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND e.suffix IN ('.txt','.md','.doc','.docx','.pdf','.rtf')
           AND e.id IN ({entry_sql}) ORDER BY e.id""",
        params,
    )
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
        checkpoint(database, job_id, processed_count=index, state={"last_family_key": key})
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
