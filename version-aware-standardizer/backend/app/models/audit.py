"""Audit runs and per-mapping audit results."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.constants import AuditRunStatus
from backend.app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditRun(Base):
    """One execution of the auditor over a set of mappings.

    The LOINC and SNOMED versions in force are stamped on the run, so any
    number reported in a thesis can be traced back to exactly the releases that
    produced it (Master Instruction 26: "every audit must be reproducible").
    """

    __tablename__ = "audit_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    loinc_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snomed_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mapping_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AuditRunStatus.RUNNING.value
    )
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional filter used for this run, e.g. {"source_dataset": "MIMIC_III"}
    scope_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    results: Mapped[list["AuditResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AuditRun {self.id} loinc={self.loinc_version} "
            f"snomed={self.snomed_version} n={self.mapping_count} {self.status}>"
        )


class AuditResult(Base):
    """The engine's verdict for ONE mapping in ONE run (Master Instruction 25)."""

    __tablename__ = "audit_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_run_id: Mapped[int] = mapped_column(
        ForeignKey("audit_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("local_mapping.id", ondelete="SET NULL"), nullable=True, index=True
    )

    target_system: Mapped[str] = mapped_column(String(32), nullable=False)
    old_code: Mapped[str] = mapped_column(String(32), nullable=False)

    # The release the verdict was computed against -- NOT the release the
    # mapping was originally made against.
    current_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    terminology_status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    suggested_targets_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    run: Mapped["AuditRun"] = relationship(back_populates="results")

    __table_args__ = (
        Index("ix_audit_result_run_decision", "audit_run_id", "decision"),
        Index("ix_audit_result_code", "target_system", "old_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<AuditResult run={self.audit_run_id} {self.target_system}:{self.old_code} "
            f"{self.terminology_status}/{self.decision}>"
        )
