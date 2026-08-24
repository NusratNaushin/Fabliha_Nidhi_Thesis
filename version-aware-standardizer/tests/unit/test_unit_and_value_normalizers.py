"""Units and categorical values: what may be changed, and what may not.

The line these tests police is between fixing a *spelling* and changing a
*number*. The first is safe to do in bulk; the second needs an approved,
test-specific rule, because the factor depends on what is being measured.
"""

from __future__ import annotations

import pytest

from backend.app.constants import ResultIssue, UnitStatus, ValueMappingStatus, ValueType
from backend.app.models import ResultValueMapping, UnitMappingRule
from backend.app.services.categorical_normalizer import (
    CategoricalNormalizer,
    parse_count_range,
    seed_value_mappings,
)
from backend.app.services.unit_normalizer import (
    UnitNormalizer,
    ambiguity_of,
    family_of,
    is_not_a_unit,
    looks_like_ucum,
    seed_unit_rules,
)


@pytest.fixture()
def units():
    return UnitNormalizer(session=None)


@pytest.fixture()
def categories():
    return CategoricalNormalizer(session=None)


# ================================================================== units
@pytest.mark.parametrize(
    "raw,ucum",
    [
        ("mg/dl", "mg/dL"), ("MG/DL", "mg/dL"), ("mg/dL", "mg/dL"),
        ("mEq/L", "meq/L"), ("MEQ/L", "meq/L"),
        ("mm Hg", "mm[Hg]"), ("mmHg", "mm[Hg]"),
        ("sec", "s"), ("SECONDS", "s"),
        ("IU/L", "[IU]/L"),
        ("mOsm/kg", "mosm/kg"),
        ("#/hpf", "/[HPF]"),
    ],
)
def test_spelling_is_normalised_to_ucum(units, raw, ucum):
    result = units.normalize(raw, 42.0)
    assert result.ucum_code == ucum


def test_a_spelling_fix_never_moves_the_number(units):
    result = units.normalize("mg/dl", 120.0)
    assert result.numeric_value == 120.0
    assert result.value_changed is False
    assert result.status in (UnitStatus.NORMALIZED, UnitStatus.VALID)


def test_counts_per_microlitre_use_a_power_of_ten(units):
    """K/uL means thousands per microlitre; UCUM writes that as 10*3/uL."""
    assert units.normalize("K/uL", 7.2).ucum_code == "10*3/uL"
    assert units.normalize("m/uL", 4.5).ucum_code == "10*6/uL"


def test_a_missing_unit_is_never_guessed(units):
    result = units.normalize("", 5.0)
    assert result.status is UnitStatus.MISSING
    assert result.ucum_code is None
    assert result.numeric_value == 5.0
    assert ResultIssue.UNIT_MISSING in result.issues


def test_an_unknown_unit_leaves_the_value_alone(units):
    result = units.normalize("flibbles", 5.0)
    assert result.status is UnitStatus.UNKNOWN
    assert result.ucum_code is None
    assert result.numeric_value == 5.0, "nothing may be converted on a guess"
    assert ResultIssue.UNIT_UNKNOWN in result.issues


def test_a_result_vocabulary_in_the_unit_column_is_named_as_such(units):
    """`+/-` and `Pos/Neg` turn up as units in real extracts. They are not units."""
    for raw in ("+/-", "Pos/Neg"):
        result = units.normalize(raw, None)
        assert result.status is UnitStatus.REVIEW_REQUIRED
        assert result.ucum_code is None
        assert "not a unit" in result.notes[0]
    assert is_not_a_unit("+/-")
    assert not is_not_a_unit("mg/dL")


def test_a_unit_with_no_determinate_ucum_form_goes_to_review(units):
    """"units" could be international, enzyme or arbitrary. Guessing would be wrong."""
    result = units.normalize("units", 12.0)
    assert result.status is UnitStatus.REVIEW_REQUIRED
    assert result.ucum_code is None
    assert result.numeric_value == 12.0
    assert ambiguity_of("units")


# -- dimension checking ---------------------------------------------------
def test_a_genuinely_impossible_pairing_is_quarantined(units):
    result = units.normalize("sec", 13.0, loinc_property="MCnc")
    assert result.status is UnitStatus.INCOMPATIBLE
    assert ResultIssue.UNIT_INCOMPATIBLE in result.issues
    assert result.numeric_value == 13.0, "an impossible unit never deletes the value"


@pytest.mark.parametrize(
    "raw,prop",
    [("mEq/L", "SCnc"), ("mg/dL", "SCnc"), ("%", "NCnc"), ("mmol/L", "MCnc")],
)
def test_normal_reporting_practice_is_not_an_error(units, raw, prop):
    """Sodium is a substance concentration reported in mEq/L on 13,851 real rows.

    Flagging those would have buried the review queue in false alarms and taught
    everyone to ignore the warning.
    """
    result = units.normalize(raw, 140.0, loinc_property=prop)
    assert result.status is not UnitStatus.INCOMPATIBLE
    assert ResultIssue.UNIT_INCOMPATIBLE not in result.issues


def test_amounts_of_an_analyte_share_a_family():
    assert family_of("mass_concentration") == family_of("substance_concentration")
    assert family_of("time") != family_of("mass_concentration")


def test_ucum_shape_check_is_structural_only():
    assert looks_like_ucum("mg/dL") and looks_like_ucum("10*3/uL")
    assert not looks_like_ucum("") and not looks_like_ucum("mg per dL")


