"""The audit engine: replay every stored mapping against the current releases.

This is the project's real deliverable.  It answers, for a whole mapping set at
once: *which of our existing standardizations are still valid today, which have
an official successor, and which need a human?*

The summary deliberately reports an **abstention rate** alongside the usual
counts.  Selective prediction (Swaminathan et al., JAMIA 2024) frames exactly
this trade-off: a system that may decline is more useful than one forced to
answer, provided it reports how often it declined and why.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.constants import (
    AuditRunStatus,
    Decision,
    Reason,
    ReviewStatus,
    TerminologyStatus,
    TerminologySystem,
)
from backend.app.models import AuditResult, AuditRun, LocalMapping
from backend.app.services import release_service
from backend.app.services.loinc_resolver import LoincResolver
from backend.app.services.snomed_resolver import SnomedResolver
from backend.app.utils.logging import get_logger

log = get_logger("audit")

CSV_COLUMNS = [
    "mapping_id",
    "source_dataset",
    "local_code",
    "local_text",
    "target_system",
    "old_code",
    "target_display",
    "mapped_against_version",
    "current_version",
    "terminology_status",
    "decision",
    "reason",
    "suggested_targets",
    "metadata_changed",
    "notes",
]


@dataclass
class AuditSummary:
    """Counts reported for one audit run (Master Instruction 27 and 48)."""

    total_mappings: int = 0
    by_system: dict[str, int] = field(default_factory=dict)
    valid: int = 0
    trial_warning: int = 0
    discouraged: int = 0
    deprecated: int = 0
    inactive_snomed: int = 0
    unknown: int = 0
    single_replacement: int = 0
    multiple_replacement: int = 0
    no_replacement: int = 0
    manual_review_required: int = 0
    metadata_changed: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def abstention_rate(self) -> float:
        """Fraction of mappings routed to a human rather than auto-answered."""
        if not self.total_mappings:
            return 0.0
        return round(self.manual_review_required / self.total_mappings, 4)

    def as_dict(self) -> dict:
        data = {
            "total_mappings": self.total_mappings,
            "by_system": self.by_system,
            "valid": self.valid,
            "trial_warning": self.trial_warning,
            "discouraged": self.discouraged,
            "deprecated": self.deprecated,
            "inactive_snomed": self.inactive_snomed,
            "unknown": self.unknown,
            "single_replacement": self.single_replacement,
            "multiple_replacement": self.multiple_replacement,
            "no_replacement": self.no_replacement,
            "manual_review_required": self.manual_review_required,
            "metadata_changed": self.metadata_changed,
            "abstention_rate": self.abstention_rate,
            "decisions": self.decisions,
            "reasons": self.reasons,
        }
        return data


def _select_mappings(
    session: Session,
    *,
    source_dataset: str | None,
    target_system: str | None,
    limit: int | None,
) -> list[LocalMapping]:
    stmt = select(LocalMapping).order_by(LocalMapping.id)
    if source_dataset:
        stmt = stmt.where(LocalMapping.source_dataset == source_dataset)
    if target_system:
        stmt = stmt.where(LocalMapping.target_system == target_system)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def run_audit(
    session: Session,
    *,
    source_dataset: str | None = None,
    target_system: str | None = None,
    limit: int | None = None,
    export_csv: bool = True,
    report_name: str | None = None,
    mark_review_status: bool = True,
) -> AuditRun:
    """Audit every selected mapping against the current LOINC/SNOMED releases."""
    loinc_release = release_service.get_current(session, TerminologySystem.LOINC.value)
    snomed_release = release_service.get_current(
        session, TerminologySystem.SNOMED_CT.value
    )

    run = AuditRun(
        loinc_version=loinc_release.version if loinc_release else None,
        snomed_version=snomed_release.version if snomed_release else None,
        status=AuditRunStatus.RUNNING.value,
        scope_json={
            "source_dataset": source_dataset,
            "target_system": target_system,
            "limit": limit,
        },
    )
    session.add(run)
    session.flush()

    log.info(
        "audit run %s started (loinc=%s snomed=%s scope=%s)",
        run.id,
        run.loinc_version,
        run.snomed_version,
        run.scope_json,
    )

    try:
        mappings = _select_mappings(
            session,
            source_dataset=source_dataset,
            target_system=target_system,
            limit=limit,
        )
        run.mapping_count = len(mappings)

        loinc_resolver = LoincResolver(session, release=loinc_release)
        snomed_resolver = SnomedResolver(session, release=snomed_release)

        # Batch preload -- this is what keeps the audit free of N+1 queries.
        loinc_resolver.preload(
            [
                m.target_code
                for m in mappings
                if m.target_system == TerminologySystem.LOINC.value
            ]
        )
        snomed_resolver.preload(
            [
                m.target_code
                for m in mappings
                if m.target_system == TerminologySystem.SNOMED_CT.value
            ]
        )
        # Metadata-drift baselines, grouped by the release each mapping was
        # originally made against, so drift detection is also batched.
        baselines: dict[str, list[str]] = defaultdict(list)
        for mapping in mappings:
            if (
                mapping.target_system == TerminologySystem.LOINC.value
                and mapping.mapped_against_version
                and mapping.mapped_against_version != loinc_resolver.version
            ):
                baselines[mapping.mapped_against_version].append(mapping.target_code)
        for baseline_version, codes in baselines.items():
            loinc_resolver.preload_baseline(baseline_version, codes)

        summary = AuditSummary(total_mappings=len(mappings))
        decisions: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        systems: Counter[str] = Counter()
        rows: list[dict] = []

        for mapping in mappings:
            systems[mapping.target_system] += 1
            result, csv_row = _audit_one(
                session, run, mapping, loinc_resolver, snomed_resolver
            )
            session.add(result)
            rows.append(csv_row)
            decisions[result.decision] += 1
            if result.reason:
                reasons[result.reason] += 1
            _accumulate(summary, result)

            if mark_review_status:
                if result.decision in (
                    Decision.MANUAL_REVIEW.value,
                    Decision.SUGGEST_REPLACEMENT.value,
                    Decision.UNKNOWN_CODE.value,
                ):
                    mapping.review_status = ReviewStatus.NEEDS_REVIEW.value

        summary.decisions = dict(decisions)
        summary.reasons = dict(reasons)
        summary.by_system = dict(systems)

        run.summary_json = summary.as_dict()
        run.status = AuditRunStatus.COMPLETED.value
        run.completed_at = datetime.now(timezone.utc)

        if export_csv:
            run.report_path = str(export_audit_csv(run, rows, report_name))

        session.commit()
        log.info(
            "audit run %s completed: %d mappings, %d need review (abstention %.1f%%)",
            run.id,
            summary.total_mappings,
            summary.manual_review_required,
            summary.abstention_rate * 100,
        )
        return run
    except Exception as exc:  # noqa: BLE001 - recorded, re-raised (Hard Rule 15)
        run.status = AuditRunStatus.FAILED.value
        run.error_message = f"{type(exc).__name__}: {exc}"
        run.completed_at = datetime.now(timezone.utc)
        session.commit()
        log.exception("audit run %s failed", run.id)
        raise


def _audit_one(
    session: Session,
    run: AuditRun,
    mapping: LocalMapping,
    loinc_resolver: LoincResolver,
    snomed_resolver: SnomedResolver,
) -> tuple[AuditResult, dict]:
    if mapping.target_system == TerminologySystem.LOINC.value:
        resolution = loinc_resolver.resolve(
            mapping.target_code,
            mapped_against_version=mapping.mapped_against_version,
        )
        payload = resolution.as_dict()
        suggested = payload["suggested_targets"]
        metadata_json = {
            "raw_status": resolution.raw_status,
            "display": resolution.display,
            "metadata_changed": resolution.metadata_changed,
            "metadata_diff": resolution.metadata_diff,
            "mapped_against_version": mapping.mapped_against_version,
            "map_correlation": mapping.map_correlation,
            "details": resolution.details,
        }
        current_version = loinc_resolver.version
        status = resolution.status
        decision = resolution.decision
        reason = resolution.reason
        display = resolution.display
        metadata_changed = bool(resolution.metadata_changed)
    else:
        resolution = snomed_resolver.resolve(mapping.target_code)
        payload = resolution.as_dict()
        suggested = payload["suggested_targets"]
        metadata_json = {
            "active": resolution.active,
            "display": resolution.display,
            "inactivation_reason": resolution.inactivation_reason,
            "inactivation_value_id": resolution.inactivation_value_id,
            "historical_associations": payload["historical_associations"],
            "mapped_against_version": mapping.mapped_against_version,
            "map_correlation": mapping.map_correlation,
            "details": resolution.details,
        }
        current_version = snomed_resolver.version
        status = resolution.status
        decision = resolution.decision
        reason = resolution.reason
        display = resolution.display
        metadata_changed = False

    result = AuditResult(
        audit_run_id=run.id,
        mapping_id=mapping.id,
        target_system=mapping.target_system,
        old_code=mapping.target_code,
        current_version=current_version,
        terminology_status=status.value,
        decision=decision.value,
        suggested_targets_json=suggested,
        reason=reason.value if reason else None,
        metadata_json=metadata_json,
    )

    notes = metadata_json.get("details", {}) or {}
    csv_row = {
        "mapping_id": mapping.id,
        "source_dataset": mapping.source_dataset,
        "local_code": mapping.local_code,
        "local_text": mapping.local_text,
        "target_system": mapping.target_system,
        "old_code": mapping.target_code,
        "target_display": display or mapping.target_display or "",
        "mapped_against_version": mapping.mapped_against_version or "",
        "current_version": current_version or "",
        "terminology_status": status.value,
        "decision": decision.value,
        "reason": reason.value if reason else "",
        "suggested_targets": ";".join(
            str(t.get("code") or t.get("concept_id") or "") for t in suggested
        ),
        "metadata_changed": "yes" if metadata_changed else "",
        "notes": notes.get("message") or notes.get("warning") or "",
    }
    return result, csv_row


def _accumulate(summary: AuditSummary, result: AuditResult) -> None:
    status = result.terminology_status
    decision = result.decision
    reason = result.reason

    if status == TerminologyStatus.CURRENT_VALID.value:
        summary.valid += 1
    elif status == TerminologyStatus.CURRENT_TRIAL.value:
        summary.trial_warning += 1
    elif status == TerminologyStatus.DISCOURAGED.value:
        summary.discouraged += 1
    elif status == TerminologyStatus.DEPRECATED.value:
        summary.deprecated += 1
    elif status == TerminologyStatus.INACTIVE.value:
        summary.inactive_snomed += 1
    else:
        summary.unknown += 1

    if decision == Decision.SUGGEST_REPLACEMENT.value:
        summary.single_replacement += 1
    if decision in (Decision.MANUAL_REVIEW.value, Decision.UNKNOWN_CODE.value):
        summary.manual_review_required += 1
    if reason == Reason.MULTIPLE_REPLACEMENTS.value:
        summary.multiple_replacement += 1
    if reason in (
        Reason.NO_OFFICIAL_REPLACEMENT.value,
        Reason.NO_HISTORICAL_ASSOCIATION.value,
    ):
        summary.no_replacement += 1

    if (result.metadata_json or {}).get("metadata_changed"):
        summary.metadata_changed += 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def export_audit_csv(
    run: AuditRun, rows: list[dict], report_name: str | None = None
) -> Path:
    reports_dir = settings.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    name = report_name or f"audit_run_{run.id}.csv"
    path = reports_dir / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("audit run %s CSV written to %s", run.id, path)
    return path


def render_report(run: AuditRun) -> str:
    """Human-readable report (Master Instruction 48)."""
    summary = run.summary_json or {}
    lines = [
        "Terminology Audit Report",
        "========================",
        "",
        f"Audit run id:            {run.id}",
        f"Started:                 {run.started_at}",
        f"Completed:               {run.completed_at}",
        f"Status:                  {run.status}",
        "",
        f"LOINC current version:   {run.loinc_version or '(none imported)'}",
        f"SNOMED current version:  {run.snomed_version or '(none imported)'}",
        "",
        f"Mappings audited:        {summary.get('total_mappings', 0)}",
        "",
        f"Valid:                   {summary.get('valid', 0)}",
        f"Warnings (TRIAL):        {summary.get('trial_warning', 0)}",
        f"Discouraged:             {summary.get('discouraged', 0)}",
        f"Deprecated:              {summary.get('deprecated', 0)}",
        f"Inactive SNOMED:         {summary.get('inactive_snomed', 0)}",
        f"Unknown:                 {summary.get('unknown', 0)}",
        "",
        f"Single replacement:      {summary.get('single_replacement', 0)}",
        f"Ambiguous replacements:  {summary.get('multiple_replacement', 0)}",
        f"No replacements:         {summary.get('no_replacement', 0)}",
        f"Metadata changed:        {summary.get('metadata_changed', 0)}",
        "",
        f"Manual review required:  {summary.get('manual_review_required', 0)}",
        f"Abstention rate:         {summary.get('abstention_rate', 0.0) * 100:.1f}%",
    ]
    if run.report_path:
        lines += ["", f"CSV report:              {run.report_path}"]
    if run.error_message:
        lines += ["", f"ERROR: {run.error_message}"]
    return "\n".join(lines)


def get_run(session: Session, run_id: int) -> AuditRun | None:
    return session.get(AuditRun, run_id)


def list_runs(session: Session, limit: int = 50) -> list[AuditRun]:
    return list(
        session.scalars(
            select(AuditRun).order_by(AuditRun.id.desc()).limit(limit)
        )
    )


def list_results(
    session: Session,
    run_id: int,
    *,
    decision: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[AuditResult]:
    stmt = (
        select(AuditResult)
        .where(AuditResult.audit_run_id == run_id)
        .order_by(AuditResult.id)
    )
    if decision:
        stmt = stmt.where(AuditResult.decision == decision)
    return list(session.scalars(stmt.limit(limit).offset(offset)))


def summary_json(run: AuditRun) -> str:
    return json.dumps(run.summary_json or {}, indent=2)


__all__ = [
    "AuditSummary",
    "CSV_COLUMNS",
    "export_audit_csv",
    "get_run",
    "list_results",
    "list_runs",
    "render_report",
    "run_audit",
]
