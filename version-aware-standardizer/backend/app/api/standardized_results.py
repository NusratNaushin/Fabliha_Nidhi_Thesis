"""Endpoints for the result-standardization layer.

The console reads these. They are shaped for a person looking at one screen --
a run and its headline numbers, a page of results, the issues grouped by kind --
rather than for a bulk consumer, which is what the NDJSON export is for.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.models import (
    SourceLabItem,
    SourceLabResult,
    StandardizationIssue,
    StandardizationRun,
    StandardizedLabObservation,
)
from backend.app.services.fhir_observation_exporter import (
    to_observation,
    validate_observation,
)

router = APIRouter(prefix="/api/v1/standardization", tags=["standardization"])


def _run_out(run: StandardizationRun) -> dict:
    return {
        "id": run.id,
        "source_dataset": run.source_dataset,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "loinc_version": run.loinc_version,
        "snomed_version": run.snomed_version,
        "unit_rule_version": run.unit_rule_version,
        "value_rule_version": run.value_rule_version,
        "input_rows": run.input_rows,
        "standardized_rows": run.standardized_rows,
        "quarantined_rows": run.quarantined_rows,
        "rows_accounted_for": run.rows_accounted_for,
        "status": run.status,
        "summary": run.summary_json,
        "manifest": run.manifest_json,
        "error_message": run.error_message,
    }


def _observation_out(o: StandardizedLabObservation) -> dict:
    return {
        "id": o.id,
        "source_row_id": o.source_row_id,
        "subject_key": o.subject_key,
        "itemid": o.itemid,
        "charttime": o.charttime,
        "source_label": o.source_label,
        "source_fluid": o.source_fluid,
        "source_category": o.source_category,
        "original_loinc_code": o.original_loinc_code,
        "resolver_decision": o.resolver_decision,
        "engine_suggested_loinc": o.engine_suggested_loinc,
        "approved_current_loinc": o.approved_current_loinc,
        "current_loinc_version": o.current_loinc_version,
        "loinc_scale": o.loinc_scale,
        "loinc_property": o.loinc_property,
        "raw_value": o.raw_value,
        "raw_unit": o.raw_unit,
        "raw_flag": o.raw_flag,
        "value_type": o.value_type,
        "comparator": o.comparator,
        "standard_numeric_value": o.standard_numeric_value,
        "standard_ucum_unit": o.standard_ucum_unit,
        "unit_status": o.unit_status,
        "normalized_text_value": o.normalized_text_value,
        "coded_value_code": o.coded_value_code,
        "value_mapping_status": o.value_mapping_status,
        "interpretation_code": o.interpretation_code,
        "data_absent_reason": o.data_absent_reason,
        "quality_status": o.quality_status,
        "issues": o.issues_json or [],
    }


def _get_run(session: Session, run_id: int | None) -> StandardizationRun:
    if run_id is None:
        run = session.scalars(
            select(StandardizationRun).order_by(StandardizationRun.id.desc()).limit(1)
        ).first()
    else:
        run = session.get(StandardizationRun, run_id)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No standardization run found. Import raw results and run "
            "scripts/standardize_mimic_results.py first.",
        )
    return run


@router.get("/runs", summary="Every standardization run")
def list_runs(
    limit: int = Query(default=25, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    runs = session.scalars(
        select(StandardizationRun).order_by(StandardizationRun.id.desc()).limit(limit)
    )
    return [_run_out(r) for r in runs]


@router.get("/runs/latest", summary="The most recent run, with its headline numbers")
def latest_run(session: Session = Depends(get_session)) -> dict:
    return _run_out(_get_run(session, None))


@router.get("/runs/{run_id}", summary="One run")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    return _run_out(_get_run(session, run_id))


@router.get("/runs/{run_id}/results", summary="A page of standardized results")
def list_results(
    run_id: int,
    quality: str | None = Query(default=None, description="OK, WARNING or QUARANTINED"),
    value_type: str | None = Query(default=None, description="QUANTITY, CODEABLE_CONCEPT, STRING, ABSENT"),
    issue: str | None = Query(default=None, description="only rows carrying this issue code"),
    search: str | None = Query(default=None, description="match the test name or a code"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    run = _get_run(session, run_id)
    stmt = select(StandardizedLabObservation).where(
        StandardizedLabObservation.standardization_run_id == run.id
    )
    if quality:
        stmt = stmt.where(StandardizedLabObservation.quality_status == quality.upper())
    if value_type:
        stmt = stmt.where(StandardizedLabObservation.value_type == value_type.upper())
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            StandardizedLabObservation.source_label.ilike(like)
            | StandardizedLabObservation.original_loinc_code.ilike(like)
            | StandardizedLabObservation.approved_current_loinc.ilike(like)
            | StandardizedLabObservation.itemid.ilike(like)
        )

    total = session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    rows = session.scalars(
        stmt.order_by(StandardizedLabObservation.id).limit(limit).offset(offset)
    ).all()

    # The issue filter is applied in Python: issues live in a JSON array, and a
    # portable JSON query across SQLite and PostgreSQL is not worth the
    # complexity for a filter used on one screen.
    if issue:
        wanted = issue.upper()
        rows = [r for r in rows if wanted in (r.issues_json or [])]

    return {
        "run_id": run.id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
        "results": [_observation_out(r) for r in rows],
    }


@router.get("/runs/{run_id}/issues", summary="Issues grouped by kind")
def list_issues(
    run_id: int,
    limit_examples: int = Query(default=3, ge=0, le=20),
    session: Session = Depends(get_session),
) -> dict:
    run = _get_run(session, run_id)
    counts = session.execute(
        select(
            StandardizationIssue.issue_code,
            StandardizationIssue.severity,
            func.count().label("n"),
        )
        .where(StandardizationIssue.standardization_run_id == run.id)
        .group_by(StandardizationIssue.issue_code, StandardizationIssue.severity)
        .order_by(func.count().desc())
    ).all()

    groups = []
    for code, severity, n in counts:
        examples = session.scalars(
            select(StandardizationIssue)
            .where(
                StandardizationIssue.standardization_run_id == run.id,
                StandardizationIssue.issue_code == code,
            )
            .limit(limit_examples)
        ).all() if limit_examples else []
        groups.append({
            "issue_code": code,
            "severity": severity,
            "rows": n,
            "share": round(n / run.input_rows, 4) if run.input_rows else None,
            "examples": [
                {
                    "source_row_id": e.source_row_id,
                    "itemid": e.itemid,
                    "detail": e.detail,
                }
                for e in examples
            ],
        })
    return {"run_id": run.id, "input_rows": run.input_rows, "issues": groups}


@router.get("/results/{observation_id}/fhir", summary="One result as a FHIR Observation")
def observation_as_fhir(
    observation_id: int, session: Session = Depends(get_session)
) -> dict:
    observation = session.get(StandardizedLabObservation, observation_id)
    if observation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such standardized result.")
    resource = to_observation(observation)
    return {"resource": resource, "validation_problems": validate_observation(resource)}


@router.get("/unmapped", summary="Tests that carry no LOINC code at all")
def unmapped_items(
    dataset: str = Query(default="MIMIC_III"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> dict:
    """The review queue for coding, as opposed to the queue for re-coding.

    These tests were never mapped, so there is nothing to re-check -- somebody
    has to choose a code. The observed units and example values are included
    because that is what a person needs in order to choose one.
    """
    items = session.scalars(
        select(SourceLabItem)
        .where(
            SourceLabItem.source_dataset == dataset,
            SourceLabItem.original_loinc_code.is_(None),
        )
        .order_by(SourceLabItem.itemid)
        .limit(limit)
    ).all()

    out = []
    for item in items:
        rows = session.scalars(
            select(SourceLabResult)
            .where(
                SourceLabResult.source_dataset == dataset,
                SourceLabResult.itemid == item.itemid,
            )
            .limit(200)
        ).all()
        total = session.scalar(
            select(func.count()).select_from(SourceLabResult).where(
                SourceLabResult.source_dataset == dataset,
                SourceLabResult.itemid == item.itemid,
            )
        ) or 0
        units: dict[str, int] = {}
        for r in rows:
            if r.raw_unit:
                units[r.raw_unit] = units.get(r.raw_unit, 0) + 1
        out.append({
            "itemid": item.itemid,
            "label": item.label,
            "fluid": item.fluid,
            "category": item.category,
            "result_count": total,
            "observed_units": sorted(units.items(), key=lambda kv: -kv[1])[:4],
            "examples": [r.raw_value for r in rows if r.raw_value][:5],
        })
    out.sort(key=lambda i: -i["result_count"])
    return {"dataset": dataset, "count": len(out), "items": out}


@router.get("/coverage", summary="How far the standardization got, in one object")
def coverage(
    run_id: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    """Everything the console's overview needs, in one round trip."""
    run = _get_run(session, run_id)
    s = run.summary_json or {}
    total = run.input_rows or 1
    with_code = s.get("loinc_coverage", 0)
    approved = s.get("approved_loinc_coverage", 0)
    return {
        "run_id": run.id,
        "loinc_version": run.loinc_version,
        "input_rows": run.input_rows,
        "rows_accounted_for": run.rows_accounted_for,
        "by_value_type": s.get("by_value_type", {}),
        "quality": s.get("quality", {}),
        "terminology": {
            "with_any_code": with_code,
            "with_approved_code": approved,
            # The number the whole project exists to surface: a code is present
            # but is no longer the right one.
            "present_but_stale": with_code - approved,
            "no_code_at_all": total - with_code,
            "approved_rate": s.get("approved_loinc_rate"),
        },
        "units": {
            "numeric_rows": s.get("by_value_type", {}).get("QUANTITY", 0),
            "with_ucum": s.get("ucum_coverage", 0),
            "ucum_rate_of_numeric": s.get("ucum_rate_of_numeric"),
        },
        "issues": s.get("issues", {}),
    }
