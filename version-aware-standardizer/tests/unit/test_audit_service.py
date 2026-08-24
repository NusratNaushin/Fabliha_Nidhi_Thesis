"""Audit engine behaviour and reporting (Master Instruction 27, 38, 48)."""

from __future__ import annotations

import csv

import pytest

from backend.app.constants import (
    AuditRunStatus,
    Decision,
    ReviewStatus,
    TerminologyStatus,
)
from backend.app.services import audit_service, mapping_service
from tests.fixtures import synthetic as fx

# One mapping per decision branch, so the summary exercises every counter.
CASES = [
    ("m-active", fx.L_ACTIVE, "LOINC", Decision.KEEP),
    ("m-trial", fx.L_TRIAL, "LOINC", Decision.KEEP_WITH_WARNING),
    ("m-disc-one", fx.L_DISC_ONE, "LOINC", Decision.SUGGEST_REPLACEMENT),
    ("m-disc-many", fx.L_DISC_MANY, "LOINC", Decision.MANUAL_REVIEW),
    ("m-dep-one", fx.L_DEP_ONE, "LOINC", Decision.SUGGEST_REPLACEMENT),
    ("m-dep-none", fx.L_DEP_NONE, "LOINC", Decision.MANUAL_REVIEW),
    ("m-unknown", fx.L_UNKNOWN, "LOINC", Decision.UNKNOWN_CODE),
    ("m-sct-active", fx.S_ACTIVE, "SNOMED_CT", Decision.KEEP),
    ("m-sct-replaced", fx.S_REPLACED, "SNOMED_CT", Decision.SUGGEST_REPLACEMENT),
    ("m-sct-possibly", fx.S_POSSIBLY, "SNOMED_CT", Decision.MANUAL_REVIEW),
]


@pytest.fixture()
def seeded(full_session):
    for local_code, target, system, _ in CASES:
        mapping_service.create_mapping(
            full_session,
            source_dataset="MANUAL_TEST",
            local_code=local_code,
            local_text=f"local term for {target}",
            target_system=system,
            target_code=target,
            mapped_against_version=(
                fx.LOINC_OLD_VERSION if system == "LOINC" else fx.SNOMED_OLD_VERSION
            ),
        )
    full_session.commit()
    return full_session


def test_every_mapping_gets_the_expected_decision(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    results = {r.mapping_id: r for r in audit_service.list_results(seeded, run.id)}
    by_code = {r.old_code: r.decision for r in results.values()}
    for _, target, _, expected in CASES:
        assert by_code[target] == expected.value


def test_run_stamps_the_releases_it_used(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    assert run.loinc_version == fx.LOINC_NEW_VERSION
    assert run.snomed_version == fx.SNOMED_NEW_VERSION
    assert run.status == AuditRunStatus.COMPLETED.value
    assert run.completed_at is not None
    assert run.mapping_count == len(CASES)


def test_summary_counts_match_the_cases(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    summary = run.summary_json

    assert summary["total_mappings"] == len(CASES)
    assert summary["by_system"] == {"LOINC": 7, "SNOMED_CT": 3}
    assert summary["valid"] == 2          # L_ACTIVE + S_ACTIVE
    assert summary["trial_warning"] == 1
    assert summary["discouraged"] == 2
    assert summary["deprecated"] == 2
    assert summary["inactive_snomed"] == 2
    assert summary["unknown"] == 1
    assert summary["single_replacement"] == 3
    assert summary["multiple_replacement"] == 1
    assert summary["no_replacement"] == 1
    assert summary["manual_review_required"] == 4


def test_abstention_rate_is_reported(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    summary = run.summary_json
    expected = round(summary["manual_review_required"] / summary["total_mappings"], 4)
    assert summary["abstention_rate"] == expected


def test_metadata_drift_is_counted(full_session):
    mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="m-meta",
        local_text="haemoglobin",
        target_system="LOINC",
        target_code=fx.L_META,
        mapped_against_version=fx.LOINC_OLD_VERSION,
    )
    full_session.commit()
    run = audit_service.run_audit(full_session, export_csv=False)
    assert run.summary_json["metadata_changed"] == 1

    result = audit_service.list_results(full_session, run.id)[0]
    assert result.decision == Decision.KEEP.value
    assert result.terminology_status == TerminologyStatus.CURRENT_VALID.value
    assert "component" in result.metadata_json["metadata_diff"]


def test_audit_flags_mappings_that_need_a_human(seeded):
    audit_service.run_audit(seeded, export_csv=False)
    flagged = mapping_service.list_mappings(
        seeded, review_status=ReviewStatus.NEEDS_REVIEW.value
    )
    flagged_codes = {m.target_code for m in flagged}
    assert fx.L_DISC_MANY in flagged_codes
    assert fx.L_UNKNOWN in flagged_codes
    assert fx.L_ACTIVE not in flagged_codes


def test_scope_filters_are_honoured(seeded):
    run = audit_service.run_audit(seeded, target_system="LOINC", export_csv=False)
    assert run.mapping_count == 7
    assert run.scope_json["target_system"] == "LOINC"

    limited = audit_service.run_audit(seeded, limit=3, export_csv=False)
    assert limited.mapping_count == 3


def test_csv_report_is_written_with_the_agreed_columns(seeded):
    run = audit_service.run_audit(
        seeded, export_csv=True, report_name="unit_audit.csv"
    )
    assert run.report_path
    with open(run.report_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == audit_service.CSV_COLUMNS
        rows = list(reader)
    assert len(rows) == len(CASES)
    row = next(r for r in rows if r["old_code"] == fx.L_DEP_ONE)
    assert row["decision"] == Decision.SUGGEST_REPLACEMENT.value
    assert row["suggested_targets"] == fx.L_ACTIVE
    assert row["mapped_against_version"] == fx.LOINC_OLD_VERSION
    assert row["current_version"] == fx.LOINC_NEW_VERSION


def test_render_report_is_human_readable(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    text = audit_service.render_report(run)
    assert "Terminology Audit Report" in text
    assert f"LOINC current version:   {fx.LOINC_NEW_VERSION}" in text
    assert "Manual review required:" in text
    assert "Abstention rate:" in text


def test_audit_with_no_releases_still_records_a_reproducible_run(session):
    mapping_service.create_mapping(
        session,
        source_dataset="MANUAL_TEST",
        local_code="orphan",
        local_text="no releases imported",
        target_system="LOINC",
        target_code=fx.L_ACTIVE,
    )
    session.commit()
    run = audit_service.run_audit(session, export_csv=False)
    assert run.loinc_version is None
    result = audit_service.list_results(session, run.id)[0]
    assert result.decision == Decision.MANUAL_REVIEW.value
    assert result.reason == "NO_CURRENT_RELEASE"


def test_results_can_be_filtered_by_decision(seeded):
    run = audit_service.run_audit(seeded, export_csv=False)
    manual = audit_service.list_results(
        seeded, run.id, decision=Decision.MANUAL_REVIEW.value
    )
    assert manual
    assert all(r.decision == Decision.MANUAL_REVIEW.value for r in manual)


def test_two_runs_are_independent_and_both_survive(seeded):
    first = audit_service.run_audit(seeded, export_csv=False)
    second = audit_service.run_audit(seeded, export_csv=False)
    assert first.id != second.id
    runs = audit_service.list_runs(seeded)
    assert {r.id for r in runs} >= {first.id, second.id}
    assert len(audit_service.list_results(seeded, first.id)) == len(CASES)
