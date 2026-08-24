"""Independent LOINC release-to-release diff (Master Instruction 31).

Two things happen here, and the second is the scientifically interesting one:

1. We compute our *own* diff between two imported releases, purely from
   ``loinc_concept_version`` rows.
2. We then check that diff against the official ``LoincChangeSnapshot.csv`` of
   the newer release.  The target is **zero missed official changes** for the
   fields we support -- that is the primary correctness evidence for the LOINC
   update engine, and it is evidence that does not depend on anybody's opinion,
   only on two official files.

The literature motivates step 2: the 2026 systematic review of automatic
terminology mapping found that published methods routinely under-report
validation, so an engine that can be checked against an official change log is
worth more than one evaluated only on hand-picked examples.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import LoincChange, LoincConceptVersion
from backend.app.utils.logging import get_logger

log = get_logger("loinc.diff")

# Our model field -> the property token used by LoincChangeSnapshot.csv.
FIELD_TO_OFFICIAL_PROPERTY: dict[str, str] = {
    "status": "STATUS",
    "long_common_name": "LONG_COMMON_NAME",
    "short_name": "SHORTNAME",
    "component": "COMPONENT",
    "property": "PROPERTY",
    "time_aspect": "TIME_ASPCT",
    "system": "SYSTEM",
    "scale_type": "SCALE_TYP",
    "method_type": "METHOD_TYP",
    "class_name": "CLASS",
}
OFFICIAL_PROPERTY_TO_FIELD = {v: k for k, v in FIELD_TO_OFFICIAL_PROPERTY.items()}

COMPARED_FIELDS: tuple[str, ...] = tuple(FIELD_TO_OFFICIAL_PROPERTY)

DIFF_CSV_COLUMNS = [
    "loinc_num",
    "change_kind",
    "field",
    "value_prior",
    "value_current",
]


class DiffError(RuntimeError):
    pass


@dataclass
class FieldChange:
    loinc_num: str
    field: str
    value_prior: str | None
    value_current: str | None

    @property
    def official_property(self) -> str:
        return FIELD_TO_OFFICIAL_PROPERTY[self.field]


@dataclass
class ValidationReport:
    """How our computed diff compares with the official Change Snapshot."""

    official_changes: int = 0
    detected_changes: int = 0
    matched_changes: int = 0
    missed_changes: list[dict] = field(default_factory=list)
    unexpected_changes: int = 0
    unsupported_official_properties: dict[str, int] = field(default_factory=dict)
    change_snapshot_available: bool = True

    @property
    def missed_count(self) -> int:
        return len(self.missed_changes)

    def as_dict(self) -> dict:
        return {
            "change_snapshot_available": self.change_snapshot_available,
            "official_changes": self.official_changes,
            "detected_changes": self.detected_changes,
            "matched_changes": self.matched_changes,
            "missed_changes": self.missed_count,
            "missed_changes_sample": self.missed_changes[:25],
            "unexpected_changes": self.unexpected_changes,
            "unsupported_official_properties": self.unsupported_official_properties,
        }


@dataclass
class LoincDiffReport:
    old_version: str
    new_version: str
    old_total: int = 0
    new_total: int = 0
    new_codes: list[str] = field(default_factory=list)
    removed_codes: list[str] = field(default_factory=list)
    changes: list[FieldChange] = field(default_factory=list)
    status_transitions: dict[str, int] = field(default_factory=dict)
    changes_by_field: dict[str, int] = field(default_factory=dict)
    validation: ValidationReport = field(default_factory=ValidationReport)
    report_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_total": self.old_total,
            "new_total": self.new_total,
            "new_codes": len(self.new_codes),
            "new_codes_sample": self.new_codes[:25],
            "removed_codes": len(self.removed_codes),
            "removed_codes_sample": self.removed_codes[:25],
            "changed_codes": len({c.loinc_num for c in self.changes}),
            "total_field_changes": len(self.changes),
            "changes_by_field": self.changes_by_field,
            "status_transitions": self.status_transitions,
            "validation": self.validation.as_dict(),
            "report_path": self.report_path,
        }

    def render(self) -> str:
        v = self.validation
        lines = [
            "LOINC Release Diff",
            "==================",
            "",
            f"Old release:             {self.old_version}  ({self.old_total} codes)",
            f"New release:             {self.new_version}  ({self.new_total} codes)",
            "",
            f"New codes:               {len(self.new_codes)}",
            f"Codes absent in new:     {len(self.removed_codes)}",
            f"Codes with changes:      {len({c.loinc_num for c in self.changes})}",
            f"Field-level changes:     {len(self.changes)}",
            "",
            "Status transitions:",
        ]
        if self.status_transitions:
            for transition, count in sorted(
                self.status_transitions.items(), key=lambda kv: -kv[1]
            ):
                lines.append(f"  {transition:<32} {count}")
        else:
            lines.append("  (none)")

        lines += ["", "Changes by field:"]
        if self.changes_by_field:
            for name, count in sorted(self.changes_by_field.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name:<32} {count}")
        else:
            lines.append("  (none)")

        lines += ["", "Validation against the official Change Snapshot:"]
        if not v.change_snapshot_available:
            lines.append(
                "  LoincChangeSnapshot.csv was not present in the new release; "
                "the computed diff stands alone."
            )
        else:
            lines += [
                f"  official_changes       {v.official_changes}",
                f"  detected_changes       {v.detected_changes}",
                f"  matched_changes        {v.matched_changes}",
                f"  missed_changes         {v.missed_count}   (target: 0)",
                f"  unexpected_changes     {v.unexpected_changes}",
            ]
            if v.unsupported_official_properties:
                lines.append(
                    f"  official properties not modelled: "
                    f"{sorted(v.unsupported_official_properties)}"
                )
        if self.report_path:
            lines += ["", f"CSV report:              {self.report_path}"]
        return "\n".join(lines)


def _load_release(session: Session, version: str) -> dict[str, LoincConceptVersion]:
    rows = session.scalars(
        select(LoincConceptVersion).where(
            LoincConceptVersion.release_version == version
        )
    )
    concepts = {r.loinc_num: r for r in rows}
    if not concepts:
        raise DiffError(
            f"LOINC release {version!r} has no rows in loinc_concept_version. "
            f"Import it first with scripts/import_loinc.py."
        )
    return concepts


def diff_releases(
    session: Session,
    *,
    old_version: str,
    new_version: str,
    export_csv: bool = True,
    report_name: str | None = None,
) -> LoincDiffReport:
    """Compute the diff and validate it against the official Change Snapshot."""
    if old_version == new_version:
        raise DiffError("old_version and new_version must differ.")

    old = _load_release(session, old_version)
    new = _load_release(session, new_version)

    report = LoincDiffReport(
        old_version=old_version,
        new_version=new_version,
        old_total=len(old),
        new_total=len(new),
    )
    report.new_codes = sorted(set(new) - set(old))
    # LOINC never deletes a code, so this list is normally empty; a non-empty
    # list means the older package contained codes the newer one dropped, which
    # is worth surfacing rather than hiding.
    report.removed_codes = sorted(set(old) - set(new))

    status_transitions: Counter[str] = Counter()
    by_field: Counter[str] = Counter()

    for code in sorted(set(old) & set(new)):
        before, after = old[code], new[code]
        for name in COMPARED_FIELDS:
            prior = getattr(before, name)
            current = getattr(after, name)
            if prior == current:
                continue
            report.changes.append(
                FieldChange(
                    loinc_num=code,
                    field=name,
                    value_prior=prior,
                    value_current=current,
                )
            )
            by_field[name] += 1
            if name == "status":
                status_transitions[f"{prior or 'NONE'} -> {current or 'NONE'}"] += 1

    report.changes_by_field = dict(by_field)
    report.status_transitions = dict(status_transitions)
    report.validation = validate_against_change_snapshot(session, report, new_version)

    if export_csv:
        report.report_path = str(export_diff_csv(report, report_name))

    log.info(
        "LOINC diff %s -> %s: %d new, %d changed codes, %d field changes, "
        "%d missed official changes",
        old_version,
        new_version,
        len(report.new_codes),
        len({c.loinc_num for c in report.changes}),
        len(report.changes),
        report.validation.missed_count,
    )
    return report


def validate_against_change_snapshot(
    session: Session, report: LoincDiffReport, new_version: str
) -> ValidationReport:
    """Check the computed diff against LoincChangeSnapshot.csv of the new release."""
    official_rows = list(
        session.scalars(
            select(LoincChange).where(LoincChange.release_version == new_version)
        )
    )
    validation = ValidationReport(detected_changes=len(report.changes))

    if not official_rows:
        validation.change_snapshot_available = False
        log.warning(
            "no LoincChangeSnapshot rows stored for release %s; "
            "diff validation skipped",
            new_version,
        )
        return validation

    detected: set[tuple[str, str]] = {
        (c.loinc_num, c.official_property) for c in report.changes
    }
    new_code_set = set(report.new_codes)

    official: set[tuple[str, str]] = set()
    unsupported: Counter[str] = Counter()
    for row in official_rows:
        prop = (row.property or "").strip().upper()
        if prop not in OFFICIAL_PROPERTY_TO_FIELD:
            unsupported[prop] += 1
            continue
        # A newly created term legitimately has no "prior" state to diff, so
        # its rows are outside the scope of a release-to-release comparison
        # (Master Instruction 16).
        if row.loinc_num in new_code_set:
            continue
        official.add((row.loinc_num, prop))

    matched = official & detected
    missed = official - detected

    validation.official_changes = len(official)
    validation.matched_changes = len(matched)
    validation.unexpected_changes = len(detected - official)
    validation.unsupported_official_properties = dict(unsupported)
    validation.missed_changes = [
        {"loinc_num": code, "property": prop} for code, prop in sorted(missed)
    ]
    return validation


def export_diff_csv(report: LoincDiffReport, report_name: str | None = None) -> Path:
    reports_dir = settings.reports_path
    reports_dir.mkdir(parents=True, exist_ok=True)
    name = (
        report_name
        or f"loinc_diff_{report.old_version}_to_{report.new_version}.csv".replace(
            "/", "-"
        )
    )
    path = reports_dir / name
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DIFF_CSV_COLUMNS)
        writer.writeheader()
        for code in report.new_codes:
            writer.writerow(
                {
                    "loinc_num": code,
                    "change_kind": "NEW_CODE",
                    "field": "",
                    "value_prior": "",
                    "value_current": "",
                }
            )
        for code in report.removed_codes:
            writer.writerow(
                {
                    "loinc_num": code,
                    "change_kind": "ABSENT_IN_NEW_RELEASE",
                    "field": "",
                    "value_prior": "",
                    "value_current": "",
                }
            )
        for change in report.changes:
            writer.writerow(
                {
                    "loinc_num": change.loinc_num,
                    "change_kind": (
                        "STATUS_CHANGE" if change.field == "status" else "FIELD_CHANGE"
                    ),
                    "field": change.official_property,
                    "value_prior": change.value_prior or "",
                    "value_current": change.value_current or "",
                }
            )
    log.info("LOINC diff CSV written to %s", path)
    return path


def status_change_codes(
    session: Session, *, old_version: str, new_version: str
) -> dict[str, list[str]]:
    """Codes whose STATUS changed, grouped by ``OLD -> NEW``.

    This is the generator for the strongest validation experiment (Master
    Instruction 40): treat the older release as the historical mapping set and
    ask the auditor to rediscover exactly these transitions.
    """
    old = _load_release(session, old_version)
    new = _load_release(session, new_version)
    grouped: dict[str, list[str]] = defaultdict(list)
    for code in sorted(set(old) & set(new)):
        before = (old[code].status or "").strip().upper()
        after = (new[code].status or "").strip().upper()
        if before != after:
            grouped[f"{before or 'NONE'} -> {after or 'NONE'}"].append(code)
    return dict(grouped)


__all__ = [
    "COMPARED_FIELDS",
    "DiffError",
    "FieldChange",
    "LoincDiffReport",
    "ValidationReport",
    "diff_releases",
    "export_diff_csv",
    "status_change_codes",
    "validate_against_change_snapshot",
]
