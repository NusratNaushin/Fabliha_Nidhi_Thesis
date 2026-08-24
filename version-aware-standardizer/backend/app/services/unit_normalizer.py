"""Turning the unit a lab typed into a unit a computer can compare.

The same unit arrives spelled a dozen ways -- ``mg/dl``, ``mg/dL``, ``MG/DL``,
``mg per dL`` -- and none of them can be compared with another system's until
they agree on one machine-readable form. That form is UCUM.

The distinction this module exists to protect is between two operations that
look similar and are not:

**Normalising a spelling** turns ``mg/dl`` into ``mg/dL``. The number does not
move. This is safe to do in bulk from a lookup table.

**Converting a unit** turns ``mg/dL`` into ``mmol/L``. The number *must* move,
and by how much depends on the substance being measured: glucose and creatinine
have different molar masses, so a blanket "all mg/dL become mmol/L" rule would
silently corrupt every creatinine in the dataset. A conversion therefore
requires a rule that names the LOINC code it applies to, and that a person has
approved. Without one we keep the original number and say so.

A further trap, and the reason unit rules are keyed on the test rather than the
unit alone: LOINC's ``EXAMPLE_UNITS`` and ``EXAMPLE_UCUM_UNITS`` are
*examples*. They are not a list of permitted units, so a unit cannot be
validated -- or guessed -- from the LOINC code by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.constants import ResultIssue, UnitStatus
from backend.app.models import UnitMappingRule
from backend.app.utils.logging import get_logger

log = get_logger("unit")

UCUM_SYSTEM = "http://unitsofmeasure.org"

# Spelling variants observed in real laboratory extracts, mapped to their UCUM
# form. Every entry here leaves the number alone -- these are the same unit
# written differently, not a different unit.
#
# Where UCUM's form is genuinely unobvious it is noted, because "mEq/L is meq/L"
# and "K/uL is 10*3/uL" are the kind of thing a reader should not have to take
# on trust.
SEED_UNIT_SPELLINGS: dict[str, str] = {
    # mass / volume
    "mg/dl": "mg/dL", "mg/dL": "mg/dL", "MG/DL": "mg/dL", "mg per dl": "mg/dL",
    "mg/l": "mg/L", "mg/L": "mg/L",
    "g/dl": "g/dL", "g/dL": "g/dL", "G/DL": "g/dL",
    "ug/dl": "ug/dL", "ug/dL": "ug/dL", "mcg/dl": "ug/dL",
    "ng/ml": "ng/mL", "ng/mL": "ng/mL", "NG/ML": "ng/mL",
    "pg/ml": "pg/mL", "pg/mL": "pg/mL",
    "ug/ml": "ug/mL", "ug/mL": "ug/mL",
    "g/l": "g/L", "g/L": "g/L",
    # amount / volume
    "mmol/l": "mmol/L", "mmol/L": "mmol/L", "MMOL/L": "mmol/L",
    "umol/l": "umol/L", "umol/L": "umol/L",
    "nmol/l": "nmol/L", "nmol/L": "nmol/L",
    # equivalents -- UCUM spells the unit "eq", so milliequivalents are "meq"
    "meq/l": "meq/L", "mEq/L": "meq/L", "MEQ/L": "meq/L", "mEq/l": "meq/L",
    # osmolality -- UCUM "osm", so milliosmoles are "mosm"
    "mosm/kg": "mosm/kg", "mOsm/kg": "mosm/kg", "MOSM/KG": "mosm/kg",
    # enzyme and international units -- UCUM brackets the arbitrary unit
    "iu/l": "[IU]/L", "IU/L": "[IU]/L", "IU/mL": "[IU]/mL", "iu/ml": "[IU]/mL",
    "u/l": "U/L", "U/L": "U/L", "units/l": "U/L",
    # counts per volume. "K/uL" means thousands per microlitre, which UCUM
    # writes as a power of ten; "m/uL" means millions.
    "k/ul": "10*3/uL", "K/uL": "10*3/uL", "K/UL": "10*3/uL", "k/uL": "10*3/uL",
    "m/ul": "10*6/uL", "m/uL": "10*6/uL", "M/uL": "10*6/uL",
    "#/ul": "/uL", "#/uL": "/uL",
    "cells/ul": "/uL", "/ul": "/uL", "/uL": "/uL",
    # fractions
    "%": "%", "percent": "%", "PERCENT": "%",
    # pressure -- UCUM brackets the mercury
    "mmhg": "mm[Hg]", "mm Hg": "mm[Hg]", "mmHg": "mm[Hg]", "MMHG": "mm[Hg]",
    # time
    "sec": "s", "SEC": "s", "seconds": "s", "s": "s",
    "min": "min", "hours": "h", "hr": "h",
    # small volumes and masses
    "fl": "fL", "fL": "fL", "FL": "fL",
    "pg": "pg", "PG": "pg",
    "ml": "mL", "mL": "mL",
    "l": "L", "L": "L",
    # ratios and indices
    "ratio": "1", "index": "1", "Ratio": "1",
    # microscopy: per high- and low-power field
    "#/hpf": "/[HPF]", "#/HPF": "/[HPF]", "/hpf": "/[HPF]",
    "#/lpf": "/[LPF]", "#/LPF": "/[LPF]", "/lpf": "/[LPF]",
    # sub-multiples of the international unit
    "uiu/ml": "u[IU]/mL", "uIU/mL": "u[IU]/mL",
    "miu/ml": "m[IU]/mL", "mIU/mL": "m[IU]/mL",
    "u/ml": "U/mL", "U/mL": "U/mL", "uU/ML": "uU/mL", "uu/ml": "uU/mL",
    "ng/dl": "ng/dL", "ng/dL": "ng/dL",
    # flow, sedimentation rate, mass ratio
    "l/min": "L/min", "L/min": "L/min",
    "mm/hr": "mm/h", "MM/HR": "mm/h",
    "mg/g": "mg/g", "MG/G": "mg/g",
}

# Strings that turn up in the unit column but are not units -- they are result
# vocabularies that leaked across. Normalising them as units would attach a
# meaningless UCUM code to a categorical result, so they are named and refused.
NOT_A_UNIT: frozenset[str] = frozenset({
    "+/-", "pos/neg", "positive/negative", "neg/pos", "n/a", "na", "none", "-",
})

# Units observed in real data whose UCUM form is genuinely not determinate.
# Left unmapped on purpose: "units" could be international, enzyme or arbitrary
# units, and Ehrlich units have no UCUM representation at all. Guessing would
# put a wrong code on a real measurement, so these go to review instead.
AMBIGUOUS_UNITS: dict[str, str] = {
    "units": "\"units\" does not say which kind -- international, enzyme or arbitrary.",
    "unit": "\"unit\" does not say which kind -- international, enzyme or arbitrary.",
    "eu/dl": "Ehrlich units have no UCUM representation.",
    "u/g/hb": "A composite unit (per gram of haemoglobin) that needs a curated rule.",
}

# Characters UCUM permits in an expression. Not a full grammar -- a full UCUM
# parser is a project of its own -- but enough to reject obvious rubbish while
# being honest that it is a structural check, not semantic validation.
_UCUM_SHAPE = re.compile(r"^[A-Za-z0-9\[\]{}/.*+\-^%_]+$")

# Which physical dimension a UCUM code belongs to. Used only to catch a unit
# that cannot possibly belong to a measurement -- never to decide that a unit is
# correct, which LOINC itself does not let us do.
_DIMENSION: dict[str, str] = {
    "mg/dL": "mass_concentration", "mg/L": "mass_concentration",
    "g/dL": "mass_concentration", "g/L": "mass_concentration",
    "ug/dL": "mass_concentration", "ug/mL": "mass_concentration",
    "ng/mL": "mass_concentration", "pg/mL": "mass_concentration",
    "ng/dL": "mass_concentration",
    "mmol/L": "substance_concentration", "umol/L": "substance_concentration",
    "nmol/L": "substance_concentration",
    "meq/L": "equivalent_concentration",
    "mosm/kg": "osmolality",
    "[IU]/L": "catalytic_activity", "[IU]/mL": "catalytic_activity",
    "U/L": "catalytic_activity", "U/mL": "catalytic_activity",
    "u[IU]/mL": "catalytic_activity", "m[IU]/mL": "catalytic_activity",
    "uU/mL": "catalytic_activity",
    "10*3/uL": "number_concentration", "10*6/uL": "number_concentration",
    "/uL": "number_concentration", "/[HPF]": "number_concentration",
    "/[LPF]": "number_concentration",
    "%": "fraction", "1": "fraction", "mg/g": "fraction",
    "mm[Hg]": "pressure",
    "s": "time", "min": "time", "h": "time",
    "fL": "entity_volume", "mL": "volume", "L": "volume",
    "pg": "entity_mass",
    "L/min": "flow",
    "mm/h": "linear_rate",
}

# Dimensions that legitimately stand in for one another on the same test.
#
# This grouping is not a theoretical nicety -- it is what the data demanded.
# Sodium is a substance concentration reported in mEq/L on 13,851 rows and
# glucose is one reported in mg/dL on thousands more. Treating those as errors
# would have flagged most of the dataset and taught everyone to ignore the
# warning. Within a family the difference is a conversion factor, which is a
# note; across families it is a real mistake, which is a quarantine.
_DIMENSION_FAMILY: dict[str, str] = {
    "mass_concentration": "analyte_amount",
    "substance_concentration": "analyte_amount",
    "equivalent_concentration": "analyte_amount",
    "osmolality": "analyte_amount",
    "catalytic_activity": "analyte_amount",
    "number_concentration": "analyte_amount",
    "fraction": "analyte_amount",
    "pressure": "pressure",
    "time": "time",
    "entity_volume": "entity_property",
    "entity_mass": "entity_property",
    "volume": "volume",
    "flow": "flow",
    "linear_rate": "linear_rate",
}

# LOINC PROPERTY -> the dimension a result of that property should carry.
# Only the unambiguous ones are listed; anything absent is simply not checked,
# which is the honest position rather than inventing an expectation.
_PROPERTY_DIMENSION: dict[str, str] = {
    "MCnc": "mass_concentration",
    "SCnc": "substance_concentration",
    "ECnc": "equivalent_concentration",
    "Osmol": "osmolality",
    "CCnc": "catalytic_activity", "LsCnc": "catalytic_activity",
    "ACnc": "catalytic_activity",
    "NCnc": "number_concentration",
    "MFr": "fraction", "NFr": "fraction", "VFr": "fraction", "SFr": "fraction",
    "CFr": "fraction", "EntMCnc": "fraction", "DistWidth": "fraction",
    "Pres": "pressure", "PPres": "pressure",
    "Time": "time",
    "EntVol": "entity_volume", "EntMeanVol": "entity_volume",
    "EntMass": "entity_mass",
    "VRat": "flow",
}


@dataclass
class NormalizedUnit:
    """What happened to one raw unit string."""

    status: UnitStatus
    ucum_code: str | None = None
    display_unit: str | None = None
    numeric_value: float | None = None
    value_changed: bool = False
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
            "ucum_code": self.ucum_code,
            "display_unit": self.display_unit,
            "numeric_value": self.numeric_value,
            "value_changed": self.value_changed,
            "rule_id": self.rule_id,
            "issues": [i.value for i in self.issues],
            "notes": self.notes,
        }


def looks_like_ucum(code: str | None) -> bool:
    """A structural check, not a semantic one.

    It rejects strings that cannot be UCUM; it does not certify that what
    remains is meaningful. Saying so plainly is better than implying a
    validation this project does not perform.
    """
    if not code or not code.strip():
        return False
    return bool(_UCUM_SHAPE.match(code.strip()))


def dimension_of(ucum_code: str | None) -> str | None:
    return _DIMENSION.get(ucum_code) if ucum_code else None


def family_of(dimension: str | None) -> str | None:
    return _DIMENSION_FAMILY.get(dimension) if dimension else None


def is_not_a_unit(raw_unit: str | None) -> bool:
    """True for strings in the unit column that are actually result values."""
    return bool(raw_unit) and raw_unit.strip().lower() in NOT_A_UNIT


def ambiguity_of(raw_unit: str | None) -> str | None:
    """Why a real unit still cannot be given a UCUM code, if that is the case."""
    return AMBIGUOUS_UNITS.get(raw_unit.strip().lower()) if raw_unit else None


def expected_dimension(loinc_property: str | None) -> str | None:
    return _PROPERTY_DIMENSION.get(loinc_property.strip()) if loinc_property else None


class UnitNormalizer:
    """Applies the unit rule table, with the seed spellings as a fallback."""

    def __init__(self, session: Session | None = None, *, use_seed: bool = True) -> None:
        self.session = session
        self.use_seed = use_seed
        self._rules: dict[tuple[str, str | None], UnitMappingRule] | None = None

    # -- rule access -------------------------------------------------------
    def _load_rules(self) -> dict[tuple[str, str | None], UnitMappingRule]:
        if self._rules is not None:
            return self._rules
        rules: dict[tuple[str, str | None], UnitMappingRule] = {}
        if self.session is not None:
            for rule in self.session.scalars(
                select(UnitMappingRule).where(UnitMappingRule.active.is_(True))
            ):
                rules[(rule.source_unit.strip().lower(), rule.loinc_code)] = rule
        self._rules = rules
        return rules

    def find_rule(self, raw_unit: str, loinc_code: str | None) -> UnitMappingRule | None:
        """The most specific active rule: test-specific first, then general."""
        rules = self._load_rules()
        key = raw_unit.strip().lower()
        if loinc_code and (key, loinc_code) in rules:
            return rules[(key, loinc_code)]
        return rules.get((key, None))

    # -- the operation -----------------------------------------------------
    def normalize(
        self,
        raw_unit: str | None,
        numeric_value: float | None = None,
        *,
        loinc_code: str | None = None,
        loinc_property: str | None = None,
    ) -> NormalizedUnit:
        """Normalise a unit, and convert the number only when told to.

        Note what this deliberately does *not* do: when the unit is unknown, it
        does not fall back to the LOINC example unit, and it does not leave the
        field blank as though no unit were given. It reports the unit as unknown
        and keeps the original string, because a missing unit and an
        unrecognised unit are different problems.
        """
        text = (raw_unit or "").strip()

        if is_not_a_unit(text):
            out = NormalizedUnit(
                status=UnitStatus.REVIEW_REQUIRED,
                display_unit=text,
                numeric_value=numeric_value,
            )
            out.add(
                ResultIssue.UNIT_UNKNOWN,
                f"{text!r} is not a unit -- it is a result vocabulary that has ended up in "
                f"the unit column. No UCUM code is attached and the value is untouched.",
            )
            return out

        ambiguity = ambiguity_of(text)
        if ambiguity:
            out = NormalizedUnit(
                status=UnitStatus.REVIEW_REQUIRED,
                display_unit=text,
                numeric_value=numeric_value,
            )
            out.add(
                ResultIssue.UNIT_UNKNOWN,
                f"{text!r} is a real unit but has no determinate UCUM form: {ambiguity} "
                f"A curated rule is needed; nothing is guessed.",
            )
            return out

        if not text:
            out = NormalizedUnit(status=UnitStatus.MISSING, numeric_value=numeric_value)
            out.add(
                ResultIssue.UNIT_MISSING,
                "No unit was recorded. It is left empty rather than guessed from the "
                "LOINC code, whose example units are examples and not a permitted list.",
            )
            return out

        rule = self.find_rule(text, loinc_code)

        # 1. a curated rule wins
        if rule is not None:
            ucum = rule.normalized_ucum_code
            out = NormalizedUnit(
                status=UnitStatus.NORMALIZED,
                ucum_code=ucum,
                display_unit=rule.display_unit or ucum,
                numeric_value=numeric_value,
                rule_id=rule.id,
            )
            if rule.changes_the_number:
                if numeric_value is None:
                    out.add(
                        ResultIssue.UNIT_CONVERSION_NOT_AVAILABLE,
                        "A conversion rule applies but there is no number to convert.",
                    )
                else:
                    converted = numeric_value * rule.conversion_factor + rule.conversion_offset
                    if rule.precision is not None:
                        converted = round(converted, rule.precision)
                    out.numeric_value = converted
                    out.value_changed = True
                    out.status = UnitStatus.CONVERTED
                    out.add(
                        note=f"Converted {numeric_value} {text} to {converted} {ucum} using "
                             f"approved rule {rule.id} (x{rule.conversion_factor}"
                             f"{'' if not rule.conversion_offset else f' {rule.conversion_offset:+}'}).",
                    )
            elif ucum != text:
                out.add(note=f"Spelling normalised from {text!r} to {ucum!r}; the number is unchanged.")
            else:
                out.status = UnitStatus.VALID
            self._check_dimension(out, loinc_property, text)
            return out

        # 2. the seed spelling table
        if self.use_seed:
            ucum = SEED_UNIT_SPELLINGS.get(text) or SEED_UNIT_SPELLINGS.get(text.lower())
            if ucum:
                out = NormalizedUnit(
                    status=UnitStatus.VALID if ucum == text else UnitStatus.NORMALIZED,
                    ucum_code=ucum,
                    display_unit=text,
                    numeric_value=numeric_value,
                )
                if ucum != text:
                    out.add(note=f"Spelling normalised from {text!r} to {ucum!r}; the number is unchanged.")
                self._check_dimension(out, loinc_property, text)
                return out

        # 3. we have never seen it
        out = NormalizedUnit(
            status=UnitStatus.UNKNOWN,
            ucum_code=None,
            display_unit=text,
            numeric_value=numeric_value,
        )
        out.add(
            ResultIssue.UNIT_UNKNOWN,
            f"No rule for unit {text!r}. The value and the original unit are kept "
            f"unchanged; nothing is converted on a guess.",
        )
        return out

    def _check_dimension(
        self, out: NormalizedUnit, loinc_property: str | None, raw_unit: str
    ) -> None:
        """Flag a unit that cannot belong to this kind of measurement.

        Only a *cross-family* mismatch is treated as an error -- a time unit on
        a concentration test, say. Within the analyte-amount family the pairing
        is normal practice (sodium in mEq/L, glucose in mg/dL, both substance
        concentrations) and produces a note about the conversion that would be
        needed, not a warning.

        Nothing here ever deletes or corrects a value. The worst it does is send
        the row for review, which is the only safe response to "this looks
        impossible".
        """
        want = expected_dimension(loinc_property)
        got = dimension_of(out.ucum_code)
        if not (want and got) or want == got:
            return

        if family_of(want) == family_of(got):
            out.add(
                note=f"LOINC property {loinc_property!r} implies a "
                     f"{want.replace('_', ' ')} while {raw_unit!r} is a "
                     f"{got.replace('_', ' ')}. Both measure the amount of an analyte, so "
                     f"this is normal reporting practice; a conversion factor would be "
                     f"needed to compare across the two, and none is applied here.",
            )
            return

        out.status = UnitStatus.INCOMPATIBLE
        out.add(
            ResultIssue.UNIT_INCOMPATIBLE,
            f"LOINC property {loinc_property!r} implies a {want.replace('_', ' ')}, "
            f"but {raw_unit!r} is a {got.replace('_', ' ')}. Those are not the same kind "
            f"of quantity. The value is kept and quarantined for review, not corrected.",
        )


def seed_unit_rules(session: Session, *, rule_version: str = "seed-1") -> int:
    """Load the spelling table into the database as reviewable rules.

    They arrive marked ``SEEDED`` rather than approved: every one is a claim
    that two strings mean the same unit, and a person should be able to see and
    contest them. None of them changes a number, which is what makes seeding
    them safe in the first place.
    """
    existing = {
        (r.source_unit, r.loinc_code)
        for r in session.scalars(
            select(UnitMappingRule).where(UnitMappingRule.rule_version == rule_version)
        )
    }
    added = 0
    for source_unit, ucum in SEED_UNIT_SPELLINGS.items():
        if (source_unit, None) in existing:
            continue
        session.add(
            UnitMappingRule(
                source_unit=source_unit,
                normalized_ucum_code=ucum,
                display_unit=source_unit,
                loinc_code=None,
                conversion_factor=1.0,
                conversion_offset=0.0,
                rule_version=rule_version,
                review_status="SEEDED",
                clinical_reference="Spelling normalisation only; the numeric value is unchanged.",
                active=True,
            )
        )
        added += 1
    session.flush()
    log.info("seeded %d unit spelling rules (version %s)", added, rule_version)
    return added


__all__ = [
    "AMBIGUOUS_UNITS",
    "NOT_A_UNIT",
    "NormalizedUnit",
    "SEED_UNIT_SPELLINGS",
    "ambiguity_of",
    "family_of",
    "is_not_a_unit",
    "UCUM_SYSTEM",
    "UnitNormalizer",
    "dimension_of",
    "expected_dimension",
    "looks_like_ucum",
    "seed_unit_rules",
]
