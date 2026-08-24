"""Load MIMIC's raw laboratory results, pseudonymised at the door.

    python scripts/import_mimic_labevents.py --file "<LABEVENTS.csv or a .zip>"
    python scripts/import_mimic_labevents.py --file <...> --limit 1000

Until now this project read only ``itemid`` from the result table, because
counting how often a test was used needs nothing else. Standardizing the results
themselves needs the values, the units, the times and -- unavoidably -- the
patient and admission identifiers that tie rows together.

So this is the first script in the project that touches patient-level data, and
it does two things before anything else:

* **identifiers are replaced at import**, by keyed HMAC, so nothing downstream
  ever sees a real ``SUBJECT_ID``. The key lives in the environment;
  ``PSEUDONYM_SECRET`` is required and the script refuses to run without it.
* **values are stored exactly as they arrived.** ``source_lab_result`` is
  written once and never edited. Everything standardized lands in a separate
  table, so the original text, unit and flag survive whatever later processing
  concludes.

A null ``HADM_ID`` is preserved as null. It means an outpatient result -- a real
state, not missing data -- and dropping those rows would quietly bias the
dataset toward inpatients.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import SourceLabItem, SourceLabResult  # noqa: E402
from backend.app.utils.checksum import sha256_file  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402
from backend.app.utils.privacy import PseudonymError, Pseudonymiser, secret_fingerprint  # noqa: E402

log = get_logger("script.import_labevents")

DEFAULT_DATASET = "MIMIC_III"
LABEVENTS_NAME = "LABEVENTS.csv"
DICTIONARY_NAME = "D_LABITEMS.csv"

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _column(row: dict, *names: str) -> str | None:
    """Fetch a column whatever case the export used."""
    lowered = {k.lower(): v for k, v in row.items() if k}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def _open_member(path: Path, basename: str) -> Iterator[dict]:
    """Stream a CSV from a plain file, a .gz, or a ZIP that contains it."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [
                n for n in archive.namelist()
                if n.rsplit("/", 1)[-1].lower() == basename.lower()
            ]
            if not members:
                raise FileNotFoundError(
                    f"{path.name} contains no {basename}. Members: {archive.namelist()[:10]}"
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


def import_dictionary(session, path: Path, dataset: str) -> int:
    """Load ``D_LABITEMS`` into ``source_lab_item``. Idempotent by (dataset, itemid)."""
    known = {
        i.itemid for i in session.scalars(
            select(SourceLabItem).where(SourceLabItem.source_dataset == dataset)
        )
    }
    added = 0
    for row in _open_member(path, DICTIONARY_NAME):
        itemid = _column(row, "itemid")
        if not itemid or itemid in known:
            continue
        session.add(
            SourceLabItem(
                source_dataset=dataset,
                itemid=itemid,
                label=_column(row, "label"),
                fluid=_column(row, "fluid"),
                category=_column(row, "category"),
                original_loinc_code=_column(row, "loinc_code", "loinc"),
            )
        )
        known.add(itemid)
        added += 1
    session.flush()
    return added


def import_results(
    session, path: Path, dataset: str, *, limit: int | None, batch_size: int
) -> tuple[int, int, int]:
    """Stream ``LABEVENTS`` into ``source_lab_result``.

    Returns (read, inserted, skipped-as-already-present).
    """
    try:
        pseudonymiser = Pseudonymiser(namespace=dataset)
    except PseudonymError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    existing = {
        r for (r,) in session.execute(
            select(SourceLabResult.source_row_id).where(
                SourceLabResult.source_dataset == dataset
            )
        )
    }

    read = inserted = skipped = 0
    batch: list[dict] = []

    for row in _open_member(path, LABEVENTS_NAME):
        read += 1
        row_id = _column(row, "row_id")
        if not row_id:
            row_id = f"auto-{read}"
        if row_id in existing:
            skipped += 1
            continue

        numeric = _column(row, "valuenum")
        try:
            numeric_value = float(numeric) if numeric is not None else None
        except ValueError:
            numeric_value = None

        batch.append({
            "source_dataset": dataset,
            "source_row_id": row_id,
            # The identifiers stop being identifiers here and nowhere later.
            "subject_key": pseudonymiser.subject(_column(row, "subject_id")),
            "encounter_key": pseudonymiser.encounter(_column(row, "hadm_id")),
            "itemid": _column(row, "itemid") or "",
            "charttime": _column(row, "charttime"),
            "raw_value": _column(row, "value"),
            "raw_numeric_value": numeric_value,
            "raw_unit": _column(row, "valueuom"),
            "raw_flag": _column(row, "flag"),
        })
        existing.add(row_id)
        inserted += 1

        if len(batch) >= batch_size:
            session.execute(SourceLabResult.__table__.insert(), batch)
            batch.clear()
        if limit and inserted >= limit:
            break

    if batch:
        session.execute(SourceLabResult.__table__.insert(), batch)
    session.flush()
    return read, inserted, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True,
                        help="LABEVENTS.csv, a .gz, or a ZIP containing it")
    parser.add_argument("--dictionary", default=None,
                        help="D_LABITEMS source, if it is in a different file")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many new rows (for a quick trial)")
    parser.add_argument("--batch-size", type=int, default=settings.ingest_batch_size)
    parser.add_argument("--replace", action="store_true",
                        help="delete this dataset's existing raw rows first")
    args = parser.parse_args(argv)

    configure_logging()
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        return 2
    dictionary_path = Path(args.dictionary) if args.dictionary else path

    print(f"Source file : {path}")
    print(f"Dataset     : {args.dataset}")
    print(f"Checksum    : {sha256_file(path)}")
    print(f"Secret      : PSEUDONYM_SECRET fingerprint {secret_fingerprint()}")
    print()

    with SessionLocal() as session:
        if args.replace:
            removed = session.execute(
                delete(SourceLabResult).where(
                    SourceLabResult.source_dataset == args.dataset
                )
            ).rowcount
            print(f"Removed {removed:,} existing raw rows for {args.dataset}.")

        try:
            items = import_dictionary(session, dictionary_path, args.dataset)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Dictionary  : {items:,} new test definitions")

        try:
            read, inserted, skipped = import_results(
                session, path, args.dataset,
                limit=args.limit, batch_size=args.batch_size,
            )
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        session.commit()

        total_items = session.scalar(
            select(func.count()).select_from(SourceLabItem).where(
                SourceLabItem.source_dataset == args.dataset)
        )
        total_rows = session.scalar(
            select(func.count()).select_from(SourceLabResult).where(
                SourceLabResult.source_dataset == args.dataset)
        )
        pseudonyms = session.scalar(
            select(func.count(func.distinct(SourceLabResult.subject_key))).where(
                SourceLabResult.source_dataset == args.dataset)
        )

    print(f"Results     : {read:,} read, {inserted:,} inserted, {skipped:,} already present")
    print()
    print("In the database now")
    print(f"  test definitions   : {total_items:,}")
    print(f"  raw result rows    : {total_rows:,}")
    print(f"  distinct patients  : {pseudonyms:,}  (as pseudonyms; no real id was stored)")
    print()
    print("Next:")
    print("  python scripts/standardize_mimic_results.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
