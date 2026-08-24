"""Archive handling and importer robustness.

Real terminology packages change their internal folder layout between releases
and drop or rename columns over the years.  These tests pin the behaviour the
Master Instruction asks for: find files by basename/pattern, tolerate extra
columns, degrade gracefully on missing optional columns, and fail loudly -- with
an actionable message -- when something genuinely required is absent.
"""

from __future__ import annotations

import zipfile

import pytest

from backend.app.services.loinc_ingest import (
    LOINC_COLUMN_ALIASES,
    REQUIRED_LOINC_COLUMNS,
    LoincIngestError,
    detect_version,
    ingest_loinc_release,
    resolve_columns,
)
from backend.app.services.snomed_rf2_parser import (
    Rf2ParseError,
    detect_version as detect_snomed_version,
    validate_rf2_archive,
    version_to_date,
)
from backend.app.utils.archive import ArchiveError, ReleaseArchive
from tests.fixtures import synthetic as fx


# ---------------------------------------------------------------------------
# ReleaseArchive
# ---------------------------------------------------------------------------
def test_finds_a_file_regardless_of_its_folder_depth(loinc_new_zip):
    with ReleaseArchive(loinc_new_zip) as archive:
        member = archive.require_basename("Loinc.csv")
        assert member.basename == "Loinc.csv"
        assert "/" in member.name  # it really was nested


def test_basename_lookup_is_case_insensitive(loinc_new_zip):
    with ReleaseArchive(loinc_new_zip) as archive:
        assert archive.find_by_basename("loinc.CSV")


def test_pattern_lookup_matches_rf2_names(snomed_new_zip):
    with ReleaseArchive(snomed_new_zip) as archive:
        member = archive.require_pattern("sct2_Concept_Snapshot*.txt")
        assert member.basename.startswith("sct2_Concept_Snapshot")


def test_shallowest_duplicate_wins(tmp_path):
    path = tmp_path / "dupes.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("deep/nested/copy/Loinc.csv", b"LOINC_NUM\n1-1\n")
        zf.writestr("Loinc.csv", b"LOINC_NUM\n2-2\n")
    with ReleaseArchive(path) as archive:
        assert archive.require_basename("Loinc.csv").name == "Loinc.csv"


def test_an_extracted_directory_works_like_a_zip(tmp_path, loinc_new_zip):
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(loinc_new_zip) as zf:
        zf.extractall(extracted)
    with ReleaseArchive(extracted) as archive:
        assert archive.require_basename("MapTo.csv")
        assert len(archive.members) > 1


def test_missing_path_is_a_clear_error(tmp_path):
    with pytest.raises(ArchiveError, match="does not exist"):
        ReleaseArchive(tmp_path / "nope.zip")


def test_a_non_zip_file_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a zip", encoding="utf-8")
    with pytest.raises(ArchiveError, match="not a valid ZIP"):
        ReleaseArchive(path)


