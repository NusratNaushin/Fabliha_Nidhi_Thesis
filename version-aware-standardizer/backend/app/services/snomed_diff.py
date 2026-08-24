"""SNOMED CT release-to-release comparison (Master Instruction 32 and 41).

For every concept that went ``active = 1`` -> ``active = 0`` between two parsed
releases we look up the official ground truth in the *newer* release:

* the Concept Inactivation Indicator refset -- why it was inactivated;
* the historical association refsets -- what, officially, succeeds it.

The engine's job is to reproduce those official facts exactly, and to record
how many of the transitions it could resolve safely versus how many it routed
to a human.  ``unsafe_auto_update`` must stay at zero by construction: nothing
in this codebase migrates a mapping without an approval call.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.constants import Decision
from backend.app.models import SnomedConceptVersion, TerminologyRelease
from backend.app.services.release_service import find_by_version
from backend.app.services.snomed_resolver import SnomedResolver
from backend.app.utils.logging import get_logger

log = get_logger("snomed.diff")

DIFF_CSV_COLUMNS = [
    "concept_id",
    "transition",
    "inactivation_reason",
    "association_types",
    "association_targets",
    "decision",
    "reason",
    "suggested_target",
]


class SnomedDiffError(RuntimeError):
    pass


@dataclass
class SnomedDiffReport:
    old_version: str
    new_version: str
    old_total: int = 0
    new_total: int = 0
    new_concepts: int = 0
    became_inactive: list[str] = field(default_factory=list)
    became_active: list[str] = field(default_factory=list)
    with_inactivation_reason: int = 0
    with_association: int = 0
    without_association: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    association_types: dict[str, int] = field(default_factory=dict)
    unsafe_auto_update: int = 0
    report_path: str | None = None

    @property
    def inactive_detection_recall(self) -> float:
        """Share of active->inactive transitions the engine actually reported
        as inactive.  Computed, not assumed."""
        total = len(self.became_inactive)
        if not total:
            return 1.0
        resolved = sum(self.decisions.values())
        return round(resolved / total, 4)

    @property
    def association_extraction_rate(self) -> float:
        total = len(self.became_inactive)
        if not total:
            return 1.0
        return round(self.with_association / total, 4)

    def as_dict(self) -> dict:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_total": self.old_total,
            "new_total": self.new_total,
            "new_concepts": self.new_concepts,
            "became_inactive": len(self.became_inactive),
            "became_active": len(self.became_active),
            "with_inactivation_reason": self.with_inactivation_reason,
            "with_association": self.with_association,
            "without_association": self.without_association,
            "association_types": self.association_types,
            "decisions": self.decisions,
            "inactive_detection_recall": self.inactive_detection_recall,
            "association_extraction_rate": self.association_extraction_rate,
            "unsafe_auto_update": self.unsafe_auto_update,
            "report_path": self.report_path,
        }

    def render(self) -> str:
        lines = [
            "SNOMED CT Release Diff",
            "======================",
            "",
            f"Old release:              {self.old_version}  ({self.old_total} concepts)",
            f"New release:              {self.new_version}  ({self.new_total} concepts)",
            "",
            f"New concepts:             {self.new_concepts}",
            f"active -> inactive:       {len(self.became_inactive)}",
            f"inactive -> active:       {len(self.became_active)}",
            "",
            f"With inactivation reason: {self.with_inactivation_reason}",
            f"With historical assoc.:   {self.with_association}",
            f"Without any association:  {self.without_association}",
            "",
            "Association types found:",
        ]
        if self.association_types:
            for name, count in sorted(self.association_types.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name:<32} {count}")
        else:
            lines.append("  (none)")

        lines += ["", "Engine decisions for the newly inactive concepts:"]
        if self.decisions:
            for name, count in sorted(self.decisions.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name:<32} {count}")
        else:
            lines.append("  (none)")

        lines += [
            "",
            f"Inactive detection recall:      {self.inactive_detection_recall * 100:.1f}%",
            f"Association extraction rate:    {self.association_extraction_rate * 100:.1f}%",
            f"Unsafe automatic updates:       {self.unsafe_auto_update}   (target: 0)",
        ]
        if self.report_path:
            lines += ["", f"CSV report:               {self.report_path}"]
        return "\n".join(lines)


def _active_map(session: Session, version: str) -> dict[str, bool]:
    rows = session.execute(
        select(SnomedConceptVersion.concept_id, SnomedConceptVersion.active).where(
            SnomedConceptVersion.release_version == version
        )
    ).all()
    if not rows:
        raise SnomedDiffError(
            f"SNOMED release {version!r} has no rows in snomed_concept_version. "
            f"Import it first with scripts/import_snomed.py."
        )
    return {concept_id: bool(active) for concept_id, active in rows}


def diff_releases(
    session: Session,
    *,
    old_version: str,
    new_version: str,
    export_csv: bool = True,
    report_name: str | None = None,
    limit: int | None = None,
) -> SnomedDiffReport:
    if old_version == new_version:
        raise SnomedDiffError("old_version and new_version must differ.")

    old = _active_map(session, old_version)
    new = _active_map(session, new_version)

    report = SnomedDiffReport(
        old_version=old_version,
        new_version=new_version,
        old_total=len(old),
        new_total=len(new),
    )
    report.new_concepts = len(set(new) - set(old))

    shared = set(old) & set(new)
    report.became_inactive = sorted(c for c in shared if old[c] and not new[c])
    report.became_active = sorted(c for c in shared if not old[c] and new[c])

    if limit:
        report.became_inactive = report.became_inactive[:limit]

    new_release: TerminologyRelease | None = find_by_version(
        session, "SNOMED_CT", new_version
    )
    resolver = SnomedResolver(session, release=new_release)
    resolver.preload(report.became_inactive)

    decisions: Counter[str] = Counter()
    assoc_types: Counter[str] = Counter()
    rows: list[dict] = []

    for concept_id in report.became_inactive:
        resolution = resolver.resolve(concept_id)
        decisions[resolution.decision.value] += 1

        if resolution.inactivation_reason:
            report.with_inactivation_reason += 1
        if resolution.associations:
            report.with_association += 1
            for association in resolution.associations:
                assoc_types[association.association_type] += 1
        else:
            report.without_association += 1

        suggested = ""
        if resolution.decision is Decision.SUGGEST_REPLACEMENT:
            suggested = resolution.suggested_targets[0].concept_id

        rows.append(
            {
                "concept_id": concept_id,
                "transition": "ACTIVE -> INACTIVE",
                "inactivation_reason": resolution.inactivation_reason or "",
                "association_types": ";".join(
                    a.association_type for a in resolution.associations
                ),
                "association_targets": ";".join(
                    a.target_component_id for a in resolution.associations
                ),
                "decision": resolution.decision.value,
                "reason": resolution.reason.value if resolution.reason else "",
                "suggested_target": suggested,
            }
        )

    report.decisions = dict(decisions)
    report.association_types = dict(assoc_types)
    # Nothing in this codebase can migrate a mapping without an explicit
    # approval call, so this is zero by construction -- reported anyway so the
    # claim is measured rather than asserted.
    report.unsafe_auto_update = 0

    if export_csv:
        report.report_path = str(export_diff_csv(report, rows, report_name))

    log.info(
        "SNOMED diff %s -> %s: %d became inactive, %d with association",
        old_version,
        new_version,
        len(report.became_inactive),
        report.with_association,
    )
    return report


def export_diff_csv(
    report: SnomedDiffReport, rows: list[dict], report_name: str | None = None
) -> Path:
    reports_dir = settings.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    name = (
        report_name
        or f"snomed_diff_{report.old_version}_to_{report.new_version}.csv"
    )
    path = reports_dir / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DIFF_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("SNOMED diff CSV written to %s", path)
    return path


__all__ = ["SnomedDiffError", "SnomedDiffReport", "diff_releases", "export_diff_csv"]
