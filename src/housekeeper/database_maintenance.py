from pathlib import Path
from .database import Database


def backup(database: Database, output: Path) -> Path:
    return database.backup(output)


def integrity_check(database: Database) -> str:
    return database.integrity_check()


def optimize(database: Database) -> None:
    database.connect().execute("PRAGMA optimize")
    database.connect().commit()


def checkpoint(database: Database, mode: str = "PASSIVE") -> tuple[int, int, int]:
    return database.checkpoint_wal(mode)


def vacuum(database: Database) -> None:
    database.vacuum()
