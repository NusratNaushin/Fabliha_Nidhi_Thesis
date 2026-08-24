"""Shared pytest fixtures.

The test database is a throwaway SQLite file in a temp directory.  It is
configured through the environment *before* the application is imported, so the
same settings object the app uses in production is the one under test -- no
monkeypatching of module globals.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="vas-tests-"))

# By default the suite runs against a throwaway SQLite file, so a fresh clone is
# green before Docker is up.  Point VAS_TEST_DATABASE_URL at a *disposable*
# database to run the identical suite on PostgreSQL -- the fixtures drop and
# recreate every table, so never aim it at anything you care about.
_EXTERNAL_DB = os.environ.get("VAS_TEST_DATABASE_URL", "").strip()
os.environ["DATABASE_URL"] = (
    _EXTERNAL_DB or f"sqlite:///{(_TMP_ROOT / 'test.db').as_posix()}"
)
os.environ["REPORTS_DIR"] = str(_TMP_ROOT / "reports")
os.environ["DATA_RAW_DIR"] = str(_TMP_ROOT / "raw")
os.environ["LOG_LEVEL"] = "WARNING"

from backend.app.database import Base, SessionLocal, engine  # noqa: E402
from backend.app import models  # noqa: E402,F401
from backend.app.services.loinc_ingest import ingest_loinc_release  # noqa: E402
from backend.app.services.snomed_rf2_parser import ingest_snomed_release  # noqa: E402
from tests.fixtures import synthetic  # noqa: E402


def pytest_sessionfinish(session, exitstatus) -> None:  # pragma: no cover
    engine.dispose()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return _TMP_ROOT


@pytest.fixture(scope="session")
def release_dir(tmp_root: Path) -> Path:
    """Synthetic release archives, built once for the whole test session."""
    directory = tmp_root / "releases"
    directory.mkdir(parents=True, exist_ok=True)
    synthetic.write_loinc_old(directory)
    synthetic.write_loinc_new(directory)
    synthetic.write_snomed_old(directory)
    synthetic.write_snomed_new(directory)
    return directory


@pytest.fixture(scope="session")
def loinc_old_zip(release_dir: Path) -> Path:
    return release_dir / f"Loinc_{synthetic.LOINC_OLD_VERSION}.zip"


@pytest.fixture(scope="session")
def loinc_new_zip(release_dir: Path) -> Path:
    return release_dir / f"Loinc_{synthetic.LOINC_NEW_VERSION}.zip"


@pytest.fixture(scope="session")
def snomed_old_zip(release_dir: Path) -> Path:
    return (
        release_dir
        / f"SnomedCT_SyntheticRF2_PRODUCTION_{synthetic.SNOMED_OLD_VERSION}T120000Z.zip"
    )


@pytest.fixture(scope="session")
def snomed_new_zip(release_dir: Path) -> Path:
    return (
        release_dir
        / f"SnomedCT_SyntheticRF2_PRODUCTION_{synthetic.SNOMED_NEW_VERSION}T120000Z.zip"
    )


@pytest.fixture()
def session():
    """A clean database with the full schema and no rows."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture(scope="module")
def session_factory_module():
    """A clean database that lives for one test module.

    Used by the validation suite, where importing two real terminology releases
    costs minutes and must not be repeated per test.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def loinc_session(session, loinc_new_zip: Path):
    """Only the current LOINC release is imported."""
    ingest_loinc_release(
        session,
        file_path=loinc_new_zip,
        version=synthetic.LOINC_NEW_VERSION,
        make_current=True,
    )
    return session


@pytest.fixture()
def snomed_session(session, snomed_new_zip: Path):
    """Only the current SNOMED release is imported."""
    ingest_snomed_release(
        session,
        file_path=snomed_new_zip,
        version=synthetic.SNOMED_NEW_VERSION,
        make_current=True,
    )
    return session


@pytest.fixture()
def full_session(session, loinc_old_zip, loinc_new_zip, snomed_old_zip, snomed_new_zip):
    """Both releases of both terminologies; the newer one is current."""
    ingest_loinc_release(
        session,
        file_path=loinc_old_zip,
        version=synthetic.LOINC_OLD_VERSION,
        make_current=False,
    )
    ingest_loinc_release(
        session,
        file_path=loinc_new_zip,
        version=synthetic.LOINC_NEW_VERSION,
        make_current=True,
    )
    ingest_snomed_release(
        session,
        file_path=snomed_old_zip,
        version=synthetic.SNOMED_OLD_VERSION,
        make_current=False,
    )
    ingest_snomed_release(
        session,
        file_path=snomed_new_zip,
        version=synthetic.SNOMED_NEW_VERSION,
        make_current=True,
    )
    return session


@pytest.fixture()
def client(full_session):
    """FastAPI TestClient bound to the seeded test database."""
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as test_client:
        yield test_client
