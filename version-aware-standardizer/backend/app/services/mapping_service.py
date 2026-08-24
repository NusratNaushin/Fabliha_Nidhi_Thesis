"""Local mapping CRUD plus the human-approval path.

Two hard rules shape this module:

* 4 / 5 -- an old mapping is never deleted and history is never overwritten.
  Every change appends a row to ``mapping_revision``.
* 6 / 7 -- a replacement is never committed just because the engine found one.
  ``approve_replacement`` is the *only* function that mutates a target code,
  and it requires an explicit reviewer.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.constants import (
    Decision,
    MapCorrelation,
    ReviewStatus,
    TerminologySystem,
)
from backend.app.models import AuditResult, LocalMapping, MappingRevision
from backend.app.services import release_service
from backend.app.services.loinc_resolver import LoincResolver
from backend.app.services.snomed_resolver import SnomedResolver
from backend.app.utils.logging import get_logger

log = get_logger("mapping")


class MappingError(RuntimeError):
    pass


class MappingNotFoundError(MappingError):
    pass


class ReplacementRejected(MappingError):
    """Raised when a proposed replacement fails a pre-approval check."""


def _normalise_system(system: str) -> str:
    value = (system or "").strip().upper().replace("-", "_").replace(" ", "_")
    if value in {"SNOMED", "SNOMEDCT", "SNOMED_CT"}:
        return TerminologySystem.SNOMED_CT.value
    if value == "LOINC":
        return TerminologySystem.LOINC.value
    raise ValueError(
        f"Unsupported target system {system!r}; expected LOINC or SNOMED_CT."
    )


def create_mapping(
    session: Session,
    *,
    source_dataset: str,
    local_code: str,
    local_text: str,
    target_system: str,
    target_code: str,
    target_display: str | None = None,
    source_system: str | None = None,
    local_context: dict | None = None,
    mapped_against_version: str | None = None,
    map_correlation: str = MapCorrelation.NOT_SPECIFIED.value,
    review_status: str = ReviewStatus.UNREVIEWED.value,
) -> LocalMapping:
    """Record one existing local mapping.

    ``mapped_against_version`` defaults to the release that is current *now*,
    which is the honest answer when a mapping is created through this API.  For
    historical imports (MIMIC, a hospital extract) the caller should pass the
    release the mapping actually came from, or leave it explicit as unknown.
    """
    target_system = _normalise_system(target_system)
    if not local_code or not str(local_code).strip():
        raise MappingError("local_code is required.")
    if not target_code or not str(target_code).strip():
        raise MappingError("target_code is required.")

    existing = session.scalar(
        select(LocalMapping).where(
            LocalMapping.source_dataset == source_dataset,
            LocalMapping.local_code == str(local_code),
            LocalMapping.target_system == target_system,
        )
    )
    if existing is not None:
        raise MappingError(
            f"A mapping for {source_dataset}/{local_code} -> {target_system} "
            f"already exists (id={existing.id}). Use the approval endpoint to "
            f"change its target so that history is preserved."
        )

    mapping = LocalMapping(
        source_dataset=source_dataset,
        source_system=source_system,
        local_code=str(local_code),
        local_text=local_text,
        local_context_json=local_context,
        target_system=target_system,
        target_code=str(target_code).strip(),
        target_display=target_display,
        mapped_against_version=mapped_against_version,
        map_correlation=map_correlation,
        review_status=review_status,
    )
    session.add(mapping)
    session.flush()
    log.info(
        "created mapping %s/%s -> %s:%s against version=%s",
        source_dataset,
        local_code,
        target_system,
        target_code,
        mapped_against_version,
    )
    return mapping


def bulk_create_mappings(session: Session, rows: list[dict]) -> tuple[int, int]:
    """Insert many mappings, skipping ones that already exist.

    Returns ``(created, skipped)``.  Used by the MIMIC importer.
    """
    created = 0
    skipped = 0
    existing_keys = {
        (m.source_dataset, m.local_code, m.target_system)
        for m in session.scalars(select(LocalMapping))
    }
    for row in rows:
        key = (
            row["source_dataset"],
            str(row["local_code"]),
            _normalise_system(row["target_system"]),
        )
        if key in existing_keys:
            skipped += 1
            continue
        create_mapping(session, **row)
        existing_keys.add(key)
        created += 1
    return created, skipped


def get_mapping(session: Session, mapping_id: int) -> LocalMapping:
    mapping = session.get(LocalMapping, mapping_id)
    if mapping is None:
        raise MappingNotFoundError(f"No local mapping with id {mapping_id}.")
    return mapping


def list_mappings(
    session: Session,
    *,
    source_dataset: str | None = None,
    target_system: str | None = None,
    review_status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[LocalMapping]:
    stmt = select(LocalMapping).order_by(LocalMapping.id)
    if source_dataset:
        stmt = stmt.where(LocalMapping.source_dataset == source_dataset)
    if target_system:
        stmt = stmt.where(LocalMapping.target_system == _normalise_system(target_system))
    if review_status:
        stmt = stmt.where(LocalMapping.review_status == review_status)
    return list(session.scalars(stmt.limit(limit).offset(offset)))


def count_mappings(session: Session, **filters: str | None) -> int:
    stmt = select(func.count()).select_from(LocalMapping)
    if filters.get("source_dataset"):
        stmt = stmt.where(LocalMapping.source_dataset == filters["source_dataset"])
    if filters.get("target_system"):
        stmt = stmt.where(
            LocalMapping.target_system == _normalise_system(filters["target_system"])
        )
    return int(session.scalar(stmt) or 0)


def get_revisions(session: Session, mapping_id: int) -> list[MappingRevision]:
    return list(
        session.scalars(
            select(MappingRevision)
            .where(MappingRevision.mapping_id == mapping_id)
            .order_by(MappingRevision.id)
        )
    )


# ---------------------------------------------------------------------------
# Human approval -- the only mutation path for a target code
# ---------------------------------------------------------------------------
def _allowed_targets(session: Session, mapping: LocalMapping) -> set[str]:
    """Codes previously *suggested* by the engine for this mapping."""
    allowed: set[str] = set()
    results = session.scalars(
        select(AuditResult).where(AuditResult.mapping_id == mapping.id)
    )
    for result in results:
        for target in result.suggested_targets_json or []:
            code = target.get("code") or target.get("concept_id")
            if code:
                allowed.add(str(code))
    return allowed


def _target_is_currently_valid(
    session: Session, target_system: str, target_code: str
) -> tuple[bool, str, str | None, str | None]:
    """(is_valid, status, display, current_version) for a proposed target."""
    if target_system == TerminologySystem.LOINC.value:
        resolver = LoincResolver(session)
        resolution = resolver.resolve(target_code)
        display = resolution.display
        return (
            resolution.decision in (Decision.KEEP, Decision.KEEP_WITH_WARNING),
            resolution.status.value,
            display,
            resolver.version,
        )
    resolver = SnomedResolver(session)
    resolution = resolver.resolve(target_code)
    return (
        resolution.decision is Decision.KEEP,
        resolution.status.value,
        resolution.display,
        resolver.version,
    )


def approve_replacement(
    session: Session,
    *,
    mapping_id: int,
    target_code: str,
    reviewer: str,
    reason: str | None = None,
    audit_result_id: int | None = None,
    allow_unsuggested: bool = False,
) -> MappingRevision:
    """Apply a reviewer-approved replacement and append it to history.

    Pre-approval checks, in the order the Master Instruction lists them:

    1. the code must be one the engine actually suggested (unless the reviewer
       explicitly overrides with ``allow_unsuggested``);
    2. the code must be valid in the *current* release;
    3. a revision row is written;
    4. the mapping is updated;
    5. the previous code and the version it was mapped against survive on the
       revision row.
    """
    mapping = get_mapping(session, mapping_id)
    target_code = (target_code or "").strip()
    reviewer = (reviewer or "").strip()

    if not target_code:
        raise ReplacementRejected("target_code is required.")
    if not reviewer:
        raise ReplacementRejected(
            "reviewer is required: an approval must be attributable to a person."
        )
    if target_code == mapping.target_code:
        raise ReplacementRejected(
            f"Mapping {mapping_id} already points at {target_code}."
        )

    if not allow_unsuggested:
        allowed = _allowed_targets(session, mapping)
        if not allowed:
            raise ReplacementRejected(
                f"No audit has suggested any replacement for mapping {mapping_id}. "
                f"Run an audit first, or pass allow_unsuggested=true to record a "
                f"purely manual decision."
            )
        if target_code not in allowed:
            raise ReplacementRejected(
                f"{target_code} was never suggested by the engine for mapping "
                f"{mapping_id}. Suggested codes were: {sorted(allowed)}. "
                f"Pass allow_unsuggested=true to override deliberately."
            )

    is_valid, status, display, current_version = _target_is_currently_valid(
        session, mapping.target_system, target_code
    )
    if not is_valid:
        raise ReplacementRejected(
            f"{target_code} is {status} in the current "
            f"{mapping.target_system} release ({current_version}); refusing to "
            f"migrate a mapping onto a target that is not currently valid."
        )

    revision = MappingRevision(
        mapping_id=mapping.id,
        old_target_code=mapping.target_code,
        old_target_version=mapping.mapped_against_version,
        new_target_code=target_code,
        new_target_version=current_version,
        reason=reason
        or f"reviewer-approved replacement ({status} in {current_version})",
        audit_result_id=audit_result_id,
        approved=True,
        approved_by=reviewer,
        approved_at=datetime.now(timezone.utc),
    )
    session.add(revision)

    mapping.target_code = target_code
    mapping.target_display = display or mapping.target_display
    mapping.mapped_against_version = current_version
    mapping.review_status = ReviewStatus.APPROVED.value
    session.flush()

    log.info(
        "approved replacement mapping=%s %s -> %s by %s (version %s)",
        mapping.id,
        revision.old_target_code,
        target_code,
        reviewer,
        current_version,
    )
    return revision


def current_release_versions(session: Session) -> dict[str, str | None]:
    loinc = release_service.get_current(session, TerminologySystem.LOINC.value)
    snomed = release_service.get_current(session, TerminologySystem.SNOMED_CT.value)
    return {
        TerminologySystem.LOINC.value: loinc.version if loinc else None,
        TerminologySystem.SNOMED_CT.value: snomed.version if snomed else None,
    }


__all__ = [
    "MappingError",
    "MappingNotFoundError",
    "ReplacementRejected",
    "approve_replacement",
    "bulk_create_mappings",
    "count_mappings",
    "create_mapping",
    "current_release_versions",
    "get_mapping",
    "get_revisions",
    "list_mappings",
]
