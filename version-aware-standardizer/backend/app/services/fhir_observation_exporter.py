"""Writing standardized results as FHIR R4 Observations.

The relational table is what this project reasons over; FHIR is how another
system receives it. Both are produced, because a CSV is easier to inspect and a
FHIR resource is easier to exchange, and neither is a substitute for the other.

Three choices worth explaining, because each looks like a shortcut and is not:

**``status`` is ``unknown``.** FHIR requires Observation.status, and MIMIC's
LABEVENTS does not record whether a result was preliminary, final or corrected.
Writing ``final`` would be asserting something the source never said, so the
resource says it does not know -- which is exactly what ``unknown`` is for.

**A censored result keeps its comparator.** ``<2.0 mmol/L`` becomes
``valueQuantity`` with ``value: 2.0`` and ``comparator: "<"``. Dropping the sign
would turn a below-detection-limit reading into a measurement.

**A missing value becomes ``dataAbsentReason``, not a value of zero.** The
resource records that nothing was measured rather than pretending something was.

The subject reference is a pseudonym. Nothing here can be traced to a patient
without the HMAC key, which never leaves the environment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.constants import (
    INTERPRETATION_ABNORMAL,
    INTERPRETATION_ABNORMAL_DISPLAY,
    ValueType,
)
from backend.app.models import StandardizedLabObservation, StandardizationRun
from backend.app.services.unit_normalizer import UCUM_SYSTEM
from backend.app.utils.logging import get_logger

log = get_logger("fhir")

LOINC_SYSTEM = "http://loinc.org"
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DATA_ABSENT_SYSTEM = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
LOCAL_ITEM_SYSTEM = "urn:mimic:itemid"

# FHIR dateTime is ISO 8601: the date and the time are joined by "T", not a
# space. MIMIC exports "2164-09-24 20:21:00", which looks close enough to pass a
# glance and is not a valid FHIR dateTime -- a consumer would reject the whole
# resource.
_SPACED_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2}(?::\d{2})?)")
_FHIR_DATETIME = re.compile(
    r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"(Z|[+-]\d{2}:\d{2})?)?)?)?$"
)


def to_fhir_datetime(value: str | None) -> str | None:
    """Coerce a source timestamp into a FHIR dateTime, or leave it alone.

    Only the separator is changed. The instant is never shifted and no timezone
    is invented: MIMIC's times carry no offset, and asserting one would be
    inventing information about when something happened.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _SPACED_DATETIME.match(text)
    if match:
        return f"{match.group(1)}T{match.group(2)}"
    return text


def _codeable_concept(observation: StandardizedLabObservation) -> dict:
    """Observation.code: the approved LOINC code plus the source's own identifier.

    Both are carried on purpose. The LOINC coding is what another system reads;
    the local itemid is what lets somebody trace a resource back to the row it
    came from. Where no approved code exists the local identifier stands alone,
    with the test name as text -- which is honest, and still useful.
    """
    codings: list[dict] = []
    if observation.approved_current_loinc:
        codings.append({
            "system": LOINC_SYSTEM,
            "code": observation.approved_current_loinc,
            **({"version": observation.current_loinc_version}
               if observation.current_loinc_version else {}),
        })
    codings.append({
        "system": LOCAL_ITEM_SYSTEM,
        "code": observation.itemid,
        **({"display": observation.source_label} if observation.source_label else {}),
    })
    concept: dict = {"coding": codings}
    if observation.source_label:
        concept["text"] = observation.source_label
    return concept


def _value(observation: StandardizedLabObservation) -> dict:
    """Whichever value[x] fits, or dataAbsentReason when there is nothing."""
    kind = observation.value_type

    if kind == ValueType.QUANTITY.value and observation.standard_numeric_value is not None:
        quantity: dict = {"value": observation.standard_numeric_value}
        if observation.comparator:
            quantity["comparator"] = observation.comparator
        if observation.standard_ucum_unit:
            quantity["unit"] = observation.standard_ucum_unit
            quantity["system"] = UCUM_SYSTEM
            quantity["code"] = observation.standard_ucum_unit
        elif observation.raw_unit:
            # A unit we could not turn into UCUM is still shown to a reader, but
            # without a system or code -- claiming it were UCUM would be false.
            quantity["unit"] = observation.raw_unit
        return {"valueQuantity": quantity}

    if kind == ValueType.CODEABLE_CONCEPT.value:
        concept: dict = {}
        if observation.coded_value_code and observation.coded_value_system:
            concept["coding"] = [{
                "system": observation.coded_value_system,
                "code": observation.coded_value_code,
                **({"display": observation.coded_value_display}
                   if observation.coded_value_display else {}),
            }]
        # With no licensed code system the normalised wording travels as text.
        # FHIR allows exactly this, and it is better than a fabricated code.
        text = observation.normalized_text_value or observation.raw_value
        if text:
            concept["text"] = text
        return {"valueCodeableConcept": concept} if concept else _absent(observation)

    if kind == ValueType.STRING.value:
        text = observation.normalized_text_value or observation.raw_value
        return {"valueString": text} if text else _absent(observation)

    return _absent(observation)


def _absent(observation: StandardizedLabObservation) -> dict:
    return {
        "dataAbsentReason": {
            "coding": [{
                "system": DATA_ABSENT_SYSTEM,
                "code": observation.data_absent_reason or "unknown",
            }]
        }
    }


