"""LOINC lookup and resolution endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.schemas.terminology import LoincConceptOut, LoincResolveOut
from backend.app.services.loinc_resolver import LoincResolver

router = APIRouter(prefix="/api/v1/loinc", tags=["loinc"])


@router.get(
    "/{code}",
    response_model=LoincConceptOut,
    summary="Raw LOINC record from the current release",
)
def get_loinc(code: str, session: Session = Depends(get_session)) -> LoincConceptOut:
    resolver = LoincResolver(session)
    if resolver.release is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No current LOINC release is imported. Run "
                "scripts/import_loinc.py --file data/raw/loinc/<release>.zip "
                "--version <version> first."
            ),
        )
    record = resolver.lookup(code)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"{code} is not present in LOINC {resolver.version}.",
        )
    return LoincConceptOut.model_validate(record)


@router.get(
    "/{code}/resolve",
    response_model=LoincResolveOut,
    summary="Version-aware verdict for a LOINC code",
)
def resolve_loinc(
    code: str,
    mapped_against_version: str | None = Query(
        default=None,
        description=(
            "the release the mapping was originally made against; supplying it "
            "enables metadata-drift detection"
        ),
    ),
    session: Session = Depends(get_session),
) -> LoincResolveOut:
    """Never mutates anything: a suggestion is a suggestion."""
    resolver = LoincResolver(session)
    resolution = resolver.resolve(code, mapped_against_version=mapped_against_version)
    return LoincResolveOut.model_validate(resolution.as_dict())
