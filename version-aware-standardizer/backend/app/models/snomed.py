"""SNOMED CT RF2 content parsed locally.

Master Instruction 9 is deliberate: the version-aware logic must NOT depend on
Snowstorm alone.  Snowstorm gives search and preferred terms; these tables give
us reproducible, release-stamped facts we can diff offline.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.database import Base


class SnomedConceptVersion(Base):
    """A row of sct2_Concept_Snapshot for ONE release (Master Instruction 9)."""

    __tablename__ = "snomed_concept_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    concept_id: Mapped[str] = mapped_column(String(24), nullable=False)
    effective_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    module_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    definition_status_id: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "concept_id", "release_version", name="uq_snomed_concept_release"
        ),
        Index("ix_snomed_concept_id", "concept_id"),
        Index("ix_snomed_concept_release_active", "release_version", "active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SnomedConceptVersion {self.concept_id}@{self.release_version} active={self.active}>"


class SnomedHistoricalAssociation(Base):
    """A row of an association reference set (Master Instruction 10).

    ``refset_id`` identifies the association type (REPLACED BY, SAME AS, ...).
    Inactive refset members are stored too -- they are part of the record --
    but are filtered out when suggesting a current replacement.
    """

    __tablename__ = "snomed_historical_association"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    member_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refset_id: Mapped[str] = mapped_column(String(24), nullable=False)
    referenced_component_id: Mapped[str] = mapped_column(String(24), nullable=False)
    target_component_id: Mapped[str] = mapped_column(String(24), nullable=False)
    effective_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "member_id", "release_version", name="uq_snomed_assoc_member_release"
        ),
        Index(
            "ix_snomed_assoc_source",
            "release_version",
            "referenced_component_id",
            "active",
        ),
        Index("ix_snomed_assoc_refset", "release_version", "refset_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SnomedHistoricalAssociation {self.referenced_component_id}"
            f"-[{self.refset_id}]->{self.target_component_id}>"
        )


class SnomedInactivation(Base):
    """A row of the Concept Inactivation Indicator refset (Master Instruction 11)."""

    __tablename__ = "snomed_inactivation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    member_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    concept_id: Mapped[str] = mapped_column(String(24), nullable=False)
    value_id: Mapped[str] = mapped_column(String(24), nullable=False)
    effective_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "member_id", "release_version", name="uq_snomed_inactivation_member_release"
        ),
        Index(
            "ix_snomed_inactivation_concept",
            "release_version",
            "concept_id",
            "active",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SnomedInactivation {self.concept_id} value={self.value_id}>"


class SnomedConceptTerm(Base):
    """The human-readable names of a concept, per release.

    One row per concept rather than the ~1.4 million raw description rows: the
    auditor only ever needs the fully specified name and the preferred term, so
    those are resolved once at import time from the description file and the
    language reference set, and the bulk is discarded.

    Populating this is what lets an audit report say "Escherichia coli" instead
    of "112283007" with Snowstorm switched off.
    """

    __tablename__ = "snomed_concept_term"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("terminology_release.id", ondelete="CASCADE"), nullable=False
    )
    release_version: Mapped[str] = mapped_column(String(64), nullable=False)

    concept_id: Mapped[str] = mapped_column(String(24), nullable=False)
    fsn: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which language reference set supplied the preferred term.
    language_refset_id: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "concept_id", "release_version", name="uq_snomed_term_concept_release"
        ),
        Index("ix_snomed_term_release_concept", "release_version", "concept_id"),
    )

    @property
    def display(self) -> str | None:
        """Preferred term when there is one, otherwise the FSN."""
        return self.preferred_term or self.fsn

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SnomedConceptTerm {self.concept_id}@{self.release_version} {self.display!r}>"
