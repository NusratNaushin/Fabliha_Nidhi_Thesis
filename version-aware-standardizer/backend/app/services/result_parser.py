"""Reading what a laboratory result actually says.

This is the module where a convenient shortcut becomes a clinical error, so it
is written defensively and refuses things on purpose.

Three rules it will not break:

* **"Negative" is never 0.** Nor is "Trace" 0.1, nor "Positive" 1. Those look
  like helpful coercions and they destroy meaning: a negative result and a
  result of zero are different clinical statements, and downstream arithmetic
  cannot tell them apart afterwards.
* **A missing result is never 0.** Absence is recorded as absence, with a
  reason, so that an average over the column does not quietly include zeros
  that were never measured.
* **A censored result keeps its sign.** "<2.0" is 2.0 *with* a comparator, not
  2.0 and not 0. Dropping the "<" turns a below-detection-limit reading into a
  measured value, which is exactly the kind of silent change that makes a
  dataset untrustworthy.

The parser is told what shape of answer to expect (from the LOINC scale) but
never overrides what it actually finds -- if a quantitative test carries the
word "Negative", that mismatch is reported rather than resolved by guessing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from backend.app.constants import (
    AMBIGUOUS_SCALES,
    CATEGORICAL_SCALES,
    NARRATIVE_SCALES,
    Comparator,
    DataAbsentReason,
    LoincScale,
    ResultIssue,
    ValueType,
)

# A number optionally preceded by a comparator: "<2.0", ">= 4.5", "-7", "1.2e3".
_NUMERIC_RE = re.compile(
    r"""^\s*
    (?P<cmp><=|>=|<|>)?          # optional comparator
    \s*
    (?P<num>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    \s*$""",
    re.VERBOSE,
)

# Values that mean "nothing was recorded" rather than a result.
_NULLISH = frozenset({
    "", "-", "--", "---", ".", "n/a", "na", "none", "null", "nil",
    "not given", "no value", "unknown", "?",
})

# Strings labs use for a result that exists but is not a number. Recognising
# these is only used to explain *why* parsing stopped -- never to invent a value.
_ERROR_LIKE = frozenset({
    "error", "err", "invalid", "cancelled", "canceled", "not done", "nd",
    "test not performed", "tnp", "qns", "quantity not sufficient",
    "unable to report", "hemolyzed", "clotted", "lost", "specimen lost",
})

MAX_REASONABLE = 1e12  # a lab value beyond this is a data error, not a reading


@dataclass
class ParsedResult:
    """What one raw result turned out to be."""

    value_type: ValueType
    comparator: Comparator | None = None
    numeric_value: float | None = None
    text_value: str | None = None
    data_absent_reason: DataAbsentReason | None = None
    issues: list[ResultIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, issue: ResultIssue, note: str | None = None) -> None:
        if issue not in self.issues:
            self.issues.append(issue)
        if note and note not in self.notes:
            self.notes.append(note)

    def as_dict(self) -> dict:
        return {
            "value_type": self.value_type.value,
            "comparator": self.comparator.value if self.comparator else None,
            "numeric_value": self.numeric_value,
            "text_value": self.text_value,
            "data_absent_reason": (
                self.data_absent_reason.value if self.data_absent_reason else None
            ),
            "issues": [i.value for i in self.issues],
            "notes": self.notes,
        }


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def looks_absent(text: str | None) -> bool:
    return text is None or text.strip().lower() in _NULLISH


def parse_number(text: str) -> tuple[Comparator | None, float | None]:
    """Split ``"<2.0"`` into a comparator and a number. No comparator is fine."""
    match = _NUMERIC_RE.match(text)
    if not match:
        return None, None
    raw_cmp = match.group("cmp")
    comparator = Comparator(raw_cmp) if raw_cmp else None
    try:
        number = float(match.group("num"))
    except (TypeError, ValueError):
        return comparator, None
    if math.isnan(number) or math.isinf(number):
        return comparator, None
    return comparator, number


def expected_value_type(scale: str | None) -> ValueType | None:
    """What the LOINC scale says this test should produce.

    Returns ``None`` for scales that can go either way (``OrdQn``) or when no
    scale is known -- in which case the parser follows the data instead of
    imposing an expectation on it.
    """
    if not scale:
        return None
    scale = scale.strip()
    # Checked first: SemiQn is in CATEGORICAL_SCALES but also carries pH, and a
    # scale that can honestly be either must not generate an expectation.
    if scale in AMBIGUOUS_SCALES:
        return None
    if scale == LoincScale.QN.value:
        return ValueType.QUANTITY
    if scale in CATEGORICAL_SCALES:
        return ValueType.CODEABLE_CONCEPT
    if scale in NARRATIVE_SCALES:
        return ValueType.STRING
    return None  # something we do not model: let the data speak


def parse_result(
    raw_value: object,
    raw_numeric_value: object = None,
    *,
    scale: str | None = None,
) -> ParsedResult:
    """Work out what a raw result is, without ever inventing one.

    ``raw_value`` is the source's text; ``raw_numeric_value`` is its own numeric
    reading, which MIMIC provides in ``VALUENUM`` and leaves null for anything
    non-numeric. Both are used, and disagreement between them is reported.
    """
    text = _clean(raw_value)
    expected = expected_value_type(scale)

    # -- nothing was recorded ------------------------------------------
    if looks_absent(text):
        result = ParsedResult(
            value_type=ValueType.ABSENT,
            data_absent_reason=DataAbsentReason.UNKNOWN,
        )
        result.add(ResultIssue.MISSING_VALUE, "No result text was recorded.")
        return result

    assert text is not None  # narrowed by looks_absent

    numeric_hint: float | None = None
    if raw_numeric_value is not None and str(raw_numeric_value).strip() != "":
        try:
            candidate = float(raw_numeric_value)
            if not (math.isnan(candidate) or math.isinf(candidate)):
                numeric_hint = candidate
        except (TypeError, ValueError):
            numeric_hint = None

    comparator, number = parse_number(text)

    # -- it reads as a number ------------------------------------------
    if number is not None:
        result = ParsedResult(
            value_type=ValueType.QUANTITY, comparator=comparator, numeric_value=number
        )
        if abs(number) > MAX_REASONABLE:
            result.add(
                ResultIssue.PARSE_ERROR,
                f"{number} is beyond any plausible laboratory value; kept, but flagged.",
            )
        # The source's own numeric reading should agree with the text. When it
        # does not, that is a fact about the data worth surfacing -- we keep the
        # text's reading, because the text is what a human wrote down.
        if numeric_hint is not None and not math.isclose(
            numeric_hint, number, rel_tol=1e-9, abs_tol=1e-9
        ):
            result.add(
                ResultIssue.VALUE_NUMERIC_MISMATCH,
                f"Source text says {number} but its numeric column says {numeric_hint}.",
            )
        if comparator in (Comparator.LESS_THAN, Comparator.LESS_OR_EQUAL):
            result.add(
                ResultIssue.BELOW_DETECTION_LIMIT,
                f"Censored result: below {number}. The comparator is kept, never dropped.",
            )
        elif comparator in (Comparator.GREATER_THAN, Comparator.GREATER_OR_EQUAL):
            result.add(
                ResultIssue.ABOVE_DETECTION_LIMIT,
                f"Censored result: above {number}. The comparator is kept, never dropped.",
            )
        if expected is not None and expected is not ValueType.QUANTITY:
            result.add(
                ResultIssue.SCALE_MISMATCH,
                f"LOINC scale {scale!r} expects a category, but the result is a number.",
            )
        return result

    # -- it is text ------------------------------------------------------
    lowered = text.lower()

    if lowered in _ERROR_LIKE:
        result = ParsedResult(
            value_type=ValueType.ABSENT,
            text_value=text,
            data_absent_reason=DataAbsentReason.ERROR,
        )
        result.add(
            ResultIssue.NOT_A_NUMBER,
            f"{text!r} reports that no result was produced, not a result of zero.",
        )
        return result

    # Narrative scales keep their text as text; ordered and nominal scales are
    # candidates for a coded concept, which the categorical normaliser handles.
    if scale and scale.strip() in NARRATIVE_SCALES:
        value_type = ValueType.STRING
    else:
        value_type = ValueType.CODEABLE_CONCEPT

    result = ParsedResult(value_type=value_type, text_value=text)
    result.add(
        ResultIssue.TEXT_RESULT,
        f"{text!r} is a categorical or narrative result; it is kept as text, never "
        f"coerced to a number.",
    )

    if numeric_hint is not None:
        # The source thought this was numeric and we cannot see why. Say so
        # rather than quietly preferring one reading over the other.
        result.add(
            ResultIssue.VALUE_NUMERIC_MISMATCH,
            f"Text {text!r} does not parse as a number, yet the source's numeric "
            f"column holds {numeric_hint}.",
        )

    if expected is ValueType.QUANTITY:
        result.add(
            ResultIssue.SCALE_MISMATCH,
            f"LOINC scale {scale!r} expects a number, but the result is text.",
        )

    return result


__all__ = [
    "MAX_REASONABLE",
    "ParsedResult",
    "expected_value_type",
    "looks_absent",
    "parse_number",
    "parse_result",
]
