"""Release endpoints -- what terminology versions is this system speaking?"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.schemas.release import CurrentReleasesOut, ReleaseOut
from backend.app.services import release_service

router = APIRouter(prefix="/api/v1/releases", tags=["releases"])


@router.get("", response_model=list[ReleaseOut], summary="List every imported release")
def list_releases(
    system: str | None = Query(default=None, description="LOINC or SNOMED_CT"),
    session: Session = Depends(get_session),
) -> list[ReleaseOut]:
    """Superseded releases stay listed forever -- they are never deleted."""
    return [
        ReleaseOut.model_validate(r) for r in release_service.list_releases(session, system)
    ]


@router.get(
    "/current",
    response_model=CurrentReleasesOut,
    summary="The release currently in force per terminology",
)
def current_releases(session: Session = Depends(get_session)) -> CurrentReleasesOut:
    return CurrentReleasesOut.model_validate(release_service.current_versions(session))
