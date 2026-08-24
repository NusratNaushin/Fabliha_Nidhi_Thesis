"""SNOMED CT lookup, search and resolution endpoints.

Lookup and resolution are answered from the locally parsed RF2 tables, so they
work with Snowstorm down.  Snowstorm is used to enrich the display term and to
serve term search, and its absence degrades the response rather than failing it
-- except for ``/search``, which genuinely needs the server.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.database import get_session
from backend.app.schemas.terminology import (
    SnomedConceptOut,
    SnomedResolveOut,
    SnomedSearchOut,
)
from backend.app.services.snomed_resolver import SnomedResolver
from backend.app.services.snowstorm_client import SnowstormClient, SnowstormError

router = APIRouter(prefix="/api/v1/snomed", tags=["snomed"])


def _preferred_term(concept_id: str) -> str | None:
    """Best-effort display term; never fatal."""
    try:
        with SnowstormClient() as client:
            return client.preferred_term(concept_id)
    except SnowstormError:
        return None


@router.get(
    "/search",
    response_model=SnomedSearchOut,
    summary="Active-only concept search via Snowstorm",
)
def search_snomed(
    term: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
    ecl: str | None = Query(default=None, description="optional ECL constraint"),
) -> SnomedSearchOut:
    """Inactive concepts are never returned: they must not become new mappings."""
    client = SnowstormClient()
    try:
        items = client.search_concepts(term, limit=limit, active_only=True, ecl=ecl)
    except SnowstormError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        client.close()
    return SnomedSearchOut(
        term=term, active_only=True, branch=client.branch, items=items
    )


@router.get(
    "/{concept_id}",
    response_model=SnomedConceptOut,
    summary="Raw SNOMED record from the current parsed release",
)
def get_snomed(
    concept_id: str, session: Session = Depends(get_session)
) -> SnomedConceptOut:
    resolver = SnomedResolver(session)
    if resolver.release is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No current SNOMED CT release is imported. Run "
                "scripts/import_snomed.py --file data/raw/snomed/<RF2>.zip "
                "--version <YYYYMMDD> first."
            ),
        )
    record = resolver.lookup(concept_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"{concept_id} is not present in SNOMED CT {resolver.version}.",
        )
    # The parsed release answers this offline; Snowstorm is only a fallback
    # for releases imported with --skip-descriptions.
    if not record.get("preferred_term"):
        record["preferred_term"] = _preferred_term(concept_id)
        record["display"] = record.get("display") or record["preferred_term"]
    return SnomedConceptOut.model_validate(record)


@router.get(
    "/{concept_id}/resolve",
    response_model=SnomedResolveOut,
    summary="Version-aware verdict for a SNOMED concept",
)
def resolve_snomed(
    concept_id: str, session: Session = Depends(get_session)
) -> SnomedResolveOut:
    resolver = SnomedResolver(session)
    resolution = resolver.resolve(concept_id)
    payload = resolution.as_dict()
    payload["display"] = payload.get("display") or _preferred_term(concept_id)
    for target in payload["suggested_targets"]:
        if target.get("usable") and not target.get("display"):
            target["display"] = _preferred_term(target["concept_id"])
    return SnomedResolveOut.model_validate(payload)