def test_an_empty_archive_is_rejected(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    with pytest.raises(ArchiveError, match="empty"):
        ReleaseArchive(path)


def test_requiring_an_absent_file_names_what_was_found(loinc_new_zip):
    with ReleaseArchive(loinc_new_zip) as archive:
        with pytest.raises(ArchiveError) as excinfo:
            archive.require_basename("NotThere.csv")
        assert "NotThere.csv" in str(excinfo.value)
        assert "Loinc.csv" in str(excinfo.value)
        with pytest.raises(ArchiveError):
            archive.require_pattern("no_such_pattern*.txt")


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------
def test_extra_columns_are_ignored():
    header = ["LOINC_NUM", "STATUS", "SOME_FUTURE_COLUMN"]
    resolved = resolve_columns(
        header, LOINC_COLUMN_ALIASES, REQUIRED_LOINC_COLUMNS, "Loinc.csv"
    )
    assert resolved["loinc_num"] == "LOINC_NUM"
    assert resolved["status"] == "STATUS"
    assert "SOME_FUTURE_COLUMN" not in resolved.values()


def test_older_spellings_are_accepted():
    header = ["LOINC_NUM", "TIME_ASPECT", "SCALE_TYPE", "VERSION_LAST_CHANGED"]
    resolved = resolve_columns(
        header, LOINC_COLUMN_ALIASES, REQUIRED_LOINC_COLUMNS, "Loinc.csv"
    )
    assert resolved["time_aspect"] == "TIME_ASPECT"
    assert resolved["scale_type"] == "SCALE_TYPE"
    assert resolved["version_last_changed"] == "VERSION_LAST_CHANGED"


def test_absent_optional_columns_are_reported_not_fatal(caplog):
    resolved = resolve_columns(
        ["LOINC_NUM"], LOINC_COLUMN_ALIASES, REQUIRED_LOINC_COLUMNS, "Loinc.csv"
    )
    assert resolved == {"loinc_num": "LOINC_NUM"}


def test_a_missing_required_column_names_the_accepted_spellings():
    with pytest.raises(LoincIngestError) as excinfo:
        resolve_columns(
            ["SOMETHING_ELSE"],
            LOINC_COLUMN_ALIASES,
            REQUIRED_LOINC_COLUMNS,
            "Loinc.csv",
        )
    message = str(excinfo.value)
    assert "loinc_num" in message
    assert "LOINC_NUM" in message
    assert "SOMETHING_ELSE" in message


# ---------------------------------------------------------------------------
# LOINC ingest
# ---------------------------------------------------------------------------
def test_an_archive_without_loinc_csv_is_refused(session, tmp_path):
    path = tmp_path / "not_loinc.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("something/else.txt", b"hello")
    with pytest.raises(LoincIngestError) as excinfo:
        ingest_loinc_release(session, file_path=path, version="0.1")
    assert "LOINC Complete" in str(excinfo.value)


def test_a_release_missing_map_to_is_refused(session, tmp_path):
    path = tmp_path / "no_mapto.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("r/LoincTable/Loinc.csv", b"LOINC_NUM,STATUS\n1-1,ACTIVE\n")
    with pytest.raises(LoincIngestError, match="MapTo.csv"):
        ingest_loinc_release(session, file_path=path, version="0.1")


def test_duplicate_codes_inside_one_release_are_collapsed(session, tmp_path):
    rows = fx.loinc_new_rows()
    rows.append(rows[0])  # same LOINC_NUM twice
    path = fx.write_loinc_release(
        tmp_path, version="dupe", rows=rows, map_to=[], changes=[]
    )
    report = ingest_loinc_release(session, file_path=path, version="dupe")
    assert report.concepts == len(fx.loinc_new_rows())


def test_duplicate_map_to_pairs_are_collapsed_but_distinct_ones_are_kept(
    session, tmp_path
):
    map_to = fx.loinc_new_map_to()
    map_to.append(list(map_to[0]))  # exact duplicate row
    path = fx.write_loinc_release(
        tmp_path, version="dupe-map", rows=fx.loinc_new_rows(), map_to=map_to, changes=[]
    )
    report = ingest_loinc_release(session, file_path=path, version="dupe-map")
    assert report.map_to_rows == len(fx.loinc_new_map_to())


def test_blank_rows_are_skipped(session, tmp_path):
    rows = fx.loinc_new_rows()
    rows.append(["" for _ in fx.LOINC_HEADER])
    path = fx.write_loinc_release(
        tmp_path, version="blank", rows=rows, map_to=[["", "", ""]], changes=[]
    )
    report = ingest_loinc_release(session, file_path=path, version="blank")
    assert report.concepts == len(fx.loinc_new_rows())
    assert report.map_to_rows == 0


def test_an_empty_version_string_is_refused(session, loinc_new_zip):
    with pytest.raises(ValueError, match="non-empty"):
        ingest_loinc_release(session, file_path=loinc_new_zip, version="   ")


def test_version_detection_reads_the_package(loinc_new_zip):
    with ReleaseArchive(loinc_new_zip) as archive:
        assert detect_version(archive) == fx.LOINC_NEW_VERSION


def test_version_detection_returns_none_when_absent(tmp_path):
    path = tmp_path / "anonymous.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data/table.csv", b"LOINC_NUM\n")
    with ReleaseArchive(path) as archive:
        assert detect_version(archive) is None


def test_report_serialises_for_logging(session, loinc_new_zip):
    report = ingest_loinc_release(
        session, file_path=loinc_new_zip, version=fx.LOINC_NEW_VERSION
    )
    payload = report.as_dict()
    assert payload["version"] == fx.LOINC_NEW_VERSION
    assert payload["change_snapshot_present"] is True
    assert len(payload["sha256"]) == 64


# ---------------------------------------------------------------------------
# SNOMED RF2
# ---------------------------------------------------------------------------
def test_rf2_version_and_date_detection(snomed_new_zip):
    with ReleaseArchive(snomed_new_zip) as archive:
        version = detect_snomed_version(archive)
    assert version == fx.SNOMED_NEW_VERSION
    assert version_to_date(version).isoformat() == "2999-02-01"


