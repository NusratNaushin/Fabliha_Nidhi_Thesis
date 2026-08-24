"""Turning one raw laboratory row into one standardized observation.

This is where the terminology layer and the value layer meet. For every row it
asks, in order: *which test is this, is that code still the right code, what
kind of answer should it carry, what does the answer actually say, and what unit
is that in* -- recording at each step both what it concluded and why.

Two invariants hold for the whole run and are asserted, not hoped for:

* **Nothing disappears.** ``input rows == standardized rows + quarantined
  rows``. A row we cannot make sense of is kept with a named reason; it is never
  dropped, because a silently shorter output table is the most dangerous
  possible failure -- it looks like success.
* **A suggestion is not a decision.** ``engine_suggested_loinc`` and
  ``approved_current_loinc`` are different columns and never assigned from one
  another. A code only becomes approved when a person has approved it, and it is
  re-checked against the current release at that moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.constants import (
    INTERPRETATION_ABNORMAL,
    Decision,
    QualityStatus,
    ResultIssue,
    ReviewStatus,
    TerminologySystem,
    ValueType,
)
from backend.app.models import (
    LocalMapping,
    LoincConceptVersion,
    SourceLabItem,
    SourceLabResult,
    StandardizationIssue,
    StandardizationRun,
    StandardizedLabObservation,
)
from backend.app.services import release_service
from backend.app.services.categorical_normalizer import CategoricalNormalizer
from backend.app.services.loinc_resolver import LoincResolver
from backend.app.services.result_parser import parse_result
from backend.app.services.unit_normalizer import UCUM_SYSTEM, UnitNormalizer
from backend.app.utils.logging import get_logger

log = get_logger("standardizer")

# Issues serious enough that the row must not be used as it stands.
QUARANTINING_ISSUES: frozenset[ResultIssue] = frozenset({
    ResultIssue.UNKNOWN_ITEMID,
    ResultIssue.UNIT_INCOMPATIBLE,
})

# Issues that are worth surfacing but leave the row usable.
_BENIGN: frozenset[ResultIssue] = frozenset({
    ResultIssue.BELOW_DETECTION_LIMIT,
    ResultIssue.ABOVE_DETECTION_LIMIT,
    ResultIssue.TEXT_RESULT,
    ResultIssue.CODE_PENDING_LICENCE,
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RowOutcome:
    """Everything decided about one row, before it is written."""

    observation: StandardizedLabObservation
    issues: list[tuple[ResultIssue, str]] = field(default_factory=list)

    @property
    def quarantined(self) -> bool:
        return self.observation.quality_status == QualityStatus.QUARANTINED.value


class ResultStandardizer:
    """Standardizes rows against the current LOINC release and the rule tables.

    One instance is reused for a whole run: the resolver, the unit table and the
    value table all cache, which is what keeps 76,000 rows from becoming 76,000
    round trips.
    """

    def __init__(
        self,
        session: Session,
        *,
        source_dataset: str,
        resolver: LoincResolver | None = None,
    ) -> None:
        self.session = session
        self.source_dataset = source_dataset
        self.resolver = resolver or LoincResolver(session)
        self.units = UnitNormalizer(session)
        self.categories = CategoricalNormalizer(session)

        self.loinc_release = release_service.get_current(
            session, TerminologySystem.LOINC.value
        )
        self.loinc_version = self.loinc_release.version if self.loinc_release else None

        self._items: dict[str, SourceLabItem] = {}
        self._approved: dict[str, LocalMapping] = {}
        self._concepts: dict[str, LoincConceptVersion | None] = {}

    # -- lookups ----------------------------------------------------------
    def load_dictionary(self) -> None:
        """Cache the test dictionary and any human-approved mappings.

        An approved mapping is what lets a stale dictionary code be superseded:
        the dictionary still says what it always said, and the approval sits
        beside it rather than overwriting it.
        """
        self._items = {
            item.itemid: item
            for item in self.session.scalars(
                select(SourceLabItem).where(
                    SourceLabItem.source_dataset == self.source_dataset
                )
            )
        }
        self._approved = {
            m.local_code: m
            for m in self.session.scalars(
                select(LocalMapping).where(
                    LocalMapping.source_dataset == self.source_dataset,
                    LocalMapping.target_system == TerminologySystem.LOINC.value,
                    LocalMapping.review_status == ReviewStatus.APPROVED.value,
                )
            )
        }
        log.info(
            "loaded %d dictionary items and %d human-approved mappings for %s",
            len(self._items), len(self._approved), self.source_dataset,
        )

    def concept(self, code: str | None) -> LoincConceptVersion | None:
        """The LOINC concept for a code in the current release, cached."""
        if not code:
            return None
        if code not in self._concepts:
            self._concepts[code] = self.resolver.get_concept(code)
        return self._concepts[code]

    # -- the work ---------------------------------------------------------
    def standardize_row(self, row: SourceLabResult, run_id: int) -> RowOutcome:
        """Standardize one raw result. Never raises for bad data; records it."""
        obs = StandardizedLabObservation(
            standardization_run_id=run_id,
            source_dataset=row.source_dataset,
            source_row_id=row.source_row_id,
            subject_key=row.subject_key,
            encounter_key=row.encounter_key,
            itemid=row.itemid,
            charttime=row.charttime,
            raw_value=row.raw_value,
            raw_numeric_value=row.raw_numeric_value,
            raw_unit=row.raw_unit,
            raw_flag=row.raw_flag,
            current_loinc_version=self.loinc_version,
        )
        outcome = RowOutcome(observation=obs)

        def note(issue: ResultIssue, detail: str) -> None:
            outcome.issues.append((issue, detail))

        # -- 1. which test is this? ---------------------------------------
        item = self._items.get(row.itemid)
        if item is None:
            note(
                ResultIssue.UNKNOWN_ITEMID,
                f"itemid {row.itemid!r} is not in the {self.source_dataset} dictionary, "
                f"so there is no way to say what test this result belongs to.",
            )
            obs.quality_status = QualityStatus.QUARANTINED.value
            obs.issues_json = [i.value for i, _ in outcome.issues]
            return outcome

        obs.source_label = item.label
        obs.source_fluid = item.fluid
        obs.source_category = item.category
        obs.original_loinc_code = item.original_loinc_code

        # -- 2. is that code still the right code? ------------------------
        approved_mapping = self._approved.get(row.itemid)
        code_to_resolve = (
            approved_mapping.target_code if approved_mapping else item.original_loinc_code
        )
        obs.mapped_against_version = (
            approved_mapping.mapped_against_version if approved_mapping else None
        )

        if not code_to_resolve:
            note(
                ResultIssue.NO_LOINC_MAPPING,
                f"No LOINC code has ever been assigned to {item.label!r}. The result is "
                f"still standardized -- value, unit and time are all usable -- but it "
                f"cannot be compared with another system until somebody maps the test.",
            )
        else:
            resolution = self.resolver.resolve(code_to_resolve)
            obs.resolver_decision = resolution.decision.value

            concept = self.concept(code_to_resolve)
            if concept is not None:
                obs.loinc_component = concept.component
                obs.loinc_property = concept.property
                obs.loinc_time_aspect = concept.time_aspect
                obs.loinc_system = concept.system
                obs.loinc_scale = concept.scale_type
                obs.loinc_method = concept.method_type

            if resolution.decision is Decision.KEEP:
                obs.approved_current_loinc = code_to_resolve
            elif resolution.decision is Decision.KEEP_WITH_WARNING:
                obs.approved_current_loinc = code_to_resolve
                note(
                    ResultIssue.LOINC_TRIAL,
                    f"{code_to_resolve} is published but provisional (TRIAL). Usable, "
                    f"and worth knowing that it may change.",
                )
            elif resolution.decision is Decision.SUGGEST_REPLACEMENT:
                # The suggestion is recorded and deliberately not promoted.
                usable = [t for t in resolution.suggested_targets if t.usable]
                obs.engine_suggested_loinc = usable[0].code if usable else None
                note(
                    ResultIssue.LOINC_NOT_APPROVED,
                    f"{code_to_resolve} should no longer be used. The terminology names "
                    f"{obs.engine_suggested_loinc} as its successor, which is recorded as a "
                    f"suggestion only -- no approved code is set until a person approves it.",
                )
            elif resolution.decision is Decision.UNKNOWN_CODE:
                note(
                    ResultIssue.LOINC_UNKNOWN_CODE,
                    f"{code_to_resolve} is not in LOINC {self.loinc_version}. No approved "
                    f"code can be set for this result.",
                )
            else:  # MANUAL_REVIEW
                note(
                    ResultIssue.LOINC_NOT_APPROVED,
                    f"{code_to_resolve} needs a human decision "
                    f"({resolution.reason.value if resolution.reason else 'ambiguous'}). "
                    f"The result is still standardized; it just carries no approved code.",
                )

            # An approval only counts if it still holds today.
            if approved_mapping and obs.approved_current_loinc:
                obs.mapping_revision_id = None  # set by the approval path, not here

        # -- 3. what kind of answer should this carry, and what does it say?
        parsed = parse_result(
            row.raw_value, row.raw_numeric_value, scale=obs.loinc_scale
        )
        obs.value_type = parsed.value_type.value
        obs.comparator = parsed.comparator.value if parsed.comparator else None
        obs.data_absent_reason = (
            parsed.data_absent_reason.value if parsed.data_absent_reason else None
        )
        for issue in parsed.issues:
            detail = next(
                (n for n in parsed.notes if n), f"{issue.value} while reading the result."
            )
            note(issue, detail)

        # -- 4. the value itself ------------------------------------------
        if parsed.value_type is ValueType.QUANTITY:
            unit = self.units.normalize(
                row.raw_unit,
                parsed.numeric_value,
                loinc_code=obs.approved_current_loinc or obs.original_loinc_code,
                loinc_property=obs.loinc_property,
            )
            obs.standard_numeric_value = unit.numeric_value
            obs.standard_ucum_unit = unit.ucum_code
            obs.unit_status = unit.status.value
            obs.unit_rule_id = unit.rule_id
            for issue in unit.issues:
                note(issue, unit.notes[0] if unit.notes else issue.value)

        elif parsed.value_type in (ValueType.CODEABLE_CONCEPT, ValueType.STRING):
            category = self.categories.normalize(
                parsed.text_value, loinc_code=obs.approved_current_loinc or obs.original_loinc_code
            )
            obs.normalized_text_value = category.normalized_display
            obs.coded_value_system = category.target_system
            obs.coded_value_code = category.target_code
            obs.coded_value_display = category.normalized_display
            obs.value_mapping_status = category.status.value
            obs.value_rule_id = category.rule_id
            # A process state resolves to absence, which overrides the parse.
            if category.value_type is ValueType.ABSENT:
                obs.value_type = ValueType.ABSENT.value
                obs.data_absent_reason = (
                    category.data_absent_reason.value if category.data_absent_reason else None
                )
            elif category.value_type is ValueType.STRING:
                obs.value_type = ValueType.STRING.value
            for issue in category.issues:
                note(issue, category.notes[0] if category.notes else issue.value)

        # -- 5. the abnormal flag -----------------------------------------
        # MIMIC's FLAG only ever says "abnormal". An empty FLAG means nothing was
        # recorded -- it emphatically does not mean the result was normal, and
        # writing "N" here would be inventing a clinical judgement.
        flag = (row.raw_flag or "").strip().lower()
        if flag == "abnormal":
            obs.interpretation_code = INTERPRETATION_ABNORMAL
        elif flag == "delta":
            # A significant change from the previous result. Not an
            # interpretation of normality, so it is not mapped to one.
            obs.interpretation_code = None
        else:
            obs.interpretation_code = None

        # -- 6. how much do we trust the row? -----------------------------
        codes = [i for i, _ in outcome.issues]
        if any(i in QUARANTINING_ISSUES for i in codes):
            obs.quality_status = QualityStatus.QUARANTINED.value
        elif any(i not in _BENIGN for i in codes):
            obs.quality_status = QualityStatus.WARNING.value
        else:
            obs.quality_status = QualityStatus.OK.value

        obs.issues_json = [i.value for i in codes]
        return outcome


def run_standardization(
    session: Session,
    *,
    source_dataset: str,
    limit: int | None = None,
    batch_size: int = 2000,
) -> StandardizationRun:
    """Standardize every stored raw result for one dataset.

    The row-conservation invariant is checked before the run is marked
    complete: if the numbers do not add up the run fails loudly rather than
    publishing a table that quietly lost something.
    """
    loinc = release_service.get_current(session, TerminologySystem.LOINC.value)
    snomed = release_service.get_current(session, TerminologySystem.SNOMED_CT.value)

    run = StandardizationRun(
        source_dataset=source_dataset,
        loinc_version=loinc.version if loinc else None,
        snomed_version=snomed.version if snomed else None,
        unit_rule_version="seed-1",
        value_rule_version="seed-1",
        status="RUNNING",
    )
    session.add(run)
    session.flush()

    standardizer = ResultStandardizer(session, source_dataset=source_dataset)
    standardizer.load_dictionary()

    stmt = (
        select(SourceLabResult)
        .where(SourceLabResult.source_dataset == source_dataset)
        .order_by(SourceLabResult.id)
    )
    if limit:
        stmt = stmt.limit(limit)

    # Preload every code the dictionary mentions, so the resolver batches.
    codes = sorted({
        i.original_loinc_code for i in standardizer._items.values() if i.original_loinc_code
    })
    if codes:
        standardizer.resolver.preload(codes)

    total = standardized = quarantined = 0
    issue_counts: dict[str, int] = {}
    pending: list[StandardizedLabObservation] = []
    pending_issues: list[StandardizationIssue] = []

    for row in session.scalars(stmt).yield_per(batch_size):
        total += 1
        outcome = standardizer.standardize_row(row, run.id)
        pending.append(outcome.observation)
        if outcome.quarantined:
            quarantined += 1
        else:
            standardized += 1
        for issue, detail in outcome.issues:
            issue_counts[issue.value] = issue_counts.get(issue.value, 0) + 1
            pending_issues.append(
                StandardizationIssue(
                    standardization_run_id=run.id,
                    source_dataset=source_dataset,
                    source_row_id=row.source_row_id,
                    itemid=row.itemid,
                    issue_code=issue.value,
                    severity=(
                        "QUARANTINE" if issue in QUARANTINING_ISSUES
                        else "INFO" if issue in _BENIGN else "WARNING"
                    ),
                    detail=detail,
                )
            )
        if len(pending) >= batch_size:
            session.add_all(pending)
            session.add_all(pending_issues)
            session.flush()
            pending.clear()
            pending_issues.clear()

    if pending:
        session.add_all(pending)
        session.add_all(pending_issues)
        session.flush()

    run.input_rows = total
    run.standardized_rows = standardized
    run.quarantined_rows = quarantined
    run.completed_at = utcnow()

    if not run.rows_accounted_for:
        run.status = "FAILED"
        run.error_message = (
            f"Row conservation failed: {total} in, {standardized} standardized, "
            f"{quarantined} quarantined. Something was lost, which is never acceptable."
        )
        session.flush()
        raise RuntimeError(run.error_message)

    run.status = "COMPLETED"
    run.summary_json = _summarise(session, run, issue_counts)
    session.flush()

    log.info(
        "standardization run %d: %d rows in, %d standardized, %d quarantined",
        run.id, total, standardized, quarantined,
    )
    return run


def _summarise(
    session: Session, run: StandardizationRun, issue_counts: dict[str, int]
) -> dict:
    """Headline numbers, computed from what was written rather than counted along the way."""
    from sqlalchemy import func

    def count_where(*conditions) -> int:
        stmt = select(func.count()).select_from(StandardizedLabObservation).where(
            StandardizedLabObservation.standardization_run_id == run.id, *conditions
        )
        return session.scalar(stmt) or 0

    total = run.input_rows or 1
    by_value_type = {
        vt: count_where(StandardizedLabObservation.value_type == vt)
        for vt in ("QUANTITY", "CODEABLE_CONCEPT", "STRING", "ABSENT")
    }
    # A row quarantined before it could be read -- an itemid with no dictionary
    # entry -- never gets a value type. Counting it explicitly keeps the
    # breakdown adding up to the total, so a reader can trust that every row is
    # somewhere in this table.
    by_value_type["UNDETERMINED"] = count_where(
        StandardizedLabObservation.value_type.is_(None)
    )
    with_loinc = count_where(StandardizedLabObservation.original_loinc_code.is_not(None))
    approved = count_where(StandardizedLabObservation.approved_current_loinc.is_not(None))
    ucum = count_where(StandardizedLabObservation.standard_ucum_unit.is_not(None))
    numeric = by_value_type["QUANTITY"]

    return {
        "input_rows": run.input_rows,
        "standardized_rows": run.standardized_rows,
        "quarantined_rows": run.quarantined_rows,
        "rows_accounted_for": run.rows_accounted_for,
        "by_value_type": by_value_type,
        "loinc_coverage": with_loinc,
        "loinc_coverage_rate": round(with_loinc / total, 4),
        "approved_loinc_coverage": approved,
        "approved_loinc_rate": round(approved / total, 4),
        "ucum_coverage": ucum,
        # Of the rows that carry a number, how many ended with a UCUM unit.
        "ucum_rate_of_numeric": round(ucum / numeric, 4) if numeric else None,
        "quality": {
            status: count_where(StandardizedLabObservation.quality_status == status)
            for status in ("OK", "WARNING", "QUARANTINED")
        },
        "quarantine_rate": round(run.quarantined_rows / total, 4),
        "issues": dict(sorted(issue_counts.items(), key=lambda kv: -kv[1])),
        "ucum_system": UCUM_SYSTEM,
    }


__all__ = ["QUARANTINING_ISSUES", "ResultStandardizer", "RowOutcome", "run_standardization"]
