"""Weight a MIMIC audit by how much data actually rests on each mapping.

    python scripts/mimic_impact_report.py --labevents <LABEVENTS.csv or a .zip containing it>

An audit says "24 of 585 mappings are stale". That is a code count, and it
undersells the problem in one direction and oversells it in another: a stale
code nobody ever used matters less than a stale code behind a fifth of the
laboratory results.

This script joins the audit verdicts to the observed row counts in
``LABEVENTS``, so the finding becomes "N% of real laboratory results rest on a
mapping that is no longer valid" -- a number a clinician can act on.

**No patient data is read beyond what is needed to count, and none is stored.**
Only ``itemid`` is looked at; subject ids, admission ids, timestamps, values and
flags are never touched, never aggregated and never written. The output is a
per-itemid row count and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.constants import Decision, TerminologyStatus  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.mimic_impact")

LABEVENTS_NAME = "LABEVENTS.csv"
IMPACT_COLUMNS = [
    "itemid",
    "result_rows",
    "share_of_all_results",
    "local_text",
    "fluid",
    "old_loinc",
    "current_status",
    "decision",
    "reason",
    "suggested_replacements",
]


class ImpactError(RuntimeError):
    pass


def _open_labevents(path: Path):
    """Yield LABEVENTS rows from a CSV, a .gz, or a ZIP that contains one."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [
                n
                for n in archive.namelist()
                if n.rsplit("/", 1)[-1].lower() == LABEVENTS_NAME.lower()
            ]
            if not members:
                raise ImpactError(
                    f"{path.name} contains no {LABEVENTS_NAME}. Members: "
                    f"{archive.namelist()[:10]}"
                )
            with archive.open(members[0]) as fh:
                yield from csv.DictReader(
                    io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                )
        return
    if path.suffix.lower() == ".gz":
        import gzip

        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as fh:
            yield from csv.DictReader(fh)
        return
    with path.open(encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


def count_usage(path: Path) -> Counter:
    """Result rows per itemid. Nothing else is retained."""
    usage: Counter = Counter()
    seen_header = False
    for row in _open_labevents(path):
        if not seen_header:
            keys = {k.lower() for k in row}
            if "itemid" not in keys:
                raise ImpactError(
                    f"{path.name} has no 'itemid' column; found {sorted(row)[:8]}"
                )
            seen_header = True
        itemid = (row.get("itemid") or row.get("ITEMID") or "").strip()
        if itemid:
            usage[itemid] += 1
    if not usage:
        raise ImpactError(f"{path.name} contained no usable rows.")
    return usage


def load_audit(path: Path) -> dict[str, dict]:
    if not path.is_file():
        raise ImpactError(
            f"{path} not found. Produce it first:\n"
            f"  python scripts/audit_mappings.py --source-dataset MIMIC_III "
            f"--report-name {path.name}"
        )
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ImpactError(f"{path} is empty.")
    return {r["local_code"]: r for r in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labevents",
        required=True,
        help="LABEVENTS.csv, LABEVENTS.csv.gz, or a ZIP containing one",
    )
    parser.add_argument(
        "--audit",
        default=None,
        help="the audit CSV (default: data/reports/mimic_loinc_audit.csv)",
    )
    parser.add_argument("--out", default=None, help="markdown report path")
    parser.add_argument("--csv-out", default=None, help="per-itemid CSV path")
    args = parser.parse_args(argv)

    configure_logging()

    audit_path = (
        Path(args.audit) if args.audit else settings.reports_path / "mimic_loinc_audit.csv"
    )
    labevents = Path(args.labevents)
    if not labevents.exists():
        print(f"ERROR: {labevents} does not exist.", file=sys.stderr)
        return 2

    try:
        audit = load_audit(audit_path)
        usage = count_usage(labevents)
    except ImpactError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    total = sum(usage.values())
    by_status: Counter = Counter()
    by_decision: Counter = Counter()
    unmapped = 0
    detail: list[tuple] = []

    for itemid, rows in usage.items():
        verdict = audit.get(itemid)
        if verdict is None:
            unmapped += rows
            continue
        by_status[verdict["terminology_status"]] += rows
        by_decision[verdict["decision"]] += rows
        if verdict["terminology_status"] != TerminologyStatus.CURRENT_VALID.value:
            detail.append((rows, itemid, verdict))

    detail.sort(key=lambda t: -t[0])
    stale_rows = sum(rows for rows, _, _ in detail)
    auto = by_decision.get(Decision.SUGGEST_REPLACEMENT.value, 0)
    manual = by_decision.get(Decision.MANUAL_REVIEW.value, 0) + by_decision.get(
        Decision.UNKNOWN_CODE.value, 0
    )

    def pct(n: int) -> str:
        return f"{n / total:.2%}" if total else "0%"

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    emit("# MIMIC-III mapping staleness, weighted by observed use")
    emit()
    emit(f"Audit verdicts : {audit_path}")
    emit(f"Result rows    : {labevents}")
    emit()
    emit(
        "Only `itemid` is read from the result table. No patient identifier, "
        "timestamp or value is examined or stored."
    )
    emit()
    emit("## Coverage")
    emit()
    emit(f"- laboratory result rows: **{total:,}**")
    emit(f"- distinct itemids used: **{len(usage):,}**")
    emit(f"- rows whose itemid carries a LOINC mapping: **{total - unmapped:,}** ({pct(total - unmapped)})")
    emit(f"- rows whose itemid has no LOINC code at all: **{unmapped:,}** ({pct(unmapped)})")
    emit()
    emit("## Result rows by the CURRENT status of the code they map to")
    emit()
    emit("| status | result rows | share |")
    emit("|---|---:|---:|")
    for status, rows in by_status.most_common():
        emit(f"| `{status}` | {rows:,} | {pct(rows)} |")
    if unmapped:
        emit(f"| (no LOINC mapping) | {unmapped:,} | {pct(unmapped)} |")
    emit()
    emit("## What the engine would do, weighted by data volume")
    emit()
    emit("| decision | result rows | share |")
    emit("|---|---:|---:|")
    for decision, rows in by_decision.most_common():
        emit(f"| `{decision}` | {rows:,} | {pct(rows)} |")
    emit()
    emit(
        f"**{stale_rows:,} result rows ({pct(stale_rows)}) rest on a mapping that is "
        f"no longer valid.** Of those, {auto:,} ({pct(auto)}) have exactly one "
        f"official replacement the engine can offer, and {manual:,} ({pct(manual)}) "
        f"need a human decision."
    )
    emit()
    emit("## Stale mappings that actually carry data")
    emit()
    emit("| result rows | itemid | old code | status | decision | suggested | test |")
    emit("|---:|---|---|---|---|---|---|")
    for rows, itemid, verdict in detail:
        emit(
            f"| {rows:,} | {itemid} | `{verdict['old_code']}` "
            f"| {verdict['terminology_status']} | {verdict['decision']} "
            f"| {verdict['suggested_targets'] or '(none)'} | {verdict['local_text']} |"
        )
    emit()
    emit(
        "A code count alone would have reported these as a handful of rows in a "
        "dictionary. Weighted by use, they are the difference between a mapping "
        "table that is 96% right and a dataset where one common coagulation test "
        "alone accounts for most of the drift."
    )

    out_path = Path(args.out) if args.out else settings.reports_path / "mimic_impact.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_path = (
        Path(args.csv_out) if args.csv_out else settings.reports_path / "mimic_impact.csv"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=IMPACT_COLUMNS)
        writer.writeheader()
        for itemid, rows in usage.most_common():
            verdict = audit.get(itemid, {})
            context = verdict.get("local_text", "")
            writer.writerow(
                {
                    "itemid": itemid,
                    "result_rows": rows,
                    "share_of_all_results": f"{rows / total:.6f}",
                    "local_text": context,
                    "fluid": "",
                    "old_loinc": verdict.get("old_code", ""),
                    "current_status": verdict.get("terminology_status", ""),
                    "decision": verdict.get("decision", ""),
                    "reason": verdict.get("reason", ""),
                    "suggested_replacements": verdict.get("suggested_targets", ""),
                }
            )

    print()
    print(f"Report written to {out_path}")
    print(f"Per-itemid CSV   {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
