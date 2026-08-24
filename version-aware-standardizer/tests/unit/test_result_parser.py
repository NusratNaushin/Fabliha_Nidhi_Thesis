"""The parser's job is mostly refusing things, so most of these tests check refusals.

Every "must never" here corresponds to a way of quietly changing what a
laboratory said. They are the reason this module exists.
"""

from __future__ import annotations

import pytest

from backend.app.constants import Comparator, DataAbsentReason, ResultIssue, ValueType
from backend.app.services.result_parser import (
    expected_value_type,
    looks_absent,
    parse_number,
    parse_result,
)


# ---------------------------------------------------------------- numbers
@pytest.mark.parametrize(
    "text,expected",
    [("7", 7.0), ("-7", -7.0), ("7.4", 7.4), (".5", 0.5), ("+3", 3.0), ("1.2e3", 1200.0)],
)
def test_plain_numbers_parse(text, expected):
    result = parse_result(text, scale="Qn")
    assert result.value_type is ValueType.QUANTITY
    assert result.numeric_value == expected
    assert result.comparator is None


@pytest.mark.parametrize(
    "text,comparator,number",
    [
        ("<10", Comparator.LESS_THAN, 10.0),
        ("<=10", Comparator.LESS_OR_EQUAL, 10.0),
        (">12000", Comparator.GREATER_THAN, 12000.0),
        (">=4.5", Comparator.GREATER_OR_EQUAL, 4.5),
        ("< 2.0", Comparator.LESS_THAN, 2.0),
    ],
)
def test_a_censored_result_keeps_both_halves(text, comparator, number):
    """"<2.0" is 2.0 *with* a sign. Dropping the sign invents a measurement."""
    result = parse_result(text, scale="Qn")
    assert result.value_type is ValueType.QUANTITY
    assert result.comparator is comparator
    assert result.numeric_value == number


def test_a_below_limit_result_is_flagged_as_one():
    result = parse_result("<2.0", 2.0, scale="Qn")
    assert ResultIssue.BELOW_DETECTION_LIMIT in result.issues


def test_an_above_limit_result_is_flagged_as_one():
    result = parse_result(">12000", 12000.0, scale="Qn")
    assert ResultIssue.ABOVE_DETECTION_LIMIT in result.issues


def test_parse_number_handles_a_bare_string():
    assert parse_number("<7.5") == (Comparator.LESS_THAN, 7.5)
    assert parse_number("banana") == (None, None)


# ------------------------------------------------- the things it must never do
@pytest.mark.parametrize("word", ["Negative", "NEGATIVE", "Trace", "Positive", "POS"])
def test_a_word_never_becomes_a_number(word):
    """The single most dangerous coercion available. It must not happen."""
    result = parse_result(word, None, scale="Ord")
    assert result.numeric_value is None
    assert result.value_type is not ValueType.QUANTITY
    assert result.text_value == word


@pytest.mark.parametrize("empty", [None, "", "   ", "N/A", "none", "-", "?"])
def test_a_missing_result_never_becomes_a_number(empty):
    result = parse_result(empty, None, scale="Qn")
    assert result.value_type is ValueType.ABSENT
    assert result.numeric_value is None
    assert result.data_absent_reason is DataAbsentReason.UNKNOWN
    assert ResultIssue.MISSING_VALUE in result.issues


def test_a_missing_result_is_never_zero():
    result = parse_result(None, None, scale="Qn")
    assert result.numeric_value != 0
    assert result.numeric_value is None


def test_a_process_state_is_not_a_result():
    """"ERROR" says no result was produced, which is not a result of zero."""
    result = parse_result("ERROR", None, scale="Qn")
    assert result.value_type is ValueType.ABSENT
    assert result.numeric_value is None
    assert result.data_absent_reason is DataAbsentReason.ERROR
    assert ResultIssue.NOT_A_NUMBER in result.issues


# ------------------------------------------------------------------- text
def test_a_narrative_scale_keeps_its_text_as_text():
    result = parse_result("No growth after 48 hours", None, scale="Nar")
    assert result.value_type is ValueType.STRING
    assert result.text_value == "No growth after 48 hours"


def test_an_ordinal_scale_becomes_a_candidate_for_coding():
    result = parse_result("Negative", None, scale="Ord")
    assert result.value_type is ValueType.CODEABLE_CONCEPT
    assert ResultIssue.TEXT_RESULT in result.issues


# ------------------------------------------------------------------ scales
def test_the_scale_sets_an_expectation_but_does_not_override_the_data():
    """A worded result on a numeric code is reported, not resolved by guessing."""
    result = parse_result("Negative", None, scale="Qn")
    assert result.value_type is ValueType.CODEABLE_CONCEPT
    assert ResultIssue.SCALE_MISMATCH in result.issues
    assert result.numeric_value is None


def test_a_number_on_a_categorical_scale_is_flagged():
    result = parse_result("42", 42.0, scale="Ord")
    assert result.value_type is ValueType.QUANTITY
    assert ResultIssue.SCALE_MISMATCH in result.issues


def test_semiqn_carries_either_because_loinc_uses_it_for_ph():
    """LOINC models pH as SemiQn with property LsCnc, and 2.83 has no Qn pH code.

    Treating SemiQn as categorical flagged every real pH result as a mismatch,
    which was our error and not the data's.
    """
    assert expected_value_type("SemiQn") is None
    numeric = parse_result("7.43", 7.43, scale="SemiQn")
    assert numeric.value_type is ValueType.QUANTITY
    assert ResultIssue.SCALE_MISMATCH not in numeric.issues

    graded = parse_result("1+", None, scale="SemiQn")
    assert graded.value_type is ValueType.CODEABLE_CONCEPT
    assert ResultIssue.SCALE_MISMATCH not in graded.issues


def test_ordqn_also_declines_to_expect_anything():
    assert expected_value_type("OrdQn") is None


def test_an_unknown_scale_lets_the_data_speak():
    assert expected_value_type(None) is None
    assert expected_value_type("Wibble") is None


# ------------------------------------------------------------- disagreement
def test_text_and_numeric_column_disagreeing_is_reported():
    """MIMIC ships both; when they differ that is a fact worth surfacing."""
    result = parse_result("120", 999.0, scale="Qn")
    assert result.numeric_value == 120.0, "the text is what a person wrote down"
    assert ResultIssue.VALUE_NUMERIC_MISMATCH in result.issues


def test_text_that_is_not_a_number_alongside_a_numeric_column_is_reported():
    result = parse_result("Negative", 0.0, scale="Ord")
    assert result.numeric_value is None
    assert ResultIssue.VALUE_NUMERIC_MISMATCH in result.issues


def test_an_implausible_number_is_kept_but_flagged():
    result = parse_result("1e15", None, scale="Qn")
    assert result.numeric_value == 1e15
    assert ResultIssue.PARSE_ERROR in result.issues


def test_looks_absent_covers_the_usual_placeholders():
    assert looks_absent(None) and looks_absent("") and looks_absent("N/A")
    assert not looks_absent("0")


def test_zero_is_a_real_result_not_an_absent_one():
    """A measured zero must survive, or every genuine zero becomes missing data."""
    result = parse_result("0", 0.0, scale="Qn")
    assert result.value_type is ValueType.QUANTITY
    assert result.numeric_value == 0.0
    assert ResultIssue.MISSING_VALUE not in result.issues


def test_the_result_serialises_for_the_api():
    payload = parse_result("<2.0", 2.0, scale="Qn").as_dict()
    assert payload["comparator"] == "<"
    assert payload["numeric_value"] == 2.0
    assert "BELOW_DETECTION_LIMIT" in payload["issues"]
