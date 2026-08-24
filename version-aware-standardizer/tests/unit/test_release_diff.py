"""Release-to-release comparison (Master Instruction 31, 32, 40, 41).

The headline assertion is ``missed_changes == 0``: every change the official
LOINC Change Snapshot declares, for the fields we model, must also be found by
our own diff.  That is a check against the vendor's own change log, not against
our expectations.
"""

from __future__ import annotations

import csv

import pytest

from backend.app.constants import Decision
from backend.app.services import loinc_diff, snomed_diff
from tests.fixtures import synthetic as fx


# ---------------------------------------------------------------------------
# LOINC
# ---------------------------------------------------------------------------
@pytest.fixture()
def report(full_session):
    return loinc_diff.diff_releases(
        full_session,
        old_version=fx.LOINC_OLD_VERSION,
        new_version=fx.LOINC_NEW_VERSION,
    )


def test_new_codes_are_detected(report):
    assert report.new_codes == [fx.L_NEW]


def test_no_codes_disappear_between_releases(report):
    """LOINC never deletes a code; the fixture honours that."""
    assert report.removed_codes == []


def test_status_transitions_are_counted(report):
    assert report.status_transitions["ACTIVE -> DISCOURAGED"] == 2
    assert report.status_transitions["ACTIVE -> DEPRECATED"] == 7


def test_metadata_only_changes_are_detected(report):
    changed = {
        (c.loinc_num, c.field) for c in report.changes if c.loinc_num == fx.L_META
    }
    assert changed == {(fx.L_META, "component"), (fx.L_META, "long_common_name")}


def test_every_official_change_is_reproduced(report):
    """The primary correctness evidence for the LOINC update engine."""
    validation = report.validation
    assert validation.change_snapshot_available is True
    assert validation.official_changes == len(fx.loinc_new_changes())
    assert validation.missed_count == 0
    assert validation.matched_changes == validation.official_changes


def test_diff_csv_contains_every_change_kind(report, tmp_root):
    with open(report.report_path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    kinds = {r["change_kind"] for r in rows}
    assert "NEW_CODE" in kinds
    assert "STATUS_CHANGE" in kinds
    assert "FIELD_CHANGE" in kinds
    assert any(
        r["loinc_num"] == fx.L_DEP_ONE and r["value_current"] == "DEPRECATED"
        for r in rows
    )


def test_status_change_codes_drives_the_validation_experiment(full_session):
    """Master Instruction 40: use the older release to simulate history."""
    grouped = loinc_diff.status_change_codes(
        full_session,
        old_version=fx.LOINC_OLD_VERSION,
        new_version=fx.LOINC_NEW_VERSION,
    )
    assert set(grouped["ACTIVE -> DISCOURAGED"]) == {fx.L_DISC_ONE, fx.L_DISC_MANY}
    assert fx.L_DEP_ONE in grouped["ACTIVE -> DEPRECATED"]


def test_diff_of_identical_versions_is_refused(full_session):
    with pytest.raises(loinc_diff.DiffError):
        loinc_diff.diff_releases(
            full_session,
            old_version=fx.LOINC_NEW_VERSION,
            new_version=fx.LOINC_NEW_VERSION,
        )


def test_diff_against_an_unimported_release_is_a_clear_error(full_session):
    with pytest.raises(loinc_diff.DiffError) as excinfo:
        loinc_diff.diff_releases(
            full_session, old_version="0.01", new_version=fx.LOINC_NEW_VERSION
        )
    assert "import_loinc" in str(excinfo.value)


def test_missing_change_snapshot_degrades_but_does_not_crash(session, tmp_root):
    """Older releases may ship no Change Snapshot (Master Instruction 13)."""
    from backend.app.services.loinc_ingest import ingest_loinc_release

    directory = tmp_root / "no-snapshot"
    old = fx.write_loinc_release(
        directory,
        version="8.01",
        rows=fx.loinc_old_rows(),
        map_to=[],
        include_change_snapshot=False,
    )
    new = fx.write_loinc_release(
        directory,
        version="8.02",
        rows=fx.loinc_new_rows(),
        map_to=fx.loinc_new_map_to(),
        include_change_snapshot=False,
    )
    ingest_loinc_release(session, file_path=old, version="8.01", make_current=False)
    report = ingest_loinc_release(session, file_path=new, version="8.02")
    assert report.change_snapshot_present is False
    assert report.skipped

    diff = loinc_diff.diff_releases(
        session, old_version="8.01", new_version="8.02", export_csv=False
    )
    assert diff.validation.change_snapshot_available is False
    assert diff.changes  # the computed diff still works
    assert "stands alone" in diff.render()


# ---------------------------------------------------------------------------
# SNOMED CT
# ---------------------------------------------------------------------------
@pytest.fixture()
def snomed_report(full_session):
    return snomed_diff.diff_releases(
        full_session,
        old_version=fx.SNOMED_OLD_VERSION,
        new_version=fx.SNOMED_NEW_VERSION,
    )


def test_every_newly_inactive_concept_is_detected(snomed_report):
    assert set(snomed_report.became_inactive) == fx.INACTIVE_IN_NEW
    assert snomed_report.inactive_detection_recall == 1.0


def test_inactivation_reasons_are_extracted_for_all_of_them(snomed_report):
    assert snomed_report.with_inactivation_reason == len(fx.INACTIVE_IN_NEW)


def test_only_the_concept_without_an_active_association_lacks_one(snomed_report):
    assert snomed_report.without_association == 1
    assert snomed_report.with_association == len(fx.INACTIVE_IN_NEW) - 1


def test_engine_suggests_only_for_the_safe_association_types(snomed_report):
    decisions = snomed_report.decisions
    # REPLACED BY (x1), SAME AS (x1) and the resolvable chain (x2:
    # head and mid both resolve to an active concept).
    assert decisions[Decision.SUGGEST_REPLACEMENT.value] == 4
    assert decisions[Decision.MANUAL_REVIEW.value] == 8
    assert sum(decisions.values()) == len(fx.INACTIVE_IN_NEW)


def test_no_unsafe_automatic_update_is_ever_recorded(snomed_report):
    assert snomed_report.unsafe_auto_update == 0


def test_snomed_diff_csv_records_the_official_association(snomed_report):
    with open(snomed_report.report_path, encoding="utf-8", newline="") as fh:
        rows = {r["concept_id"]: r for r in csv.DictReader(fh)}
    assert rows[fx.S_REPLACED]["association_types"] == "REPLACED_BY"
    assert rows[fx.S_REPLACED]["suggested_target"] == fx.S_ACTIVE
    assert rows[fx.S_POSSIBLY]["decision"] == Decision.MANUAL_REVIEW.value
    assert rows[fx.S_POSSIBLY]["suggested_target"] == ""


def test_snomed_diff_render_reports_the_targets(snomed_report):
    text = snomed_report.render()
    assert "Unsafe automatic updates:       0" in text
    assert "Inactive detection recall:      100.0%" in text
