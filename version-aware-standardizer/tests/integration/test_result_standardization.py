"""The standardizer end to end, on synthetic rows with known answers.

These are the whole-pipeline guarantees: nothing is lost, nothing is invented,
and a suggestion never becomes an approval.
"""

from __future__ import annotations

import json

import pytest

from backend.app.constants import QualityStatus, ResultIssue, ReviewStatus, ValueType
from backend.app.models import (
    LocalMapping,
    SourceLabItem,
    SourceLabResult,
    StandardizationIssue,
    StandardizedLabObservation,
)
from backend.app.services.categorical_normalizer import seed_value_mappings
from backend.app.services.fhir_observation_exporter import (
    to_fhir_datetime,
    to_observation,
    validate_observation,
)
from backend.app.services.result_standardizer import run_standardization
from backend.app.services.unit_normalizer import seed_unit_rules
from tests.fixtures import synthetic as fx

DATASET = "TEST_LAB"


def _row(session, row_id, itemid, value, numeric=None, unit=None, flag=None):
    session.add(SourceLabResult(
        source_dataset=DATASET, source_row_id=str(row_id),
        subject_key="a" * 32, encounter_key="b" * 32,
        itemid=str(itemid), charttime="2150-06-01 08:30:00",
        raw_value=value, raw_numeric_value=numeric, raw_unit=unit, raw_flag=flag,
    ))


@pytest.fixture()
def lab(loinc_session):
    """A tiny lab: three tests against the synthetic LOINC release, and some results."""
    session = loinc_session
    seed_unit_rules(session)
    seed_value_mappings(session)

    session.add_all([
        # an ACTIVE code -> should end up approved
        SourceLabItem(source_dataset=DATASET, itemid="1", label="Sodium",
                      fluid="Blood", category="Chemistry",
                      original_loinc_code=fx.L_ACTIVE),
        # a DEPRECATED code with one replacement -> suggestion only
        SourceLabItem(source_dataset=DATASET, itemid="2", label="Old Test",
                      fluid="Blood", category="Chemistry",
                      original_loinc_code=fx.L_DEP_ONE),
        # never coded
        SourceLabItem(source_dataset=DATASET, itemid="3", label="Uncoded Test",
                      fluid="Urine", category="Chemistry", original_loinc_code=None),
    ])

    _row(session, 101, 1, "137", 137.0, "mEq/L")
    _row(session, 102, 1, "<2.0", 2.0, "mg/dl", flag="abnormal")
    _row(session, 103, 1, "Negative", None, None)
    _row(session, 104, 1, None, None, "mg/dL")
    _row(session, 105, 2, "42", 42.0, "mg/dL")
    _row(session, 106, 3, "7.4", 7.4, "flibbles")
    _row(session, 107, 1, "NotDone", None, None)
    # an itemid the dictionary has never heard of
    _row(session, 108, 999, "5", 5.0, "mg/dL")
    session.flush()
    return session


@pytest.fixture()
def run(lab):
    result = run_standardization(lab, source_dataset=DATASET)
    lab.flush()
    return result


def _by_row(session, run_id):
    return {
        o.source_row_id: o
        for o in session.query(StandardizedLabObservation).filter_by(
            standardization_run_id=run_id
        )
    }


# ------------------------------------------------------------ conservation
def test_nothing_is_lost(run, lab):
    """The invariant the whole pipeline is built around."""
    assert run.input_rows == 8
    assert run.standardized_rows + run.quarantined_rows == run.input_rows
    assert run.rows_accounted_for
    written = lab.query(StandardizedLabObservation).filter_by(
        standardization_run_id=run.id).count()
    assert written == run.input_rows


def test_every_raw_row_has_a_standardized_counterpart(run, lab):
    rows = _by_row(lab, run.id)
    assert set(rows) == {"101", "102", "103", "104", "105", "106", "107", "108"}


def test_the_raw_answer_survives_untouched(run, lab):
    rows = _by_row(lab, run.id)
    assert rows["101"].raw_value == "137"
    assert rows["101"].raw_unit == "mEq/L"
    assert rows["102"].raw_value == "<2.0"
    assert rows["106"].raw_unit == "flibbles", "an unknown unit is still preserved"


# ------------------------------------------------------------- terminology
def test_an_active_code_is_approved(run, lab):
    row = _by_row(lab, run.id)["101"]
    assert row.approved_current_loinc == fx.L_ACTIVE
    assert row.resolver_decision == "KEEP"
    assert row.current_loinc_version == fx.LOINC_NEW_VERSION


