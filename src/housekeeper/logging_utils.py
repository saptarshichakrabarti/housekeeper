import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file = RotatingFileHandler(
        log_dir / "housekeeper.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file.setFormatter(fmt)
    root.addHandler(file)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
