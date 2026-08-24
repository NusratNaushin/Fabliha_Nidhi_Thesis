"""Making sense of results that are words rather than numbers.

Roughly one MIMIC result in nine is text, and the text is not tidy: ``NEG``,
``NEGATIVE`` and ``Negative`` all appear, alongside ``TR``, ``1+``, ``FEW`` and
``OCCASIONAL``. Comparing them requires a curated table, not a lowercase call --
because whether two spellings mean the same thing is a clinical judgement, and
sometimes the answer is no.

Three distinctions the real data forced, and that a naive normaliser would miss:

**Some "results" are not results.** ``NotDone``, ``HOLD`` and ``See Comments``
record what happened to the specimen, not what was found. Treating them as
categorical findings would put a fictional observation in the record, so they
resolve to *absent* with a reason instead.

**Some are ranges.** Microscopy reports ``0-2`` or ``3-5`` cells per field. That
is neither a number nor a category, and collapsing it to a midpoint would invent
precision the laboratory never claimed.

**Some are ordered.** ``NEG < TR < 1+ < 2+ < 3+ < 4+`` is a real scale, and the
ordering is recorded so that later work can use it without re-deriving it.

Finally, and per the project's standing rule: with no SNOMED CT licence in
place, a recognised value gets normalised *text* and a null code, marked
``TEXT_NORMALIZED_CODE_PENDING``. A plausible-looking concept id would be worse
than an honest gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.constants import (
    DataAbsentReason,
    ResultIssue,
    ValueMappingStatus,
    ValueType,
)
from backend.app.models import ResultValueMapping
from backend.app.utils.logging import get_logger

log = get_logger("categorical")

# A count reported as a span: "0-2", "3-5", "10-20".
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")

# Strings that describe the handling of a specimen rather than a finding.
# These become an absent value with a reason -- never a category.
PROCESS_STATES: dict[str, str] = {
    "notdone": "Test not performed",
    "not done": "Test not performed",
    "nd": "Test not performed",
    "hold": "Specimen held",
    "see comments": "Result reported in a comment",
    "see note": "Result reported in a note",
    "comment": "Result reported in a comment",
    "pending": "Result pending",
    "cancelled": "Test cancelled",
    "canceled": "Test cancelled",
    "qns": "Quantity not sufficient",
    "unable to report": "Unable to report",
    "tnp": "Test not performed",
    "done": "Recorded as done, with no finding given",
}

# The ordinal family used for dipsticks and microscopy. The rank is recorded so
# that "2+ is more than 1+" survives standardization; it is not turned into a
# number, because the spacing between these steps is not defined.
ORDINAL_RANK: dict[str, int] = {
    "Negative": 0, "Trace": 1, "1+": 2, "2+": 3, "3+": 4, "4+": 5,
}

# Curated equivalences, seeded from the values that actually occur. Each is a
# claim that two strings mean the same thing, so they are reviewable rows rather
# than a hidden function.
SEED_VALUE_MAPPINGS: dict[str, str] = {
    # presence / absence
    "neg": "Negative", "negative": "Negative", "not detected": "Negative",
    "nonreactive": "Negative", "non-reactive": "Negative", "nr": "Negative",
    "pos": "Positive", "positive": "Positive", "detected": "Positive",
    "reactive": "Positive",
    "tr": "Trace", "trace": "Trace",
    "1+": "1+", "2+": "2+", "3+": "3+", "4+": "4+",
    # normality as stated by the laboratory
    "normal": "Normal", "abnormal": "Abnormal", "wnl": "Normal",
    "low": "Low", "high": "High", "very low": "Very low", "very high": "Very high",
    # semi-quantitative amounts
    "none": "None seen", "rare": "Rare", "occ": "Occasional",
    "occasional": "Occasional", "few": "Few", "sm": "Small", "small": "Small",
    "mod": "Moderate", "moderate": "Moderate", "many": "Many",
    "lg": "Large", "large": "Large", "mod-many": "Moderate to many",
    # appearance
    "clear": "Clear", "hazy": "Hazy", "cloudy": "Cloudy", "turbid": "Turbid",
    "yellow": "Yellow", "straw": "Straw", "amber": "Amber", "red": "Red",
    "orange": "Orange", "brown": "Brown", "colorless": "Colourless",
    # blood-gas specimen source
    "art": "Arterial", "arterial": "Arterial",
    "ven": "Venous", "venous": "Venous",
    "mix": "Mixed venous", "mixed": "Mixed venous",
    # ventilation state recorded alongside a blood gas
    "intubated": "Intubated", "not intubated": "Not intubated",
    "spontaneous": "Spontaneous", "controlled": "Controlled",
    "random": "Random collection",
}


@dataclass
class NormalizedCategory:
    """What happened to one textual result."""

    status: ValueMappingStatus
    value_type: ValueType = ValueType.CODEABLE_CONCEPT
    normalized_display: str | None = None
    target_system: str | None = None
    target_code: str | None = None
    ordinal_rank: int | None = None
    data_absent_reason: DataAbsentReason | None = None
    rule_id: int | None = None
    issues: list[ResultIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, issue: ResultIssue | None = None, note: str | None = None) -> None:
        if issue and issue not in self.issues:
            self.issues.append(issue)
        if note and note not in self.notes:
            self.notes.append(note)

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "value_type": self.value_type.value,
            "normalized_display": self.normalized_display,
            "target_system": self.target_system,
            "target_code": self.target_code,
            "ordinal_rank": self.ordinal_rank,
            "data_absent_reason": (
                self.data_absent_reason.value if self.data_absent_reason else None
            ),
            "rule_id": self.rule_id,
            "issues": [i.value for i in self.issues],
            "notes": self.notes,
        }


def parse_count_range(text: str) -> tuple[int, int] | None:
    """``"0-2"`` -> ``(0, 2)``. Anything else -> ``None``."""
    match = _RANGE_RE.match(text)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if low <= high else None


class CategoricalNormalizer:
    """Applies the curated value table, falling back to the seed equivalences."""

    def __init__(self, session: Session | None = None, *, use_seed: bool = True) -> None:
        self.session = session
        self.use_seed = use_seed
        self._rules: dict[tuple[str, str | None], ResultValueMapping] | None = None

    def _load_rules(self) -> dict[tuple[str, str | None], ResultValueMapping]:
        if self._rules is not None:
            return self._rules
        rules: dict[tuple[str, str | None], ResultValueMapping] = {}
        if self.session is not None:
            for rule in self.session.scalars(
                select(ResultValueMapping).where(ResultValueMapping.active.is_(True))
            ):
                rules[(rule.source_value.strip().lower(), rule.loinc_code)] = rule
        self._rules = rules
        return rules

    def find_rule(self, value: str, loinc_code: str | None) -> ResultValueMapping | None:
        rules = self._load_rules()
        key = value.strip().lower()
        if loinc_code and (key, loinc_code) in rules:
            return rules[(key, loinc_code)]
        return rules.get((key, None))

    def normalize(
        self, text: str | None, *, loinc_code: str | None = None
    ) -> NormalizedCategory:
        """Normalise one textual result without ever inventing a code."""
        raw = (text or "").strip()
        if not raw:
            out = NormalizedCategory(
                status=ValueMappingStatus.UNMAPPED,
                value_type=ValueType.ABSENT,
                data_absent_reason=DataAbsentReason.UNKNOWN,
            )
            out.add(ResultIssue.MISSING_VALUE, "No text to normalise.")
            return out

        lowered = raw.lower()

        # 1. a specimen-handling note, not a finding
        if lowered in PROCESS_STATES:
            out = NormalizedCategory(
                status=ValueMappingStatus.UNMAPPED,
                value_type=ValueType.ABSENT,
                normalized_display=PROCESS_STATES[lowered],
                data_absent_reason=DataAbsentReason.ERROR,
            )
            out.add(
                ResultIssue.NOT_A_NUMBER,
                f"{raw!r} records what happened to the specimen ("
                f"{PROCESS_STATES[lowered].lower()}), not a finding. Recorded as an "
                f"absent value with a reason rather than as a result.",
            )
            return out

        # 2. a counted range
        span = parse_count_range(raw)
        if span is not None:
            low, high = span
            out = NormalizedCategory(
                status=ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING,
                value_type=ValueType.STRING,
                normalized_display=f"{low}-{high}",
            )
            out.add(
                note=f"A counted range ({low} to {high}), typically cells per field. Kept "
                     f"as a range: replacing it with a midpoint would claim a precision "
                     f"the laboratory did not report.",
            )
            return out

        # 3. a curated rule
        rule = self.find_rule(raw, loinc_code)
        if rule is not None:
            out = NormalizedCategory(
                status=ValueMappingStatus(rule.mapping_status),
                normalized_display=rule.normalized_display,
                target_system=rule.target_system,
                target_code=rule.target_code,
                ordinal_rank=ORDINAL_RANK.get(rule.normalized_display),
                rule_id=rule.id,
            )
            if not rule.target_code:
                out.add(
                    ResultIssue.CODE_PENDING_LICENCE,
                    f"Normalised to {rule.normalized_display!r}. No standard code is "
                    f"attached because SNOMED CT International is not licensed here; "
                    f"the text is kept and the code left null rather than invented.",
                )
            return out

        # 4. the seed equivalences
        if self.use_seed and lowered in SEED_VALUE_MAPPINGS:
            display = SEED_VALUE_MAPPINGS[lowered]
            out = NormalizedCategory(
                status=ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING,
                normalized_display=display,
                ordinal_rank=ORDINAL_RANK.get(display),
            )
            out.add(
                ResultIssue.CODE_PENDING_LICENCE,
                f"Normalised {raw!r} to {display!r}. No standard code is attached "
                f"because SNOMED CT International is not licensed here.",
            )
            return out

        # 5. we do not recognise it -- keep it exactly as it came
        out = NormalizedCategory(
            status=ValueMappingStatus.UNMAPPED,
            normalized_display=raw,
        )
        out.add(
            ResultIssue.CATEGORICAL_UNMAPPED,
            f"No rule for the result {raw!r}. The original text is preserved unchanged; "
            f"nothing is guessed about what it means.",
        )
        return out


def seed_value_mappings(session: Session, *, rule_version: str = "seed-1") -> int:
    """Load the curated equivalences into the database as reviewable rows.

    They arrive as ``TEXT_NORMALIZED_CODE_PENDING`` with a null code, which is
    the honest state without a SNOMED CT licence: the wording is standardised,
    the coding is not yet possible, and the record says so.
    """
    existing = {
        (r.source_value, r.loinc_code)
        for r in session.scalars(
            select(ResultValueMapping).where(ResultValueMapping.rule_version == rule_version)
        )
    }
    added = 0
    for source_value, display in SEED_VALUE_MAPPINGS.items():
        if (source_value, None) in existing:
            continue
        session.add(
            ResultValueMapping(
                loinc_code=None,
                source_value=source_value,
                normalized_display=display,
                target_system=None,
                target_code=None,
                mapping_status=ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING.value,
                rule_version=rule_version,
                active=True,
            )
        )
        added += 1
    session.flush()
    log.info("seeded %d categorical value rules (version %s)", added, rule_version)
    return added


__all__ = [
    "CategoricalNormalizer",
    "NormalizedCategory",
    "ORDINAL_RANK",
    "PROCESS_STATES",
    "SEED_VALUE_MAPPINGS",
    "parse_count_range",
    "seed_value_mappings",
]
