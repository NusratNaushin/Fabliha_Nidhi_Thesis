"""Import MIMIC-III D_LABITEMS as real-world local mappings to audit.

    python scripts/import_mimic_labitems.py --file data/raw/validation/D_LABITEMS.csv

Why MIMIC-III and not MIMIC-IV-on-FHIR: MIMIC-IV on FHIR keeps the original
MIMIC terminology for laboratory observations, so its codes are not an official
LOINC ground truth and must not be used as one.  MIMIC-III's D_LABITEMS, by
contrast, carries a LOINC_CODE column that real people assigned years ago --
exactly the kind of historical mapping set this project exists to audit.

These mappings are emphatically NOT gold labels.  Lin et al. (2011) manually
reviewed voluntary LOINC mappings at three large institutions and found errors
in roughly 4.5% of the sampled tests, in four systematic categories (human
error, wrong granularity, misunderstanding the test, misunderstanding LOINC
naming).  We therefore import them as *claims to be audited*, never as truth.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.constants import MapCorrelation, TerminologySystem  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import mapping_service  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.import_mimic")

DEFAULT_DATASET = "MIMIC_III"

COLUMN_ALIASES = {
    "itemid": ("ITEMID", "itemid"),
    "label": ("LABEL", "label"),
    "fluid": ("FLUID", "fluid"),
    "category": ("CATEGORY", "category"),
    "loinc_code": ("LOINC_CODE", "loinc_code"),
}


def _resolve(header: list[str]) -> dict[str, str]:
    lowered = {h.strip().lower(): h for h in header}
    resolved: dict[str, str] = {}
    for attr, candidates in COLUMN_ALIASES.items():
        for candidate in candidates:
            actual = lowered.get(candidate.lower())
            if actual:
                resolved[attr] = actual
                break
    missing = [a for a in ("itemid", "label", "loinc_code") if a not in resolved]
    if missing:
        raise SystemExit(
            f"D_LABITEMS.csv is missing required column(s) {missing}. "
            f"Header found: {header}"
        )
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        required=True,
        help="path to D_LABITEMS.csv (the public MIMIC-III demo is enough)",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument(
        "--mapped-against-version",
        default=None,
        help=(
            "the LOINC release these mappings were made against, if known. "
            "Leave unset: for MIMIC-III it is genuinely unknown, and inventing "
            "one would defeat the point of version-awareness."
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        print(
            "Download the MIMIC-III demo (PhysioNet) and place D_LABITEMS.csv in "
            "data/raw/validation/.",
            file=sys.stderr,
        )
        return 2

    rows: list[dict] = []
    skipped_no_code = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = _resolve(reader.fieldnames or [])
        for row in reader:
            code = (row.get(cols["loinc_code"]) or "").strip()
            if not code:
                skipped_no_code += 1
                continue
            rows.append(
                {
                    "source_dataset": args.dataset,
                    "source_system": "MIMIC-III D_LABITEMS",
                    "local_code": (row.get(cols["itemid"]) or "").strip(),
                    "local_text": (row.get(cols["label"]) or "").strip(),
                    "local_context": {
                        "fluid": (row.get(cols.get("fluid", "")) or "").strip()
                        if "fluid" in cols
                        else None,
                        "category": (row.get(cols.get("category", "")) or "").strip()
                        if "category" in cols
                        else None,
                    },
                    "target_system": TerminologySystem.LOINC.value,
                    "target_code": code,
                    "mapped_against_version": args.mapped_against_version,
                    "map_correlation": MapCorrelation.NOT_SPECIFIED.value,
                }
            )

    with SessionLocal() as session:
        created, skipped = mapping_service.bulk_create_mappings(session, rows)
        session.commit()

    print("MIMIC-III D_LABITEMS import")
    print("---------------------------")
    print(f"  rows with a LOINC code:   {len(rows)}")
    print(f"  rows without a code:      {skipped_no_code} (not imported)")
    print(f"  mappings created:         {created}")
    print(f"  already present, skipped: {skipped}")
    print()
    print("These are historical claims to be audited, not ground truth.")
    print("Next: python scripts/audit_mappings.py --source-dataset " + args.dataset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