def test_version_to_date_rejects_a_non_date_version():
    assert version_to_date("2.82") is None
    assert version_to_date("not-a-date") is None
    assert version_to_date("20261345") is None


def test_an_archive_without_a_concept_snapshot_is_refused(tmp_path):
    path = tmp_path / "no_concepts.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Snapshot/Refset/Content/der2_cRefset_x.txt", b"id\n")
    with ReleaseArchive(path) as archive:
        with pytest.raises(Rf2ParseError, match="sct2_Concept_Snapshot"):
            validate_rf2_archive(archive)


def test_missing_refsets_are_warnings_not_errors(tmp_root):
    path = fx.write_snomed_release(
        tmp_root / "bare-rf2",
        version="29990401",
        inactive=set(),
        with_associations=False,
        with_attribute_values=False,
    )
    with ReleaseArchive(path) as archive:
        warnings = validate_rf2_archive(archive)
    assert len(warnings) == 2
    assert any("association" in w for w in warnings)
    assert any("inactivation" in w for w in warnings)


def test_rf2_rows_shorter_than_the_header_are_padded(session, tmp_path):
    """Real RF2 exports occasionally end a line early; that must not crash."""
    from backend.app.services.snomed_rf2_parser import ingest_snomed_release

    path = tmp_path / "ragged.zip"
    concept_lines = (
        "id\teffectiveTime\tactive\tmoduleId\tdefinitionStatusId\n"
        "100000001\t29990101\t1\t900000000000207008\t900000000000074008\n"
        "100000002\t29990101\t1\n"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "r/Snapshot/Terminology/sct2_Concept_Snapshot_X_29990101.txt",
            concept_lines.encode("utf-8"),
        )
    report = ingest_snomed_release(session, file_path=path, version="29990101")
    assert report.concepts == 2
    assert report.associations == 0


# ---------------------------------------------------------------------------
# read_active_associations -- ground truth straight off the archive
# ---------------------------------------------------------------------------
def test_associations_can_be_read_back_from_the_archive(snomed_new_zip):
    """Validation needs a source of truth that is NOT the database.

    Comparing the parsed tables against the resolver compares our code with our
    own code and reports 100% whatever the parser did, so the check has to go
    back to the file.
    """
    from backend.app.services.snomed_rf2_parser import read_active_associations

    found = read_active_associations(snomed_new_zip)

    assert found[fx.S_REPLACED] == {("REPLACED_BY", fx.S_ACTIVE)}
    assert found[fx.S_SAME_AS] == {("SAME_AS", fx.S_ACTIVE)}
    assert found[fx.S_MULTI] == {
        ("REPLACED_BY", fx.S_ACTIVE),
        ("SAME_AS", fx.S_ACTIVE_2),
    }
    # The inactive refset member for S_NO_ASSOC must not appear.
    assert fx.S_NO_ASSOC not in found


def test_reading_associations_can_be_narrowed_to_a_few_concepts(snomed_new_zip):
    from backend.app.services.snomed_rf2_parser import read_active_associations

    found = read_active_associations(snomed_new_zip, [fx.S_REPLACED])
    assert set(found) == {fx.S_REPLACED}


def test_reading_associations_from_an_archive_without_them_is_empty(tmp_root):
    from backend.app.services.snomed_rf2_parser import read_active_associations

    path = fx.write_snomed_release(
        tmp_root / "assoc-free",
        version="29990601",
        inactive=set(),
        with_associations=False,
    )
    assert read_active_associations(path) == {}


def test_reading_associations_matches_what_was_imported(snomed_session, snomed_new_zip):
    """The file reader and the importer must agree, or one of them is wrong."""
    from sqlalchemy import select

    from backend.app.models import SnomedHistoricalAssociation
    from backend.app.constants import HISTORICAL_ASSOCIATION_REFSETS
    from backend.app.services.snomed_rf2_parser import read_active_associations

    from_file = read_active_associations(snomed_new_zip)

    from_db: dict[str, set[tuple[str, str]]] = {}
    for row in snomed_session.scalars(
        select(SnomedHistoricalAssociation).where(
            SnomedHistoricalAssociation.release_version == fx.SNOMED_NEW_VERSION,
            SnomedHistoricalAssociation.active.is_(True),
        )
    ):
        association = HISTORICAL_ASSOCIATION_REFSETS.get(row.refset_id)
        if association is None:
            continue
        from_db.setdefault(row.referenced_component_id, set()).add(
            (association, row.target_component_id)
        )

    assert from_file == from_db
