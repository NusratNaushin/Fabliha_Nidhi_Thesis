"""Audit endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.schemas.audit import AuditResultOut, AuditRunIn, AuditRunOut
from backend.app.services import audit_service

router = APIRouter(prefix="/api/v1/audits", tags=["audits"])


@router.post(
    "",
    response_model=AuditRunOut,
    status_code=201,
    summary="Audit stored mappings against the current releases",
)
def create_audit(
    payload: AuditRunIn | None = None, session: Session = Depends(get_session)
) -> AuditRunOut:
    """Read-only with respect to mapping targets.

    The run records a verdict per mapping and may flag mappings as
    NEEDS_REVIEW, but it never changes a target code -- that requires
    ``POST /api/v1/mappings/{id}/approve-replacement``.
    """
    payload = payload or AuditRunIn()
    try:
        run = audit_service.run_audit(
            session,
            source_dataset=payload.source_dataset,
            target_system=payload.target_system,
            limit=payload.limit,
            export_csv=payload.export_csv,
            report_name=payload.report_name,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return AuditRunOut.model_validate(run)


@router.get("", response_model=list[AuditRunOut], summary="List audit runs")
def list_audits(
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[AuditRunOut]:
    return [AuditRunOut.model_validate(r) for r in audit_service.list_runs(session, limit)]


@router.get("/{run_id}", response_model=AuditRunOut, summary="One audit run")
def get_audit(run_id: int, session: Session = Depends(get_session)) -> AuditRunOut:
    run = audit_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No audit run with id {run_id}.")
    return AuditRunOut.model_validate(run)


@router.get(
    "/{run_id}/results",
    response_model=list[AuditResultOut],
    summary="Per-mapping results of an audit run",
)
def get_audit_results(
    run_id: int,
    decision: str | None = Query(
        default=None,
        description="KEEP | KEEP_WITH_WARNING | SUGGEST_REPLACEMENT | MANUAL_REVIEW | UNKNOWN_CODE",
    ),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[AuditResultOut]:
    if audit_service.get_run(session, run_id) is None:
        raise HTTPException(status_code=404, detail=f"No audit run with id {run_id}.")
    return [
        AuditResultOut.model_validate(r)
        for r in audit_service.list_results(
            session, run_id, decision=decision, limit=limit, offset=offset
        )
    ]


@router.get(
    "/{run_id}/report",
    response_class=PlainTextResponse,
    summary="Human-readable audit report",
)
def get_audit_report(run_id: int, session: Session = Depends(get_session)) -> str:
    run = audit_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No audit run with id {run_id}.")
    return audit_service.render_report(run)
