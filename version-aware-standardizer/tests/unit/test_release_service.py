"""Release registry: checksums, idempotency and current-release handover.

Covers Master Instruction 44 (idempotency) and 45 (checksum identity).
"""

from __future__ import annotations

import shutil

import pytest
from sqlalchemy import func, select

from backend.app.constants import TerminologySystem
from backend.app.models import LoincConceptVersion, SnomedHistoricalAssociation
from backend.app.services import release_service
from backend.app.services.loinc_ingest import ingest_loinc_release
from backend.app.services.snomed_rf2_parser import ingest_snomed_release
from backend.app.utils.checksum import sha256_bytes, sha256_file
from tests.fixtures import synthetic as fx


def test_sha256_is_content_based(tmp_path):
    original = tmp_path / "a.txt"
    original.write_bytes(b"terminology")
    renamed = tmp_path / "b.txt"
    shutil.copyfile(original, renamed)

    assert sha256_file(original) == sha256_file(renamed)
    assert sha256_file(original) == sha256_bytes(b"terminology")


def test_checksum_of_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "nope.zip")


def test_reimporting_the_same_release_is_rejected(session, loinc_new_zip):
    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    with pytest.raises(release_service.DuplicateReleaseError):
        ingest_loinc_release(
            session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
        )
    session.rollback()

    # No duplicated concept rows and no duplicated release row.
    assert (
        session.scalar(select(func.count()).select_from(LoincConceptVersion))
        == len(fx.loinc_new_rows())
    )
    assert len(release_service.list_releases(session, "LOINC")) == 1


def test_renaming_the_archive_does_not_create_a_second_release(
    session, loinc_new_zip, tmp_path
):
    """Master Instruction 45: identity is the checksum, not the file name."""
    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    disguised = tmp_path / "Loinc_totally_different_name.zip"
    shutil.copyfile(loinc_new_zip, disguised)

    with pytest.raises(release_service.DuplicateReleaseError) as excinfo:
        ingest_loinc_release(
            session, file_path=disguised, version="9.99"
        )
    session.rollback()
    assert "already imported" in str(excinfo.value)
    assert len(release_service.list_releases(session, "LOINC")) == 1


def test_same_version_with_different_content_is_refused(session, loinc_new_zip, tmp_path):
    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    altered = fx.write_loinc_release(
        tmp_path / "altered",
        version="altered",
        rows=fx.loinc_old_rows(),
        map_to=[],
        changes=[],
    )
    with pytest.raises(release_service.DuplicateReleaseError) as excinfo:
        ingest_loinc_release(
            session, file_path=altered, version=fx.LOINC_NEW_VERSION
        )
    session.rollback()
    assert "DIFFERENT checksum" in str(excinfo.value)


def test_new_release_takes_over_current_and_old_one_survives(
    session, loinc_old_zip, loinc_new_zip
):
    ingest_loinc_release(
        session, file_path=loinc_old_zip, version=fx.LOINC_OLD_VERSION
    )
    assert release_service.get_current(session, "LOINC").version == fx.LOINC_OLD_VERSION

    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    current = release_service.get_current(session, "LOINC")
    assert current.version == fx.LOINC_NEW_VERSION

    # Hard Rule: the superseded release is still there, with all of its rows.
    versions = {r.version: r.is_current for r in release_service.list_releases(session, "LOINC")}
    assert versions == {fx.LOINC_OLD_VERSION: False, fx.LOINC_NEW_VERSION: True}
    old_rows = session.scalar(
        select(func.count())
        .select_from(LoincConceptVersion)
        .where(LoincConceptVersion.release_version == fx.LOINC_OLD_VERSION)
    )
    assert old_rows == len(fx.loinc_old_rows())


def test_importing_an_older_release_can_skip_becoming_current(
    session, loinc_new_zip, loinc_old_zip
):
    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    ingest_loinc_release(
        session,
        file_path=loinc_old_zip,
        version=fx.LOINC_OLD_VERSION,
        make_current=False,
    )
    assert release_service.get_current(session, "LOINC").version == fx.LOINC_NEW_VERSION


def test_snomed_reimport_does_not_duplicate_associations(session, snomed_new_zip):
    ingest_snomed_release(
        session, file_path=snomed_new_zip, version=fx.SNOMED_NEW_VERSION
    )
    before = session.scalar(
        select(func.count()).select_from(SnomedHistoricalAssociation)
    )
    with pytest.raises(release_service.DuplicateReleaseError):
        ingest_snomed_release(
            session, file_path=snomed_new_zip, version=fx.SNOMED_NEW_VERSION
        )
    session.rollback()
    after = session.scalar(
        select(func.count()).select_from(SnomedHistoricalAssociation)
    )
    assert before == after


def test_require_current_raises_a_useful_message(session):
    with pytest.raises(release_service.ReleaseNotFoundError) as excinfo:
        release_service.require_current(session, "LOINC")
    assert "import_loinc" in str(excinfo.value)


def test_system_name_variants_are_normalised(session, snomed_new_zip):
    ingest_snomed_release(
        session, file_path=snomed_new_zip, version=fx.SNOMED_NEW_VERSION
    )
    for alias in ("SNOMED", "snomed_ct", "SNOMED-CT"):
        assert release_service.get_current(session, alias) is not None
    with pytest.raises(ValueError):
        release_service.get_current(session, "ICD10")


def test_current_versions_reports_missing_systems_as_null(session, loinc_new_zip):
    ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    payload = release_service.current_versions(session)
    assert payload[TerminologySystem.LOINC.value]["version"] == fx.LOINC_NEW_VERSION
    assert payload[TerminologySystem.SNOMED_CT.value] is None