def test_a_retired_code_is_suggested_but_never_approved(run, lab):
    """The safety contract, expressed at the level of a single result."""
    row = _by_row(lab, run.id)["105"]
    assert row.resolver_decision == "SUGGEST_REPLACEMENT"
    assert row.engine_suggested_loinc == fx.L_ACTIVE
    assert row.approved_current_loinc is None, "a suggestion is not a decision"
    assert ResultIssue.LOINC_NOT_APPROVED.value in row.issues_json


def test_a_human_approval_supersedes_the_dictionary(lab):
    """An approved mapping is how a stale dictionary code gets superseded."""
    lab.add(LocalMapping(
        source_dataset=DATASET, local_code="2", local_text="Old Test",
        target_system="LOINC", target_code=fx.L_ACTIVE,
        mapped_against_version=fx.LOINC_NEW_VERSION,
        review_status=ReviewStatus.APPROVED.value,
    ))
    lab.flush()
    run = run_standardization(lab, source_dataset=DATASET)
    row = _by_row(lab, run.id)["105"]
    assert row.approved_current_loinc == fx.L_ACTIVE
    assert row.original_loinc_code == fx.L_DEP_ONE, "the dictionary still says what it said"


def test_an_uncoded_test_still_gets_its_value_standardized(run, lab):
    """No code is not a reason to throw the measurement away."""
    row = _by_row(lab, run.id)["106"]
    assert row.original_loinc_code is None
    assert row.approved_current_loinc is None
    assert row.value_type == ValueType.QUANTITY.value
    assert row.standard_numeric_value == 7.4
    assert ResultIssue.NO_LOINC_MAPPING.value in row.issues_json


# ------------------------------------------------------------------ values
def test_a_number_keeps_its_value_and_gains_a_ucum_unit(run, lab):
    row = _by_row(lab, run.id)["101"]
    assert row.value_type == ValueType.QUANTITY.value
    assert row.standard_numeric_value == 137.0
    assert row.standard_ucum_unit == "meq/L"


def test_a_censored_result_keeps_its_comparator(run, lab):
    row = _by_row(lab, run.id)["102"]
    assert row.comparator == "<"
    assert row.standard_numeric_value == 2.0
    assert row.standard_ucum_unit == "mg/dL"


def test_a_word_stays_a_word(run, lab):
    row = _by_row(lab, run.id)["103"]
    assert row.value_type == ValueType.CODEABLE_CONCEPT.value
    assert row.standard_numeric_value is None
    assert row.normalized_text_value == "Negative"
    assert row.coded_value_code is None, "no licence, so no invented code"


def test_a_missing_result_becomes_absence(run, lab):
    row = _by_row(lab, run.id)["104"]
    assert row.value_type == ValueType.ABSENT.value
    assert row.standard_numeric_value is None
    assert row.data_absent_reason == "unknown"


def test_a_process_state_becomes_absence(run, lab):
    row = _by_row(lab, run.id)["107"]
    assert row.value_type == ValueType.ABSENT.value
    assert row.standard_numeric_value is None


def test_an_unknown_unit_leaves_the_number_alone(run, lab):
    row = _by_row(lab, run.id)["106"]
    assert row.standard_numeric_value == 7.4
    assert row.standard_ucum_unit is None
    assert ResultIssue.UNIT_UNKNOWN.value in row.issues_json


# ------------------------------------------------------------------- flags
def test_an_abnormal_flag_becomes_an_interpretation(run, lab):
    assert _by_row(lab, run.id)["102"].interpretation_code == "A"


def test_an_empty_flag_does_not_mean_normal(run, lab):
    """MIMIC's FLAG only ever says "abnormal". Silence is not a clinical judgement."""
    assert _by_row(lab, run.id)["101"].interpretation_code is None


# -------------------------------------------------------------- quarantine
def test_an_unknown_test_is_quarantined_not_dropped(run, lab):
    row = _by_row(lab, run.id)["108"]
    assert row.quality_status == QualityStatus.QUARANTINED.value
    assert ResultIssue.UNKNOWN_ITEMID.value in row.issues_json
    assert row.raw_value == "5", "the row is kept, with its value"
    assert run.quarantined_rows == 1


def test_every_issue_is_recorded_as_a_row(run, lab):
    issues = lab.query(StandardizationIssue).filter_by(
        standardization_run_id=run.id).all()
    assert issues, "issues must be recorded, not just counted"
    assert all(i.detail for i in issues), "every issue explains itself"