# -- conversion -----------------------------------------------------------
def test_a_conversion_rule_moves_the_number_and_says_so(session):
    """The one operation that may change a value -- and only from a stored rule."""
    session.add(UnitMappingRule(
        source_unit="mg/dL", normalized_ucum_code="mmol/L", loinc_code="2345-7",
        conversion_factor=0.0555, conversion_offset=0.0, precision=3,
        rule_version="test", review_status="APPROVED", active=True,
    ))
    session.flush()

    normalizer = UnitNormalizer(session)
    converted = normalizer.normalize("mg/dL", 100.0, loinc_code="2345-7")
    assert converted.status is UnitStatus.CONVERTED
    assert converted.value_changed is True
    assert converted.numeric_value == pytest.approx(5.55, abs=1e-3)
    assert converted.ucum_code == "mmol/L"


def test_a_conversion_rule_does_not_leak_to_another_test(session):
    """Glucose and creatinine do not share a factor, so a rule names its test."""
    session.add(UnitMappingRule(
        source_unit="mg/dL", normalized_ucum_code="mmol/L", loinc_code="2345-7",
        conversion_factor=0.0555, rule_version="test", active=True,
    ))
    session.flush()

    normalizer = UnitNormalizer(session)
    other = normalizer.normalize("mg/dL", 100.0, loinc_code="2160-0")
    assert other.numeric_value == 100.0, "a different test must not be converted"
    assert other.status is not UnitStatus.CONVERTED


def test_seeded_rules_are_marked_for_review_and_change_no_numbers(session):
    added = seed_unit_rules(session)
    assert added > 50
    rules = session.query(UnitMappingRule).all()
    assert all(r.review_status == "SEEDED" for r in rules)
    assert all(not r.changes_the_number for r in rules), (
        "a seeded rule may fix a spelling; it may never move a value"
    )


def test_seeding_twice_adds_nothing_the_second_time(session):
    first = seed_unit_rules(session)
    second = seed_unit_rules(session)
    assert first > 0 and second == 0


# ========================================================== categorical values
@pytest.mark.parametrize(
    "raw,display",
    [("NEG", "Negative"), ("negative", "Negative"), ("not detected", "Negative"),
     ("POS", "Positive"), ("TR", "Trace"), ("MOD", "Moderate"), ("ART", "Arterial")],
)
def test_wording_is_normalised(categories, raw, display):
    assert categories.normalize(raw).normalized_display == display


def test_no_code_is_invented_without_a_licence(categories):
    result = categories.normalize("NEG")
    assert result.status is ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING
    assert result.target_code is None
    assert result.target_system is None
    assert ResultIssue.CODE_PENDING_LICENCE in result.issues


def test_a_process_state_becomes_absence_not_a_finding(categories):
    """"NotDone" says what happened to the specimen, not what was found."""
    for raw in ("NotDone", "HOLD", "See Comments", "QNS"):
        result = categories.normalize(raw)
        assert result.value_type is ValueType.ABSENT, raw
        assert result.data_absent_reason is not None


def test_a_counted_range_keeps_its_span(categories):
    """Replacing "0-2" with 1 would claim precision the laboratory never gave."""
    result = categories.normalize("0-2")
    assert result.value_type is ValueType.STRING
    assert result.normalized_display == "0-2"
    assert parse_count_range("3-5") == (3, 5)
    assert parse_count_range("5-3") is None
    assert parse_count_range("banana") is None


def test_unknown_wording_is_preserved_verbatim(categories):
    result = categories.normalize("Wibble Factor 9")
    assert result.status is ValueMappingStatus.UNMAPPED
    assert result.normalized_display == "Wibble Factor 9"
    assert ResultIssue.CATEGORICAL_UNMAPPED in result.issues


def test_the_ordinal_family_keeps_its_order(categories):
    ranks = [categories.normalize(v).ordinal_rank for v in ("NEG", "TR", "1+", "2+", "3+")]
    assert ranks == sorted(ranks), "Negative < Trace < 1+ < 2+ < 3+"
    assert ranks[0] == 0


def test_a_stored_rule_beats_the_seed_table(session):
    session.add(ResultValueMapping(
        loinc_code=None, source_value="neg", normalized_display="No growth",
        mapping_status=ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING.value,
        rule_version="test", active=True,
    ))
    session.flush()
    result = CategoricalNormalizer(session).normalize("NEG")
    assert result.normalized_display == "No growth"


def test_a_test_specific_rule_beats_a_general_one(session):
    session.add_all([
        ResultValueMapping(loinc_code=None, source_value="neg",
                           normalized_display="Negative", rule_version="t1", active=True),
        ResultValueMapping(loinc_code="600-7", source_value="neg",
                           normalized_display="No growth", rule_version="t2", active=True),
    ])
    session.flush()
    normalizer = CategoricalNormalizer(session)
    assert normalizer.normalize("NEG", loinc_code="600-7").normalized_display == "No growth"
    assert normalizer.normalize("NEG", loinc_code="2345-7").normalized_display == "Negative"


def test_seeded_value_rules_carry_no_code(session):
    added = seed_value_mappings(session)
    assert added > 30
    rules = session.query(ResultValueMapping).all()
    assert all(r.target_code is None for r in rules)
    assert all(
        r.mapping_status == ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING.value
        for r in rules
    )
