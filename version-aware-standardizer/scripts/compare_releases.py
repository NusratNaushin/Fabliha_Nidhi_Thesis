"""Compare two imported terminology releases.

    python scripts/compare_releases.py --system LOINC     --old <VERSION_A> --new <VERSION_B>
    python scripts/compare_releases.py --system SNOMED_CT --old <VERSION_A> --new <VERSION_B>

For LOINC the computed diff is additionally validated against the official
LoincChangeSnapshot.csv of the newer release; ``missed_changes`` must be 0 for
the supported fields.  That check -- our engine versus the vendor's own change
log -- is the primary correctness evidence for the update engine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import loinc_diff, snomed_diff  # noqa: E402
from backend.app.services.release_service import list_releases  # noqa: E402
from backend.app.utils.logging import configure_logging  # noqa: E402


def _print_available(session, system: str) -> None:
    releases = list_releases(session, system)
    print(f"Imported {system} releases:", file=sys.stderr)
    if not releases:
        print("  (none)", file=sys.stderr)
    for release in releases:
        marker = " [current]" if release.is_current else ""
        print(f"  {release.version}{marker}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True, help="LOINC or SNOMED_CT")
    parser.add_argument("--old", required=True, help="older release version")
    parser.add_argument("--new", required=True, help="newer release version")
    parser.add_argument("--report-name", default=None)
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="SNOMED only: cap how many newly-inactive concepts are resolved",
    )
    args = parser.parse_args(argv)

    configure_logging()
    system = args.system.strip().upper().replace("-", "_")
    if system in {"SNOMED", "SNOMEDCT"}:
        system = "SNOMED_CT"

    with SessionLocal() as session:
        try:
            if system == "LOINC":
                report = loinc_diff.diff_releases(
                    session,
                    old_version=args.old,
                    new_version=args.new,
                    export_csv=not args.no_csv,
                    report_name=args.report_name,
                )
            elif system == "SNOMED_CT":
                report = snomed_diff.diff_releases(
                    session,
                    old_version=args.old,
                    new_version=args.new,
                    export_csv=not args.no_csv,
                    report_name=args.report_name,
                    limit=args.limit,
                )
            else:
                print(
                    f"ERROR: --system must be LOINC or SNOMED_CT, got {args.system!r}",
                    file=sys.stderr,
                )
                return 2
        except (loinc_diff.DiffError, snomed_diff.SnomedDiffError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            _print_available(session, system)
            return 1

        print(report.render())

        if system == "LOINC":
            missed = report.validation.missed_count
            if report.validation.change_snapshot_available and missed:
                print()
                print(
                    f"FAIL: {missed} official change(s) were not detected. "
                    f"Target is 0.",
                    file=sys.stderr,
                )
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
