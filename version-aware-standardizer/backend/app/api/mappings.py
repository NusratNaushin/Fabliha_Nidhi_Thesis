"""Local mapping endpoints, including the human approval path."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.schemas.mapping import (
    ApproveReplacementIn,
    MappingCreate,
    MappingDetailOut,
    MappingOut,
    MappingRevisionOut,
)
from backend.app.services import mapping_service

router = APIRouter(prefix="/api/v1/mappings", tags=["mappings"])


@router.post(
    "",
    response_model=MappingOut,
    status_code=201,
    summary="Record an existing local mapping",
)
def create_mapping(
    payload: MappingCreate, session: Session = Depends(get_session)
) -> MappingOut:
    try:
        mapping = mapping_service.create_mapping(
            session,
            source_dataset=payload.source_dataset,
            local_code=payload.local_code,
            local_text=payload.local_text,
            target_system=payload.target_system,
            target_code=payload.target_code,
            target_display=payload.target_display,
            source_system=payload.source_system,
            local_context=payload.local_context,
            mapped_against_version=payload.mapped_against_version,
            map_correlation=payload.map_correlation.value,
            review_status=payload.review_status.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except mapping_service.MappingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return MappingOut.model_validate(mapping)


@router.get("", response_model=list[MappingOut], summary="List local mappings")
def list_mappings(
    source_dataset: str | None = None,
    target_system: str | None = None,
    review_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[MappingOut]:
    mappings = mapping_service.list_mappings(
        session,
        source_dataset=source_dataset,
        target_system=target_system,
        review_status=review_status,
        limit=limit,
        offset=offset,
    )
    return [MappingOut.model_validate(m) for m in mappings]


@router.get(
    "/{mapping_id}",
    response_model=MappingDetailOut,
    summary="One mapping with its full revision history",
)
def get_mapping(
    mapping_id: int, session: Session = Depends(get_session)
) -> MappingDetailOut:
    try:
        mapping = mapping_service.get_mapping(session, mapping_id)
    except mapping_service.MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = MappingOut.model_validate(mapping).model_dump()
    payload["revisions"] = [
        MappingRevisionOut.model_validate(r).model_dump()
        for r in mapping_service.get_revisions(session, mapping_id)
    ]
    return MappingDetailOut.model_validate(payload)


@router.post(
    "/{mapping_id}/approve-replacement",
    response_model=MappingRevisionOut,
    summary="Apply a reviewer-approved replacement",
)
def approve_replacement(
    mapping_id: int,
    payload: ApproveReplacementIn,
    session: Session = Depends(get_session),
) -> MappingRevisionOut:
    """The ONLY endpoint that may change a mapping's target code.

    An audit can suggest; only a named reviewer can commit, and the previous
    code plus the release it was mapped against survive on the revision row.
    """
    try:
        revision = mapping_service.approve_replacement(
            session,
            mapping_id=mapping_id,
            target_code=payload.target_code,
            reviewer=payload.reviewer,
            reason=payload.reason,
            audit_result_id=payload.audit_result_id,
            allow_unsuggested=payload.allow_unsuggested,
        )
    except mapping_service.MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except mapping_service.ReplacementRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return MappingRevisionOut.model_validate(revision)


@router.get(
    "/{mapping_id}/history",
    response_model=list[MappingRevisionOut],
    summary="Append-only revision history for a mapping",
)
def mapping_history(
    mapping_id: int, session: Session = Depends(get_session)
) -> list[MappingRevisionOut]:
    try:
        mapping_service.get_mapping(session, mapping_id)
    except mapping_service.MappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        MappingRevisionOut.model_validate(r)
        for r in mapping_service.get_revisions(session, mapping_id)
    ]
