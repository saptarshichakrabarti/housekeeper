from ..database import Database


def get_content_object(database: Database, content_id: int):
    return database.fetch_one("SELECT * FROM content_objects WHERE id=?", (content_id,))


def representatives(database: Database, content_id: int):
    return database.fetch_all(
        "SELECT e.* FROM filesystem_entries e JOIN entry_content_links l ON l.entry_id=e.id WHERE l.content_object_id=? AND l.link_status='VERIFIED' ORDER BY e.read_error IS NOT NULL,e.id",
        (content_id,),
    )
