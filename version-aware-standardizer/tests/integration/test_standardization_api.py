"""The endpoints the console reads.

Shaped for one screen at a time, so these check the shapes rather than the
arithmetic -- the arithmetic is tested where it happens.
"""

from __future__ import annotations

import pytest

from backend.app.models import SourceLabItem, SourceLabResult
from backend.app.services.categorical_normalizer import seed_value_mappings
from backend.app.services.result_standardizer import run_standardization
from backend.app.services.unit_normalizer import seed_unit_rules
from tests.fixtures import synthetic as fx

DATASET = "API_LAB"


@pytest.fixture()
def standardized(full_session, client):
    """A small standardized run behind the API client."""
    session = full_session
    seed_unit_rules(session)
    seed_value_mappings(session)
    session.add_all([
        SourceLabItem(source_dataset=DATASET, itemid="1", label="Sodium",
                      fluid="Blood", category="Chemistry",
                      original_loinc_code=fx.L_ACTIVE),
        SourceLabItem(source_dataset=DATASET, itemid="2", label="Uncoded",
                      fluid="Urine", category="Chemistry", original_loinc_code=None),
    ])
    for i, (item, value, numeric, unit) in enumerate([
        ("1", "137", 137.0, "mEq/L"),
        ("1", "Negative", None, None),
        ("2", "7.4", 7.4, "mg/dL"),
    ], start=1):
        session.add(SourceLabResult(
            source_dataset=DATASET, source_row_id=str(i), subject_key="c" * 32,
            itemid=item, charttime="2150-01-01 00:00:00",
            raw_value=value, raw_numeric_value=numeric, raw_unit=unit,
        ))
    session.flush()
    run = run_standardization(session, source_dataset=DATASET)
    session.commit()
    return client, run


def test_coverage_answers_the_overview_in_one_call(standardized):
    client, run = standardized
    body = client.get("/api/v1/standardization/coverage").json()
    assert body["run_id"] == run.id
    assert body["input_rows"] == 3
    assert body["rows_accounted_for"] is True
    assert set(body["terminology"]) >= {
        "with_any_code", "with_approved_code", "present_but_stale", "no_code_at_all"
    }
    assert body["terminology"]["no_code_at_all"] == 1


def test_the_value_type_breakdown_accounts_for_every_row(standardized):
    """A reader must be able to trust that every row is somewhere in the table."""
    client, run = standardized
    body = client.get("/api/v1/standardization/coverage").json()
    assert sum(body["by_value_type"].values()) == body["input_rows"]
    assert sum(body["quality"].values()) == body["input_rows"]


def test_runs_can_be_listed_and_fetched(standardized):
    client, run = standardized
    runs = client.get("/api/v1/standardization/runs").json()
    assert runs and runs[0]["id"] == run.id
    one = client.get(f"/api/v1/standardization/runs/{run.id}").json()
    assert one["source_dataset"] == DATASET
    assert one["rows_accounted_for"] is True
    latest = client.get("/api/v1/standardization/runs/latest").json()
    assert latest["id"] == run.id


def test_results_page_carries_both_the_raw_and_the_standard_form(standardized):
    """The console shows before and after, so the API must return both."""
    client, run = standardized
    body = client.get(f"/api/v1/standardization/runs/{run.id}/results").json()
    assert body["total"] == 3
    row = next(r for r in body["results"] if r["raw_value"] == "137")
    assert row["raw_unit"] == "mEq/L"
    assert row["standard_ucum_unit"] == "meq/L"
    assert row["approved_current_loinc"] == fx.L_ACTIVE
    assert row["value_type"] == "QUANTITY"


def test_results_can_be_filtered(standardized):
    client, run = standardized
    base = f"/api/v1/standardization/runs/{run.id}/results"
    numbers = client.get(f"{base}?value_type=QUANTITY").json()
    assert all(r["value_type"] == "QUANTITY" for r in numbers["results"])
    words = client.get(f"{base}?value_type=CODEABLE_CONCEPT").json()
    assert all(r["value_type"] == "CODEABLE_CONCEPT" for r in words["results"])
    searched = client.get(f"{base}?search=Sodium").json()
    assert searched["total"] == 2


def test_results_paginate(standardized):
    client, run = standardized
    base = f"/api/v1/standardization/runs/{run.id}/results"
    first = client.get(f"{base}?limit=2&offset=0").json()
    second = client.get(f"{base}?limit=2&offset=2").json()
    assert first["returned"] == 2 and second["returned"] == 1
    assert {r["id"] for r in first["results"]} & {r["id"] for r in second["results"]} == set()


def test_issues_are_grouped_with_examples(standardized):
    client, run = standardized
    body = client.get(f"/api/v1/standardization/runs/{run.id}/issues").json()
    assert body["input_rows"] == 3
    codes = {g["issue_code"] for g in body["issues"]}
    assert "NO_LOINC_MAPPING" in codes
    group = next(g for g in body["issues"] if g["issue_code"] == "NO_LOINC_MAPPING")
    assert group["rows"] >= 1
    assert group["examples"] and group["examples"][0]["detail"]


def test_one_result_can_be_seen_as_fhir(standardized):
    client, run = standardized
    results = client.get(f"/api/v1/standardization/runs/{run.id}/results").json()
    row = next(r for r in results["results"] if r["value_type"] == "QUANTITY")
    body = client.get(f"/api/v1/standardization/results/{row['id']}/fhir").json()
    assert body["resource"]["resourceType"] == "Observation"
    assert body["validation_problems"] == []
    assert body["resource"]["subject"]["reference"].startswith("Patient/")


def test_an_unknown_result_is_a_clean_404(standardized):
    client, _ = standardized
    assert client.get("/api/v1/standardization/results/424242/fhir").status_code == 404


def test_unmapped_tests_come_with_what_a_reviewer_needs(standardized):
    """A person choosing a code needs to see what the test actually produces."""
    client, _ = standardized
    body = client.get(f"/api/v1/standardization/unmapped?dataset={DATASET}").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["label"] == "Uncoded"
    assert item["result_count"] == 1
    assert item["observed_units"] == [["mg/dL", 1]]
    assert item["examples"] == ["7.4"]


def test_a_missing_run_explains_what_to_do(client):
    """Before anything is standardized, the API says so rather than 500ing."""
    res = client.get("/api/v1/standardization/coverage")
    assert res.status_code == 404
    assert "standardize_mimic_results" in res.json()["detail"]


def test_the_console_javascript_explains_every_issue_code(client):
    """The UI must not show a raw enum it has no words for."""
    js = client.get("/ui/app.js").text
    for code in (
        "TEXT_RESULT", "NO_LOINC_MAPPING", "LOINC_NOT_APPROVED", "UNIT_MISSING",
        "UNIT_UNKNOWN", "SCALE_MISMATCH", "CODE_PENDING_LICENCE", "UNKNOWN_ITEMID",
        "BELOW_DETECTION_LIMIT", "CATEGORICAL_UNMAPPED",
    ):
        assert code in js, code
