"""Shared fixtures for the housekeeper test suite.

Tests never touch a real external drive: everything runs against temporary directories and
the synthetic fixture generator in ``scripts/create_test_fixture.py``.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from create_test_fixture import build_fixture  # noqa: E402

from housekeeper.config import load_config  # noqa: E402
from housekeeper.database import Database  # noqa: E402
from housekeeper.scanner import DriveScanner  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return load_config(workspace_override=tmp_path / "workspace")


@pytest.fixture
def database(config):
    db = Database(config.database_path)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def fixture_root(tmp_path):
    """A rich synthetic drive tree produced by the shared fixture generator."""
    return build_fixture(tmp_path / "drive", clean=True)


@pytest.fixture
def scanned(config, database, fixture_root):
    """Scan the synthetic fixture and return ``(database, config, root)``."""
    DriveScanner(database, config).scan(fixture_root, incremental=False)
    return database, config, fixture_root


def analyze_and_classify(database, config):
    """Run the full analysis + classification pipeline used by several integration tests."""
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.analyzers.projects import run_project_analysis
    from housekeeper.analyzers.registry import run_content_analysis
    from housekeeper.policies import classify_all_entries

    run_exact_duplicate_analysis(database, config)
    run_content_analysis(database, config, None)
    run_project_analysis(database, config)
    return classify_all_entries(database, config)