def to_observation(observation: StandardizedLabObservation) -> dict:
    """One standardized row as a FHIR R4 Observation."""
    resource: dict = {
        "resourceType": "Observation",
        "id": f"{observation.source_dataset.lower()}-{observation.source_row_id}",
        # Not "final": MIMIC does not record result status, and inventing one
        # would assert something the source never said.
        "status": "unknown",
        "category": [{
            "coding": [{"system": OBSERVATION_CATEGORY_SYSTEM, "code": "laboratory"}]
        }],
        "code": _codeable_concept(observation),
    }

    if observation.subject_key:
        resource["subject"] = {"reference": f"Patient/{observation.subject_key}"}
    if observation.encounter_key:
        resource["encounter"] = {"reference": f"Encounter/{observation.encounter_key}"}
    effective = to_fhir_datetime(observation.charttime)
    if effective:
        resource["effectiveDateTime"] = effective

    resource.update(_value(observation))

    if observation.interpretation_code == INTERPRETATION_ABNORMAL:
        resource["interpretation"] = [{
            "coding": [{
                "system": INTERPRETATION_SYSTEM,
                "code": INTERPRETATION_ABNORMAL,
                "display": INTERPRETATION_ABNORMAL_DISPLAY,
            }]
        }]

    if observation.source_fluid:
        # No SNOMED specimen code without a licence, so the specimen travels as
        # text on the resource rather than as a coded Specimen reference.
        resource["specimen"] = {"display": observation.source_fluid}

    # Provenance: which releases and rules produced this. A resource that cannot
    # say what it was derived from cannot be checked later.
    meta_tags = [{"system": "urn:vas:quality", "code": observation.quality_status}]
    if observation.current_loinc_version:
        meta_tags.append({
            "system": "urn:vas:loinc-version", "code": observation.current_loinc_version
        })
    if observation.resolver_decision:
        meta_tags.append({
            "system": "urn:vas:resolver-decision", "code": observation.resolver_decision
        })
    resource["meta"] = {
        "source": f"urn:vas:standardization-run:{observation.standardization_run_id}",
        "tag": meta_tags,
    }
    return resource


def iter_observations(
    session: Session, run_id: int, *, include_quarantined: bool = False
) -> Iterator[dict]:
    """Every standardized row of a run, as FHIR resources.

    Quarantined rows are excluded by default: they are kept in the database, but
    exporting a resource we have said is not fit to use would defeat the point
    of quarantining it.
    """
    stmt = select(StandardizedLabObservation).where(
        StandardizedLabObservation.standardization_run_id == run_id
    )
    if not include_quarantined:
        stmt = stmt.where(StandardizedLabObservation.quality_status != "QUARANTINED")
    for observation in session.scalars(stmt.order_by(StandardizedLabObservation.id)):
        yield to_observation(observation)


def export_ndjson(
    session: Session,
    run: StandardizationRun,
    path: Path,
    *,
    include_quarantined: bool = False,
) -> int:
    """Write one Observation per line. NDJSON is what FHIR bulk export uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        for resource in iter_observations(
            session, run.id, include_quarantined=include_quarantined
        ):
            fh.write(json.dumps(resource, separators=(",", ":")))
            fh.write("\n")
            written += 1
    log.info("wrote %d FHIR Observations to %s", written, path)
    return written


def validate_observation(resource: dict) -> list[str]:
    """Structural checks against the parts of the R4 spec we rely on.

    Not a full FHIR validator -- that needs the published profiles and a
    validator to run them. It checks the invariants this exporter is responsible
    for, and says plainly that it is doing no more than that.
    """
    problems: list[str] = []

    if resource.get("resourceType") != "Observation":
        problems.append("resourceType must be Observation")
    if not resource.get("status"):
        problems.append("status is mandatory in FHIR R4")
    elif resource["status"] not in {
        "registered", "preliminary", "final", "amended", "corrected",
        "cancelled", "entered-in-error", "unknown",
    }:
        problems.append(f"status {resource['status']!r} is not an allowed code")

    code = resource.get("code") or {}
    if not code.get("coding") and not code.get("text"):
        problems.append("code must carry at least one coding or a text")

    value_keys = [k for k in resource if k.startswith("value")]
    if len(value_keys) > 1:
        problems.append(f"only one value[x] is allowed, found {value_keys}")
    if not value_keys and "dataAbsentReason" not in resource:
        problems.append("a value[x] or dataAbsentReason is required")
    if value_keys and "dataAbsentReason" in resource:
        problems.append("value[x] and dataAbsentReason must not both be present")

    effective = resource.get("effectiveDateTime")
    if effective is not None and not _FHIR_DATETIME.match(effective):
        problems.append(
            f"effectiveDateTime {effective!r} is not a FHIR dateTime "
            f"(ISO 8601, with 'T' between the date and the time)"
        )

    quantity = resource.get("valueQuantity")
    if quantity is not None:
        if "value" not in quantity:
            problems.append("valueQuantity must carry a value")
        if quantity.get("system") and quantity["system"] != UCUM_SYSTEM:
            problems.append("valueQuantity.system should be UCUM")
        if quantity.get("system") and not quantity.get("code"):
            problems.append("valueQuantity with a system must carry a code")
        if quantity.get("comparator") not in (None, "<", "<=", ">", ">="):
            problems.append(f"comparator {quantity.get('comparator')!r} is not allowed")

    return problems


__all__ = [
    "LOINC_SYSTEM",
    "to_fhir_datetime",
    "export_ndjson",
    "iter_observations",
    "to_observation",
    "validate_observation",
]
