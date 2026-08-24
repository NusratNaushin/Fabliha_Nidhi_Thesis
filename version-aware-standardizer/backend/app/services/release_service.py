"""Terminology release registry.

Enforces the release-level hard rules:

* 11 -- every release carries a SHA-256;
* 12 -- the same release/checksum is never imported twice;
*  3 -- version and effective date come from the caller/file metadata, never
        from a constant in the source tree;
* 17 -- exactly one release per system is ``is_current``; superseded releases
        stay in the database with ``is_current = False`` and are never deleted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.constants import ImportStatus, TerminologySystem
from backend.app.models import TerminologyRelease
from backend.app.utils.checksum import sha256_file
from backend.app.utils.logging import get_logger

log = get_logger("release")


class DuplicateReleaseError(RuntimeError):
    """Raised when a release with the same checksum or version already exists."""

    def __init__(self, message: str, existing: TerminologyRelease) -> None:
        super().__init__(message)
        self.existing = existing


class ReleaseNotFoundError(RuntimeError):
    pass


def normalise_system(system: str) -> str:
    """Accept the spellings people actually type and return the canonical one."""
    value = system.strip().upper().replace("-", "_").replace(" ", "_")
    if value in {"SNOMED", "SNOMEDCT", "SNOMED_CT"}:
        return TerminologySystem.SNOMED_CT.value
    if value == "LOINC":
        return TerminologySystem.LOINC.value
    raise ValueError(
        f"Unsupported terminology system {system!r}. "
        f"Expected one of: {[s.value for s in TerminologySystem]}"
    )


def find_by_checksum(
    session: Session, system: str, sha256: str
) -> TerminologyRelease | None:
    return session.scalar(
        select(TerminologyRelease).where(
            TerminologyRelease.system == normalise_system(system),
            TerminologyRelease.sha256 == sha256,
        )
    )


def find_by_version(
    session: Session, system: str, version: str
) -> TerminologyRelease | None:
    return session.scalar(
        select(TerminologyRelease).where(
            TerminologyRelease.system == normalise_system(system),
            TerminologyRelease.version == version,
        )
    )


def get_current(session: Session, system: str) -> TerminologyRelease | None:
    return session.scalar(
        select(TerminologyRelease).where(
            TerminologyRelease.system == normalise_system(system),
            TerminologyRelease.is_current.is_(True),
        )
    )


def require_current(session: Session, system: str) -> TerminologyRelease:
    release = get_current(session, system)
    if release is None:
        raise ReleaseNotFoundError(
            f"No current {normalise_system(system)} release has been imported. "
            f"Import one with scripts/import_loinc.py or scripts/import_snomed.py."
        )
    return release


def list_releases(session: Session, system: str | None = None) -> list[TerminologyRelease]:
    stmt = select(TerminologyRelease).order_by(
        TerminologyRelease.system, TerminologyRelease.imported_at.desc()
    )
    if system:
        stmt = stmt.where(TerminologyRelease.system == normalise_system(system))
    return list(session.scalars(stmt))


def register_release(
    session: Session,
    *,
    system: str,
    version: str,
    source_path: str | Path,
    effective_date: date | None = None,
    sha256: str | None = None,
    make_current: bool = True,
    notes: str | None = None,
) -> TerminologyRelease:
    """Create the release row after the duplicate checks pass.

    Raises :class:`DuplicateReleaseError` if either the checksum (same content,
    possibly renamed) or the version string is already registered.  The caller
    decides whether that is fatal or a benign "already imported" skip.
    """
    system = normalise_system(system)
    version = version.strip()
    if not version:
        raise ValueError("Release version must be a non-empty string.")

    path = Path(source_path)
    digest = sha256 or sha256_file(path)

    existing = find_by_checksum(session, system, digest)
    if existing is not None:
        raise DuplicateReleaseError(
            f"{system} release with checksum {digest[:12]}... is already imported "
            f"as version {existing.version!r} (file {existing.source_filename!r}).",
            existing,
        )

    existing = find_by_version(session, system, version)
    if existing is not None:
        raise DuplicateReleaseError(
            f"{system} version {version!r} already exists with a DIFFERENT checksum "
            f"({existing.sha256[:12]}... vs {digest[:12]}...). "
            f"Refusing to overwrite an existing release.",
            existing,
        )

    release = TerminologyRelease(
        system=system,
        version=version,
        effective_date=effective_date,
        sha256=digest,
        source_filename=path.name,
        is_current=False,
        import_status=ImportStatus.PENDING.value,
        notes=notes,
    )
    session.add(release)
    session.flush()
    log.info(
        "registered release system=%s version=%s sha256=%s file=%s",
        system,
        version,
        digest[:12],
        path.name,
    )
    if make_current:
        set_current(session, release)
    return release


def set_current(session: Session, release: TerminologyRelease) -> TerminologyRelease:
    """Make ``release`` the single current release for its system.

    The previous current release keeps all of its rows; only the flag moves.
    """
    previous = get_current(session, release.system)
    if previous is not None and previous.id != release.id:
        previous.is_current = False
        log.info(
            "release %s %s is no longer current", previous.system, previous.version
        )
    release.is_current = True
    session.flush()
    log.info("release %s %s is now current", release.system, release.version)
    return release


def mark_status(
    session: Session,
    release: TerminologyRelease,
    status: ImportStatus,
    notes: str | None = None,
) -> TerminologyRelease:
    release.import_status = status.value
    if notes:
        release.notes = notes
    session.flush()
    return release


def current_versions(session: Session) -> dict[str, dict[str, str | None]]:
    """Payload for ``GET /api/v1/releases/current``."""
    out: dict[str, dict[str, str | None]] = {}
    for system in TerminologySystem:
        release = get_current(session, system.value)
        out[system.value] = (
            None
            if release is None
            else {
                "version": release.version,
                "effective_date": (
                    release.effective_date.isoformat() if release.effective_date else None
                ),
                "imported_at": release.imported_at.isoformat(),
                "sha256": release.sha256,
                "source_filename": release.source_filename,
                "import_status": release.import_status,
            }
        )
    return out


# Kept so existing call sites and tests that used the private name still work.
_normalise_system = normalise_system
