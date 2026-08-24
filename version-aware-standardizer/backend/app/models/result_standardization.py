"""Result standardization: raw lab results in, standard observations out.

The layer above terminology. The existing tables answer "is this code still the
right code?"; these answer "what did the test actually say, and how do I write
that down so another system understands it?"

Two rules shape every table here:

* **Raw is immutable.** ``source_lab_result`` is written once at import and
  never edited. Everything standardized lands in a separate table that points
  back at it, so the original value, unit and flag survive whatever we do.
* **Nothing is dropped.** A row we cannot standardize is quarantined with a
  named reason, not discarded. Input rows must equal standardized rows plus
  quarantined rows, and a test asserts it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.constants import QualityStatus, ValueMappingStatus
from backend.app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Staging: what arrived, exactly as it arrived
# ===========================================================================
class SourceLabItem(Base):
    """A row of the source dataset's test dictionary (MIMIC ``D_LABITEMS``).

    This says *what the test is*. It is deliberately separate from
    ``local_mapping``: this is the raw dictionary as shipped, while a mapping is
    a curated claim about it that carries review state and history.
    """

    __tablename__ = "source_lab_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    itemid: Mapped[str] = mapped_column(String(64), nullable=False)

    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    fluid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The code the source shipped. Never overwritten, however wrong it turns out
    # to be -- the audit's whole job is to judge it.
    original_loinc_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    ingestion_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("source_dataset", "itemid", name="uq_source_lab_item"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SourceLabItem {self.source_dataset}/{self.itemid} {self.label!r}>"


class SourceLabResult(Base):
    """One raw laboratory result, preserved verbatim (MIMIC ``LABEVENTS``).

    Patient identifiers never land here in the clear: ``subject_key`` and
    ``encounter_key`` are keyed HMACs computed at import. The secret lives in
    the environment, never in the repository, so the pseudonyms cannot be
    reversed from anything committed here.
    """

    __tablename__ = "source_lab_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_row_id: Mapped[str] = mapped_column(String(64), nullable=False)

    subject_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Null for an outpatient row -- a real state, not missing data, so it is
    # never a reason to drop the row.
    encounter_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    itemid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    charttime: Mapped[str | None] = mapped_column(String(32), nullable=True)

    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_flag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ingestion_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("source_dataset", "source_row_id", name="uq_source_lab_result"),
        Index("ix_source_result_dataset_item", "source_dataset", "itemid"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SourceLabResult {self.source_dataset}/{self.source_row_id} item={self.itemid}>"


# ===========================================================================
# Rules: curated, versioned, reviewable
# ===========================================================================
class UnitMappingRule(Base):
    """How one raw unit string becomes a UCUM code -- and whether the number moves.

    Two very different things live here, and the difference is the whole point:

    * ``conversion_factor`` of 1.0 with no offset is a **spelling fix**
      (``mg/dl`` -> ``mg/dL``). The number is untouched.
    * anything else is a **conversion** (``mg/dL`` -> ``mmol/L``), which changes
      the number and is analyte-specific -- glucose and creatinine do not share
      a factor. A conversion rule must therefore name its ``loinc_code``; a
      blanket "all mg/dL to mmol/L" rule would be a clinical error.
    """

    __tablename__ = "unit_mapping_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_unit: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_ucum_code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Null means the rule is a pure spelling normalisation valid for any test.
    # A rule that changes the number must name the test it applies to.
    loinc_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    specimen: Mapped[str | None] = mapped_column(String(128), nullable=True)

    conversion_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    conversion_offset: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    precision: Mapped[int | None] = mapped_column(Integer, nullable=True)

    clinical_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNREVIEWED")
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "source_unit", "loinc_code", "rule_version", name="uq_unit_rule"
        ),
        Index("ix_unit_rule_lookup", "source_unit", "active"),
    )

    @property
    def changes_the_number(self) -> bool:
        return self.conversion_factor != 1.0 or self.conversion_offset != 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<UnitMappingRule {self.source_unit!r}->{self.normalized_ucum_code!r} "
            f"loinc={self.loinc_code} x{self.conversion_factor}>"
        )


class ResultValueMapping(Base):
    """How one categorical result string is normalised, and coded if we may.

    ``NEG``, ``Negative``, ``negative`` and ``Not detected`` are not
    automatically the same thing, so this is a curated table rather than a
    lowercase-and-hope function.

    Without a SNOMED CT International licence the honest output is normalised
    *text* and a null code, marked ``TEXT_NORMALIZED_CODE_PENDING``. Inventing a
    concept id would be worse than admitting the gap.
    """

    __tablename__ = "result_value_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Null means the rule applies to any test using this wording.
    loinc_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_value: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    normalized_display: Mapped[str] = mapped_column(String(256), nullable=False)
    target_system: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mapping_status: Mapped[str] = mapped_column(
        String(48), nullable=False,
        default=ValueMappingStatus.TEXT_NORMALIZED_CODE_PENDING.value,
    )
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "source_value", "loinc_code", "rule_version", name="uq_value_rule"
        ),
        Index("ix_value_rule_lookup", "source_value", "active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ResultValueMapping {self.source_value!r}->{self.normalized_display!r}>"


# ===========================================================================
# Runs and output
# ===========================================================================
class StandardizationRun(Base):
    """One execution of the standardizer, stamped with everything it depended on.

    The manifest matters as much as the output: a number in a thesis has to be
    traceable to the LOINC release, the rule versions and the commit that
    produced it.
    """

    __tablename__ = "standardization_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    loinc_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snomed_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value_rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    input_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    standardized_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    observations: Mapped[list["StandardizedLabObservation"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def rows_accounted_for(self) -> bool:
        """Nothing may vanish. This invariant is asserted, not assumed."""
        return self.input_rows == self.standardized_rows + self.quarantined_rows

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<StandardizationRun {self.id} {self.source_dataset} "
            f"in={self.input_rows} out={self.standardized_rows} q={self.quarantined_rows}>"
        )


class StandardizedLabObservation(Base):
    """One standardized result, carrying the raw one it came from.

    Both halves live in the same row on purpose. Anyone reading a standardized
    value can see, without a join, exactly what the source said -- which is the
    only way to check whether standardizing changed the meaning.
    """

    __tablename__ = "standardized_lab_observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    standardization_run_id: Mapped[int] = mapped_column(
        ForeignKey("standardization_run.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # -- provenance ------------------------------------------------------
    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    encounter_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    itemid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    charttime: Mapped[str | None] = mapped_column(String(32), nullable=True)

    source_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_fluid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_category: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # -- terminology -----------------------------------------------------
    original_loinc_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mapped_against_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolver_decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # What the engine would propose. Kept strictly apart from the approved code:
    # a suggestion is not a decision, and this column is never read as one.
    engine_suggested_loinc: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Only ever a code that is valid right now and that a person stands behind.
    approved_current_loinc: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    current_loinc_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    loinc_component: Mapped[str | None] = mapped_column(Text, nullable=True)
    loinc_property: Mapped[str | None] = mapped_column(String(128), nullable=True)
    loinc_time_aspect: Mapped[str | None] = mapped_column(String(128), nullable=True)
    loinc_system: Mapped[str | None] = mapped_column(String(256), nullable=True)
    loinc_scale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    loinc_method: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # -- the raw answer, untouched ---------------------------------------
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_flag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # -- the standardized answer -----------------------------------------
    value_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    comparator: Mapped[str | None] = mapped_column(String(4), nullable=True)
    standard_numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    standard_ucum_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    normalized_text_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    coded_value_system: Mapped[str | None] = mapped_column(String(128), nullable=True)
    coded_value_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coded_value_display: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_mapping_status: Mapped[str | None] = mapped_column(String(48), nullable=True)

    interpretation_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    data_absent_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # -- quality ---------------------------------------------------------
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=QualityStatus.OK.value, index=True
    )
    issues_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    unit_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("unit_mapping_rule.id", ondelete="SET NULL"), nullable=True
    )
    value_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("result_value_mapping.id", ondelete="SET NULL"), nullable=True
    )
    mapping_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("mapping_revision.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    run: Mapped["StandardizationRun"] = relationship(back_populates="observations")

    __table_args__ = (
        UniqueConstraint(
            "standardization_run_id", "source_dataset", "source_row_id",
            name="uq_standardized_row_per_run",
        ),
        Index("ix_std_obs_run_quality", "standardization_run_id", "quality_status"),
        Index("ix_std_obs_loinc", "approved_current_loinc"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<StandardizedLabObservation {self.source_row_id} "
            f"{self.approved_current_loinc or self.original_loinc_code} "
            f"{self.value_type} {self.quality_status}>"
        )


class StandardizationIssue(Base):
    """One named problem on one row. Counted and reported, never swallowed."""

    __tablename__ = "standardization_issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    standardization_run_id: Mapped[int] = mapped_column(
        ForeignKey("standardization_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("standardized_lab_observation.id", ondelete="CASCADE"), nullable=True
    )

    source_dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    itemid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    issue_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="WARNING")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        Index("ix_issue_run_code", "standardization_run_id", "issue_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StandardizationIssue {self.issue_code} row={self.source_row_id}>"
