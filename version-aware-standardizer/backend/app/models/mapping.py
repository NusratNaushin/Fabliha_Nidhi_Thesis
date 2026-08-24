"""Local clinical mappings and their immutable revision history."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.constants import MapCorrelation, ReviewStatus
from backend.app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LocalMapping(Base):
    """A local clinical term already mapped to LOINC or SNOMED CT.

    ``mapped_against_version`` is the whole point of the project: it records
    *which terminology release the mapping was made against*, so a later audit
    can say "this decision was correct in 2.81 and is stale in 2.83" rather
    than silently pretending the mapping was always current.
    """

    __tablename__ = "local_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # MIMIC_III | BANGLADESH_HOSPITAL_A | MANUAL_TEST | ...
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_system: Mapped[str | None] = mapped_column(String(128), nullable=True)

    local_code: Mapped[str] = mapped_column(String(128), nullable=False)
    local_text: Mapped[str] = mapped_column(Text, nullable=False)
    local_context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    target_system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_code: Mapped[str] = mapped_column(String(32), nullable=False)
    target_display: Mapped[str | None] = mapped_column(Text, nullable=True)

    mapped_against_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Sung et al. 2023 step 7: record HOW the local term relates to the target.
    map_correlation: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MapCorrelation.NOT_SPECIFIED.value
    )

    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReviewStatus.UNREVIEWED.value
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    revisions: Mapped[list["MappingRevision"]] = relationship(
        back_populates="mapping",
        cascade="all, delete-orphan",
        order_by="MappingRevision.id",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_dataset",
            "local_code",
            "target_system",
            name="uq_local_mapping_dataset_code_system",
        ),
        Index("ix_local_mapping_target", "target_system", "target_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<LocalMapping {self.source_dataset}/{self.local_code} -> "
            f"{self.target_system}:{self.target_code}@{self.mapped_against_version}>"
        )


class MappingRevision(Base):
    """Append-only history (Master Instruction 19).

    Rows are never updated and never deleted.  A mapping that moved A -> B -> C
    has two rows; both remain readable forever, which is what makes a published
    audit reproducible.
    """

    __tablename__ = "mapping_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("local_mapping.id", ondelete="CASCADE"), nullable=False, index=True
    )

    old_target_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_target_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_target_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_target_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_result_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_result.id", ondelete="SET NULL"), nullable=True
    )

    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    mapping: Mapped["LocalMapping"] = relationship(back_populates="revisions")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MappingRevision mapping={self.mapping_id} "
            f"{self.old_target_code}->{self.new_target_code} approved={self.approved}>"
        )
