"""Release endpoints -- what terminology versions is this system speaking?"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.constants import TerminologySystem
from backend.app.database import get_session
from backend.app.schemas.release import CurrentReleasesOut, ReleaseOut
from backend.app.services import loinc_diff, release_service, snomed_diff

router = APIRouter(prefix="/api/v1/releases", tags=["releases"])

# A diff between two imported releases is deterministic: a release is never
# deleted and its content is never rewritten (Hard Rules 4, 12 and 17), so the
# same pair always yields the same answer. Caching it keeps the UI responsive --
# comparing two full LOINC releases means loading ~220,000 rows.
_DIFF_CACHE: dict[tuple[str, str, str], dict] = {}
_DIFF_CACHE_MAX = 16


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


@router.get(
    "/diff",
    summary="Compare two imported releases of the same terminology",
)
def diff_releases(
    system: str = Query(description="LOINC or SNOMED_CT"),
    old: str = Query(description="the older release version"),
    new: str = Query(description="the newer release version"),
    session: Session = Depends(get_session),
) -> dict:
    """What changed between two releases, and does it match the official record?

    For LOINC the answer carries a validation block comparing our computed diff
    against the release's own Change Snapshot -- the number that matters is
    ``missed_changes``, which should be zero.
    """
    try:
        normalised = release_service.normalise_system(system)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if old == new:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "old and new must name different releases.",
        )

    for version in (old, new):
        if release_service.find_by_version(session, normalised, version) is None:
            available = [
                r.version for r in release_service.list_releases(session, normalised)
            ]
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"{normalised} release {version!r} has not been imported. "
                f"Available: {available or 'none'}",
            )

    key = (normalised, old, new)
    if key in _DIFF_CACHE:
        return _DIFF_CACHE[key]

    try:
        if normalised == TerminologySystem.LOINC.value:
            report = loinc_diff.diff_releases(
                session, old_version=old, new_version=new, export_csv=False
            )
        else:
            report = snomed_diff.diff_releases(
                session, old_version=old, new_version=new, export_csv=False
            )
    except (loinc_diff.DiffError, snomed_diff.SnomedDiffError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    payload = {"system": normalised, **report.as_dict()}
    if len(_DIFF_CACHE) >= _DIFF_CACHE_MAX:
        _DIFF_CACHE.pop(next(iter(_DIFF_CACHE)))
    _DIFF_CACHE[key] = payload
    return payload
