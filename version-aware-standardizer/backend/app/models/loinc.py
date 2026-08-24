"""LOINC release content: concepts, MapTo replacements, change snapshot."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class LoincConceptVersion(Base):
    """One LOINC term as it appears in ONE release (Master Instruction 14).

    A code appears once per imported release, so status/metadata history across
    releases is queryable without ever mutating an older row.
    """

    __tablename__ = "loinc_concept_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    loinc_num: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    long_common_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The six LOINC axes.
    component: Mapped[str | None] = mapped_column(Text, nullable=True)
    property: Mapped[str | None] = mapped_column(String(128), nullable=True)
    time_aspect: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scale_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    method_type: Mapped[str | None] = mapped_column(String(256), nullable=True)

    class_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    change_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version_first_released: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version_last_changed: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "loinc_num", "release_version", name="uq_loinc_concept_code_release"
        ),
        Index("ix_loinc_concept_code", "loinc_num"),
        Index("ix_loinc_concept_release_code", "release_version", "loinc_num"),
        Index("ix_loinc_concept_release_status", "release_version", "status"),
    )

    # -- metadata comparison (Master Instruction 22) -----------------------
    COMPARED_FIELDS: tuple[str, ...] = (
        "status",
        "long_common_name",
        "short_name",
        "component",
        "property",
        "time_aspect",
        "system",
        "scale_type",
        "method_type",
        "class_name",
    )

    def metadata_snapshot(self) -> dict[str, str | None]:
        return {f: getattr(self, f) for f in self.COMPARED_FIELDS}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoincConceptVersion {self.loinc_num}@{self.release_version} {self.status}>"


class LoincMapTo(Base):
    """A row of the official MapTo.csv (Master Instruction 15).

    One source may legitimately have several targets; those rows are kept
    separate and are never collapsed -- the ambiguity itself is the signal that
    forces MANUAL_REVIEW.
    """

    __tablename__ = "loinc_map_to"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    source_loinc: Mapped[str] = mapped_column(String(32), nullable=False)
    target_loinc: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_loinc",
            "target_loinc",
            "release_version",
            name="uq_loinc_mapto_pair_release",
        ),
        Index("ix_loinc_mapto_source", "release_version", "source_loinc"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoincMapTo {self.source_loinc}->{self.target_loinc}@{self.release_version}>"


class LoincChange(Base):
    """A row of the official LoincChangeSnapshot.csv (Master Instruction 16)."""

    __tablename__ = "loinc_change"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    loinc_num: Mapped[str] = mapped_column(String(32), nullable=False)
    property: Mapped[str] = mapped_column(String(128), nullable=False)
    value_prior: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_current: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_loinc_change_release_code", "release_version", "loinc_num"),
        Index("ix_loinc_change_release_prop", "release_version", "property"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoincChange {self.loinc_num} {self.property}@{self.release_version}>"