def test_the_summary_reports_what_was_written(run):
    s = run.summary_json
    assert s["input_rows"] == 8
    assert s["rows_accounted_for"] is True
    assert sum(s["by_value_type"].values()) == 8
    assert sum(s["quality"].values()) == 8


def test_running_twice_gives_the_same_answer(lab):
    """Determinism: the same input must standardize the same way."""
    first = run_standardization(lab, source_dataset=DATASET)
    second = run_standardization(lab, source_dataset=DATASET)
    a = {k: (o.value_type, o.standard_numeric_value, o.standard_ucum_unit,
             o.approved_current_loinc, o.quality_status)
         for k, o in _by_row(lab, first.id).items()}
    b = {k: (o.value_type, o.standard_numeric_value, o.standard_ucum_unit,
             o.approved_current_loinc, o.quality_status)
         for k, o in _by_row(lab, second.id).items()}
    assert a == b


# =============================================================== FHIR export
def test_a_number_becomes_a_value_quantity(run, lab):
    resource = to_observation(_by_row(lab, run.id)["101"])
    assert resource["resourceType"] == "Observation"
    assert resource["valueQuantity"]["value"] == 137.0
    assert resource["valueQuantity"]["code"] == "meq/L"
    assert resource["valueQuantity"]["system"] == "http://unitsofmeasure.org"
    assert validate_observation(resource) == []


def test_a_censored_result_keeps_its_comparator_in_fhir(run, lab):
    resource = to_observation(_by_row(lab, run.id)["102"])
    assert resource["valueQuantity"]["comparator"] == "<"
    assert resource["valueQuantity"]["value"] == 2.0


def test_a_word_becomes_a_codeable_concept_with_text(run, lab):
    resource = to_observation(_by_row(lab, run.id)["103"])
    concept = resource["valueCodeableConcept"]
    assert concept["text"] == "Negative"
    assert "coding" not in concept, "no licence, so no coding"


def test_a_missing_result_becomes_data_absent_reason(run, lab):
    resource = to_observation(_by_row(lab, run.id)["104"])
    assert "dataAbsentReason" in resource
    assert not [k for k in resource if k.startswith("value")]


def test_status_is_unknown_because_the_source_does_not_say(run, lab):
    """Writing "final" would assert something MIMIC never recorded."""
    assert to_observation(_by_row(lab, run.id)["101"])["status"] == "unknown"


def test_the_subject_reference_is_a_pseudonym(run, lab):
    resource = to_observation(_by_row(lab, run.id)["101"])
    assert resource["subject"]["reference"] == "Patient/" + "a" * 32


def test_the_effective_time_is_a_real_fhir_datetime(run, lab):
    """MIMIC writes "2150-06-01 08:30:00"; FHIR needs the T."""
    resource = to_observation(_by_row(lab, run.id)["101"])
    assert resource["effectiveDateTime"] == "2150-06-01T08:30:00"
    assert validate_observation(resource) == []


def test_the_validator_catches_a_space_in_the_datetime():
    bad = {
        "resourceType": "Observation", "status": "unknown",
        "code": {"text": "x"}, "valueString": "y",
        "effectiveDateTime": "2150-06-01 08:30:00",
    }
    assert any("dateTime" in p for p in validate_observation(bad))


def test_to_fhir_datetime_only_changes_the_separator():
    assert to_fhir_datetime("2150-06-01 08:30:00") == "2150-06-01T08:30:00"
    assert to_fhir_datetime("2150-06-01T08:30:00") == "2150-06-01T08:30:00"
    assert to_fhir_datetime("2150-06-01") == "2150-06-01"
    assert to_fhir_datetime(None) is None


def test_the_local_identifier_travels_alongside_the_loinc_code(run, lab):
    """So a resource can always be traced back to the row it came from."""
    coding = to_observation(_by_row(lab, run.id)["101"])["code"]["coding"]
    systems = {c["system"] for c in coding}
    assert "http://loinc.org" in systems
    assert "urn:mimic:itemid" in systems


def test_a_row_with_no_approved_code_still_exports(run, lab):
    resource = to_observation(_by_row(lab, run.id)["106"])
    coding = resource["code"]["coding"]
    assert all(c["system"] != "http://loinc.org" for c in coding)
    assert validate_observation(resource) == []


def test_every_resource_in_the_run_validates(run, lab):
    for observation in lab.query(StandardizedLabObservation).filter_by(
        standardization_run_id=run.id
    ):
        resource = to_observation(observation)
        assert validate_observation(resource) == [], observation.source_row_id
        json.dumps(resource)  # must be serialisable
