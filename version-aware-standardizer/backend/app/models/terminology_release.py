"""The release registry -- the foundation of every version-aware answer."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.constants import ImportStatus
from backend.app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TerminologyRelease(Base):
    """One imported LOINC or SNOMED CT release (Master Instruction 17).

    Old releases are never deleted; becoming non-current only flips
    ``is_current``.
    """

    __tablename__ = "terminology_release"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # "LOINC" | "SNOMED_CT" -- see constants.TerminologySystem
    system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Comes from import metadata / user input -- never hard-coded (Hard Rule 3)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(512), nullable=False)

    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    import_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ImportStatus.PENDING.value
    )

    # Free-form provenance: file counts, parsed row counts, Snowstorm import id.
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    __table_args__ = (
        # Hard Rule 12: the same content must not be imported twice, whatever
        # the file was renamed to.
        UniqueConstraint("system", "sha256", name="uq_release_system_sha256"),
        UniqueConstraint("system", "version", name="uq_release_system_version"),
        Index("ix_release_system_current", "system", "is_current"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<TerminologyRelease {self.system} {self.version} "
            f"current={self.is_current} status={self.import_status}>"
        )
