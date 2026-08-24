"""Standardize the stored raw laboratory results.

    python scripts/standardize_mimic_results.py
    python scripts/standardize_mimic_results.py --limit 5000
    python scripts/standardize_mimic_results.py --seed-rules

Takes what ``import_mimic_labevents.py`` stored and turns each row into a
standard observation: the current approved LOINC code where there is one, a
correctly typed value, a UCUM unit, normalised text for categorical results, and
a named reason wherever any of that could not be done.

The run ends by checking that input rows equal standardized plus quarantined
rows. If they do not, it fails rather than publishing a table that quietly lost
something.

Outputs, all under ``data/reports/``:

    standardized_lab_results.csv        one row per result
    result_standardization_issues.csv   every named problem
    unmapped_lab_items.csv              tests with no LOINC code, for review
    unit_mapping_coverage.md            which units were recognised
    result_value_mapping_coverage.md    which text results were recognised
    standardization_summary.md          the headline numbers
    standardization_manifest.json       what the run depended on
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import (  # noqa: E402
    SourceLabItem,
    SourceLabResult,
    StandardizationIssue,
    StandardizationRun,
    StandardizedLabObservation,
)
from backend.app.services import release_service  # noqa: E402
from backend.app.services.categorical_normalizer import seed_value_mappings  # noqa: E402
from backend.app.services.result_standardizer import run_standardization  # noqa: E402
from backend.app.services.unit_normalizer import seed_unit_rules  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402
from backend.app.utils.privacy import secret_fingerprint  # noqa: E402

log = get_logger("script.standardize")

OBSERVATION_COLUMNS = [
    "source_row_id", "subject_key", "encounter_key", "itemid", "charttime",
    "source_label", "source_fluid", "source_category",
    "original_loinc_code", "resolver_decision", "engine_suggested_loinc",
    "approved_current_loinc", "current_loinc_version",
    "loinc_component", "loinc_property", "loinc_system", "loinc_scale",
    "raw_value", "raw_numeric_value", "raw_unit", "raw_flag",
    "value_type", "comparator", "standard_numeric_value", "standard_ucum_unit",
    "unit_status", "normalized_text_value", "coded_value_system", "coded_value_code",
    "value_mapping_status", "interpretation_code", "data_absent_reason",
    "quality_status", "issues",
]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def export_observations(session, run: StandardizationRun, path: Path) -> int:
    rows = session.scalars(
        select(StandardizedLabObservation)
        .where(StandardizedLabObservation.standardization_run_id == run.id)
        .order_by(StandardizedLabObservation.id)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OBSERVATION_COLUMNS)
        writer.writeheader()
        for o in rows:
            writer.writerow({
                **{c: getattr(o, c, None) for c in OBSERVATION_COLUMNS if c != "issues"},
                "issues": ";".join(o.issues_json or []),
            })
            written += 1
    return written


def export_issues(session, run: StandardizationRun, path: Path) -> int:
    rows = session.scalars(
        select(StandardizationIssue)
        .where(StandardizationIssue.standardization_run_id == run.id)
        .order_by(StandardizationIssue.id)
    )
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_row_id", "itemid", "issue_code", "severity", "detail"])
        written = 0
        for i in rows:
            writer.writerow([i.source_row_id, i.itemid, i.issue_code, i.severity, i.detail])
            written += 1
    return written


def export_unmapped_items(session, dataset: str, path: Path) -> int:
    """Tests with no LOINC code, with what their results actually look like.

    The observed units and example values are the point: a reviewer choosing a
    code needs to see what the test produces, not just its name.
    """
    items = session.scalars(
        select(SourceLabItem).where(
            SourceLabItem.source_dataset == dataset,
            SourceLabItem.original_loinc_code.is_(None),
        ).order_by(SourceLabItem.itemid)
    ).all()

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "itemid", "label", "fluid", "category", "observed_units",
            "result_examples", "result_count",
            "candidate_loinc", "confidence", "reviewer_decision", "reviewer_name", "reviewed_at",
        ])
        for item in items:
            rows = session.scalars(
                select(SourceLabResult).where(
                    SourceLabResult.source_dataset == dataset,
                    SourceLabResult.itemid == item.itemid,
                ).limit(400)
            ).all()
            total = session.scalar(
                select(func.count()).select_from(SourceLabResult).where(
                    SourceLabResult.source_dataset == dataset,
                    SourceLabResult.itemid == item.itemid,
                )
            ) or 0
            units = Counter(r.raw_unit for r in rows if r.raw_unit)
            examples = [r.raw_value for r in rows if r.raw_value][:5]
            writer.writerow([
                item.itemid, item.label, item.fluid, item.category,
                "; ".join(f"{u} ({n})" for u, n in units.most_common(4)),
                " | ".join(str(e)[:24] for e in examples),
                total,
                "", "", "", "", "",   # left blank: a person fills these in
            ])
    return len(items)


def write_coverage_reports(session, run: StandardizationRun, reports: Path) -> None:
    """Which units and which text results we could and could not recognise."""
    obs = session.scalars(
        select(StandardizedLabObservation).where(
            StandardizedLabObservation.standardization_run_id == run.id
        )
    ).all()

    # -- units ---------------------------------------------------------
    unit_rows = [o for o in obs if o.value_type == "QUANTITY"]
    by_status = Counter(o.unit_status or "NONE" for o in unit_rows)
    unrecognised = Counter(
        o.raw_unit for o in unit_rows
        if o.raw_unit and o.unit_status in ("UNIT_UNKNOWN", "UNIT_REVIEW_REQUIRED")
    )
    recognised = Counter(
        o.standard_ucum_unit for o in unit_rows if o.standard_ucum_unit
    )
    total_numeric = len(unit_rows) or 1

    lines = ["# Unit coverage", "",
             f"Run {run.id} · LOINC {run.loinc_version} · {len(unit_rows):,} rows carry a number.", "",
             "## What happened to the unit", "", "| outcome | rows | share |", "|---|---:|---:|"]
    for status, n in by_status.most_common():
        lines.append(f"| `{status}` | {n:,} | {n / total_numeric:.2%} |")
    lines += ["", "## UCUM codes produced", "", "| UCUM | rows |", "|---|---:|"]
    for code, n in recognised.most_common(30):
        lines.append(f"| `{code}` | {n:,} |")
    if unrecognised:
        lines += ["", "## Units with no rule yet", "",
                  "Nothing was converted for these; the value and the original unit are "
                  "kept exactly as they arrived.", "",
                  "| raw unit | rows |", "|---|---:|"]
        for unit, n in unrecognised.most_common(40):
            lines.append(f"| `{unit}` | {n:,} |")
    (reports / "unit_mapping_coverage.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- categorical values --------------------------------------------
    text_rows = [o for o in obs if o.value_type in ("CODEABLE_CONCEPT", "STRING")]
    by_map = Counter(o.value_mapping_status or "NONE" for o in text_rows)
    unmapped = Counter(
        o.raw_value for o in text_rows if o.value_mapping_status == "UNMAPPED" and o.raw_value
    )
    total_text = len(text_rows) or 1

    lines = ["# Categorical result coverage", "",
             f"Run {run.id} · {len(text_rows):,} rows carry text rather than a number.", "",
             "| outcome | rows | share |", "|---|---:|---:|"]
    for status, n in by_map.most_common():
        lines.append(f"| `{status}` | {n:,} | {n / total_text:.2%} |")
    lines += ["",
              "`TEXT_NORMALIZED_CODE_PENDING` means the wording was standardised but no "
              "standard code was attached, because SNOMED CT International is not licensed "
              "here. The text is kept; a code is not invented.", ""]
    if unmapped:
        lines += ["## Text with no rule yet", "", "| raw value | rows |", "|---|---:|"]
        for value, n in unmapped.most_common(60):
            lines.append(f"| `{str(value)[:60]}` | {n:,} |")
    (reports / "result_value_mapping_coverage.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def write_summary(run: StandardizationRun, reports: Path) -> str:
    s = run.summary_json or {}
    total = run.input_rows or 1
    vt = s.get("by_value_type", {})
    q = s.get("quality", {})

    def pct(n: int) -> str:
        return f"{n / total:.2%}"

    lines = [
        "# Result standardization summary", "",
        f"Run {run.id} · dataset {run.source_dataset} · "
        f"LOINC {run.loinc_version} · finished {run.completed_at:%Y-%m-%d %H:%M}", "",
        "## Did anything get lost?", "",
        f"- rows in: **{run.input_rows:,}**",
        f"- standardized: **{run.standardized_rows:,}**",
        f"- quarantined: **{run.quarantined_rows:,}**",
        f"- accounted for: **{'yes' if run.rows_accounted_for else 'NO -- this is a bug'}**", "",
        "## What kind of answer each row carried", "",
        "| kind | rows | share |", "|---|---:|---:|",
        f"| a number | {vt.get('QUANTITY', 0):,} | {pct(vt.get('QUANTITY', 0))} |",
        f"| a category | {vt.get('CODEABLE_CONCEPT', 0):,} | {pct(vt.get('CODEABLE_CONCEPT', 0))} |",
        f"| free text | {vt.get('STRING', 0):,} | {pct(vt.get('STRING', 0))} |",
        f"| nothing recorded | {vt.get('ABSENT', 0):,} | {pct(vt.get('ABSENT', 0))} |", "",
        "## Terminology", "",
        f"- rows whose test carries a LOINC code: **{s.get('loinc_coverage', 0):,}** "
        f"({s.get('loinc_coverage_rate', 0):.2%})",
        f"- rows with a code that is **approved and valid today**: "
        f"**{s.get('approved_loinc_coverage', 0):,}** ({s.get('approved_loinc_rate', 0):.2%})", "",
        "The gap between those two lines is the point of the whole project: a code being "
        "present is not the same as a code being right.", "",
        "## Units", "",
        f"- rows with a number: **{vt.get('QUANTITY', 0):,}**",
        f"- of those, given a UCUM unit: **{s.get('ucum_coverage', 0):,}** "
        f"({(s.get('ucum_rate_of_numeric') or 0):.2%})", "",
        "## Quality", "",
        "| status | rows | share |", "|---|---:|---:|",
        f"| clean | {q.get('OK', 0):,} | {pct(q.get('OK', 0))} |",
        f"| usable, with something worth knowing | {q.get('WARNING', 0):,} | {pct(q.get('WARNING', 0))} |",
        f"| quarantined for review | {q.get('QUARANTINED', 0):,} | {pct(q.get('QUARANTINED', 0))} |", "",
        "## Every issue raised", "",
        "| issue | rows |", "|---|---:|",
    ]
    for code, n in (s.get("issues") or {}).items():
        lines.append(f"| `{code}` | {n:,} |")
    lines += ["",
              "Nothing here was silently discarded. Every count above corresponds to rows "
              "that are still in the database, with the original value intact."]

    text = "\n".join(lines) + "\n"
    (reports / "standardization_summary.md").write_text(text, encoding="utf-8")
    return text


def write_manifest(session, run: StandardizationRun, reports: Path, source_checksum: str | None) -> None:
    loinc = release_service.get_current(session, "LOINC")
    snomed = release_service.get_current(session, "SNOMED_CT")
    manifest = {
        "run_id": run.id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset": run.source_dataset,
        "source_checksum": source_checksum,
        "loinc_version": run.loinc_version,
        "loinc_source_file": loinc.source_filename if loinc else None,
        "loinc_sha256": loinc.sha256 if loinc else None,
        "snomed_version": run.snomed_version,
        "snomed_source_file": snomed.source_filename if snomed else None,
        "unit_rule_version": run.unit_rule_version,
        "value_rule_version": run.value_rule_version,
        "pseudonym_secret_fingerprint": secret_fingerprint(),
        "code_commit": _git_commit(),
        "row_counts": {
            "input": run.input_rows,
            "standardized": run.standardized_rows,
            "quarantined": run.quarantined_rows,
        },
        "summary": run.summary_json,
    }
    run.manifest_json = manifest
    (reports / "standardization_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="MIMIC_III")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--seed-rules", action="store_true",
                        help="load the unit and value rule tables first")
    parser.add_argument("--source-checksum", default=None,
                        help="checksum of the file the raw rows came from, for the manifest")
    args = parser.parse_args(argv)

    configure_logging()
    reports = settings.reports_path
    reports.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as session:
        loinc = release_service.get_current(session, "LOINC")
        if loinc is None:
            print("ERROR: no LOINC release has been imported, so no code can be checked.",
                  file=sys.stderr)
            print("       python scripts/import_loinc.py --file <...> --version <...>",
                  file=sys.stderr)
            return 1

        raw_rows = session.scalar(
            select(func.count()).select_from(SourceLabResult).where(
                SourceLabResult.source_dataset == args.dataset)
        )
        if not raw_rows:
            print(f"ERROR: no raw results stored for {args.dataset}.", file=sys.stderr)
            print("       python scripts/import_mimic_labevents.py --file <...>",
                  file=sys.stderr)
            return 1

        if args.seed_rules:
            units = seed_unit_rules(session)
            values = seed_value_mappings(session)
            session.commit()
            print(f"Seeded {units} unit rules and {values} value rules.")

        print(f"Dataset     : {args.dataset}  ({raw_rows:,} raw rows stored)")
        print(f"LOINC       : {loinc.version}")
        print("Standardizing…")
        print()

        try:
            run = run_standardization(
                session, source_dataset=args.dataset, limit=args.limit
            )
        except RuntimeError as exc:
            session.commit()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        n_obs = export_observations(session, run, reports / "standardized_lab_results.csv")
        n_iss = export_issues(session, run, reports / "result_standardization_issues.csv")
        n_unmapped = export_unmapped_items(session, args.dataset, reports / "unmapped_lab_items.csv")
        write_coverage_reports(session, run, reports)
        summary = write_summary(run, reports)
        write_manifest(session, run, reports, args.source_checksum)
        session.commit()

    print(summary)
    print(f"Wrote {n_obs:,} standardized rows, {n_iss:,} issue records, "
          f"{n_unmapped} unmapped tests for review.")
    print(f"Reports in {reports}")
    print()
    print("Next:")
    print("  python scripts/export_fhir_observations.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
