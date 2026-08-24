"""Audit every stored mapping against the current terminology releases.

    python scripts/audit_mappings.py
    python scripts/audit_mappings.py --source-dataset MIMIC_III
    python scripts/audit_mappings.py --target-system LOINC --report-name mimic_loinc_audit.csv

Prints the human-readable report and writes a CSV under data/reports/.
Nothing is migrated: approving a replacement is a separate, explicit action.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import audit_service, release_service  # noqa: E402
from backend.app.utils.logging import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dataset", default=None)
    parser.add_argument("--target-system", default=None, help="LOINC or SNOMED_CT")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report-name", default=None, help="CSV file name")
    parser.add_argument("--no-csv", action="store_true")
    args = parser.parse_args(argv)

    configure_logging()

    with SessionLocal() as session:
        versions = release_service.current_versions(session)
        if not any(versions.values()):
            print(
                "ERROR: no terminology release has been imported yet. "
                "Nothing can be validated.",
                file=sys.stderr,
            )
            print(
                "Run scripts/import_loinc.py and/or scripts/import_snomed.py first.",
                file=sys.stderr,
            )
            return 2
        for system, info in versions.items():
            state = f"{info['version']}" if info else "(not imported)"
            print(f"{system:<12} current release: {state}")
        print()

        run = audit_service.run_audit(
            session,
            source_dataset=args.source_dataset,
            target_system=args.target_system,
            limit=args.limit,
            export_csv=not args.no_csv,
            report_name=args.report_name,
        )
        print(audit_service.render_report(run))

        summary = run.summary_json or {}
        needs_review = summary.get("manual_review_required", 0)
        if needs_review:
            print()
            print(
                f"{needs_review} mapping(s) need a human decision. "
                f"Inspect them with:"
            )
            print(
                f"  GET /api/v1/audits/{run.id}/results?decision=MANUAL_REVIEW"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
