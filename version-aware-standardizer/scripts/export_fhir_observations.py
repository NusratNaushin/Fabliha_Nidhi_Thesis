"""Write a standardization run as FHIR R4 Observations.

    python scripts/export_fhir_observations.py
    python scripts/export_fhir_observations.py --run-id 2 --validate

Produces NDJSON, one Observation per line, which is the shape FHIR bulk export
uses and the easiest thing for another system to ingest.

Quarantined rows are left out by default. They stay in the database with their
reason attached, but publishing a resource we have already said is not fit to
use would defeat the purpose of quarantining it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import StandardizationRun  # noqa: E402
from backend.app.services.fhir_observation_exporter import (  # noqa: E402
    export_ndjson,
    iter_observations,
    validate_observation,
)
from backend.app.utils.logging import configure_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, default=None,
                        help="which run to export (default: the most recent)")
    parser.add_argument("--out", default=None)
    parser.add_argument("--include-quarantined", action="store_true")
    parser.add_argument("--validate", action="store_true",
                        help="check every resource against the R4 rules we rely on")
    args = parser.parse_args(argv)

    configure_logging()
    out = Path(args.out) if args.out else settings.reports_path / "standardized_lab_results.ndjson"

    with SessionLocal() as session:
        if args.run_id:
            run = session.get(StandardizationRun, args.run_id)
        else:
            run = session.scalars(
                select(StandardizationRun).order_by(StandardizationRun.id.desc()).limit(1)
            ).first()
        if run is None:
            print("ERROR: no standardization run exists yet.", file=sys.stderr)
            print("       python scripts/standardize_mimic_results.py", file=sys.stderr)
            return 1

        print(f"Run        : {run.id} ({run.source_dataset}, LOINC {run.loinc_version})")
        written = export_ndjson(
            session, run, out, include_quarantined=args.include_quarantined
        )
        print(f"Written    : {written:,} Observations -> {out}")

        if args.validate:
            problems = Counter()
            checked = bad = 0
            for resource in iter_observations(
                session, run.id, include_quarantined=args.include_quarantined
            ):
                checked += 1
                issues = validate_observation(resource)
                if issues:
                    bad += 1
                    for issue in issues:
                        problems[issue] += 1
            print()
            print(f"Validated  : {checked:,} resources, {bad:,} with problems")
            if problems:
                for problem, n in problems.most_common():
                    print(f"  {n:>7,}  {problem}")
                return 1
            print("  every resource satisfies the R4 rules this exporter is responsible for")
            print("  (structural only -- not a full profile validation)")

    print()
    print("A sample resource:")
    with out.open(encoding="utf-8") as fh:
        first = fh.readline()
    if first:
        print(json.dumps(json.loads(first), indent=2)[:1100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
