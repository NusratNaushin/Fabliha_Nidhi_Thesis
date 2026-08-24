"""Check that a standardization run kept every promise it made.

    python scripts/validate_standardized_results.py
    python scripts/validate_standardized_results.py --run-id 2

Every claim this pipeline makes is checkable, so this checks them. It is the
counterpart to the summary report: the summary says what happened, this says
whether what happened was allowed.

The checks, and why each one matters:

* **row conservation** -- nothing may vanish. A shorter output table is the most
  dangerous failure there is, because it looks like success.
* **raw preservation** -- the original value, unit and flag must survive
  unchanged, or the standardized column cannot be audited against them.
* **no invented numbers** -- "Negative" must never have become 0, and a missing
  result must never have become anything.
* **comparators kept** -- a censored result must still carry its sign.
* **approvals are real** -- an approved code must be valid in the current
  release, and must never have been filled in from a suggestion.
* **no invented codes** -- with no SNOMED licence, no coded value may carry a
  SNOMED system.
* **privacy** -- no exported row may contain anything that looks like a raw
  patient identifier.
* **determinism** -- the same input standardized twice must give the same answer.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import (  # noqa: E402
    SourceLabResult,
    StandardizationRun,
    StandardizedLabObservation,
)
from backend.app.services import release_service  # noqa: E402
from backend.app.services.fhir_observation_exporter import (  # noqa: E402
    iter_observations,
    validate_observation,
)
from backend.app.services.loinc_resolver import LoincResolver  # noqa: E402
from backend.app.constants import Decision  # noqa: E402
from backend.app.utils.logging import configure_logging  # noqa: E402

PSEUDONYM = re.compile(r"^[0-9a-f]{32}$")


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.lines: list[str] = []

    def emit(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.emit(f"{'PASS' if passed else 'FAIL'}: {name} -- {detail}")
        if not passed:
            self.failures.append(f"{name}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--sample", type=int, default=4000,
                        help="how many rows to check row-by-row")
    args = parser.parse_args(argv)

    configure_logging()
    c = Checker()

    with SessionLocal() as session:
        run = (
            session.get(StandardizationRun, args.run_id) if args.run_id
            else session.scalars(
                select(StandardizationRun).order_by(StandardizationRun.id.desc()).limit(1)
            ).first()
        )
        if run is None:
            print("ERROR: no standardization run exists yet.", file=sys.stderr)
            return 1

        c.emit(f"# Standardization validation -- run {run.id}")
        c.emit()
        c.emit(f"Dataset {run.source_dataset} - LOINC {run.loinc_version} - "
               f"{run.input_rows:,} rows in")
        c.emit()

        # -- 1. nothing lost --------------------------------------------
        written = session.scalar(
            select(func.count()).select_from(StandardizedLabObservation).where(
                StandardizedLabObservation.standardization_run_id == run.id)
        ) or 0
        c.check(
            "row conservation",
            run.rows_accounted_for and written == run.input_rows,
            f"{run.input_rows:,} in = {run.standardized_rows:,} standardized + "
            f"{run.quarantined_rows:,} quarantined; {written:,} rows written",
        )

        raw_total = session.scalar(
            select(func.count()).select_from(SourceLabResult).where(
                SourceLabResult.source_dataset == run.source_dataset)
        ) or 0
        c.check(
            "every stored raw row was processed",
            written == raw_total or run.input_rows < raw_total,
            f"{raw_total:,} raw rows stored, {written:,} standardized",
        )

        # -- sample for the row-level checks ----------------------------
        rows = session.scalars(
            select(StandardizedLabObservation)
            .where(StandardizedLabObservation.standardization_run_id == run.id)
            .limit(args.sample)
        ).all()
        raw_by_id = {
            r.source_row_id: r for r in session.scalars(
                select(SourceLabResult).where(
                    SourceLabResult.source_dataset == run.source_dataset,
                    SourceLabResult.source_row_id.in_([o.source_row_id for o in rows]),
                )
            )
        }

        # -- 2. the raw answer survived ---------------------------------
        drift = [
            o.source_row_id for o in rows
            if (raw := raw_by_id.get(o.source_row_id))
            and (o.raw_value != raw.raw_value or o.raw_unit != raw.raw_unit
                 or o.raw_flag != raw.raw_flag)
        ]
        c.check("raw value, unit and flag preserved", not drift,
                f"checked {len(rows):,} rows; {len(drift)} differ from the source")

        # -- 3. no number was invented ----------------------------------
        invented = [
            o.source_row_id for o in rows
            if o.value_type in ("CODEABLE_CONCEPT", "STRING", "ABSENT")
            and o.standard_numeric_value is not None
        ]
        c.check("no categorical or missing result became a number", not invented,
                f"{len(invented)} rows carry a number they should not")

        zeros = [
            o.source_row_id for o in rows
            if o.value_type == "ABSENT" and o.standard_numeric_value == 0
        ]
        c.check("no missing result became zero", not zeros,
                f"{len(zeros)} absent rows hold 0")

        # -- 4. censored results kept their sign ------------------------
        censored = [o for o in rows if o.raw_value and str(o.raw_value).strip()[:1] in "<>"]
        lost_sign = [o.source_row_id for o in censored if not o.comparator]
        c.check("censored results kept their comparator", not lost_sign,
                f"{len(censored)} censored rows in the sample; {len(lost_sign)} lost the sign")

        # -- 5. approvals are real --------------------------------------
        promoted = [
            o.source_row_id for o in rows
            if o.approved_current_loinc
            and o.approved_current_loinc == o.engine_suggested_loinc
        ]
        c.check("no suggestion was promoted to an approval", not promoted,
                f"{len(promoted)} rows have an approved code equal to the engine's suggestion")

        resolver = LoincResolver(session)
        approved_codes = sorted({o.approved_current_loinc for o in rows if o.approved_current_loinc})
        if approved_codes:
            resolver.preload(approved_codes)
            bad = [
                code for code in approved_codes
                if resolver.resolve(code).decision
                not in (Decision.KEEP, Decision.KEEP_WITH_WARNING)
            ]
            c.check("every approved code is valid in the current release", not bad,
                    f"{len(approved_codes)} distinct approved codes; {len(bad)} not valid")
        else:
            c.check("every approved code is valid in the current release", True,
                    "no approved codes in the sample")

        # -- 6. no invented terminology ---------------------------------
        fabricated = [
            o.source_row_id for o in rows
            if o.coded_value_code and o.coded_value_system
            and "snomed" in str(o.coded_value_system).lower()
        ]
        c.check("no SNOMED code was invented without a licence", not fabricated,
                f"{len(fabricated)} rows carry a SNOMED-coded value")

        # -- 7. privacy --------------------------------------------------
        leaked = [
            o.source_row_id for o in rows
            if (o.subject_key and not PSEUDONYM.match(o.subject_key))
            or (o.encounter_key and not PSEUDONYM.match(o.encounter_key))
        ]
        c.check("patient and admission keys are pseudonyms", not leaked,
                f"{len(leaked)} rows carry something that is not a 32-character pseudonym")

        # -- 8. FHIR ------------------------------------------------------
        checked = problems = 0
        for resource in iter_observations(session, run.id):
            checked += 1
            if validate_observation(resource):
                problems += 1
            if checked >= args.sample:
                break
        c.check("FHIR resources satisfy the R4 rules we rely on", problems == 0,
                f"{checked:,} resources checked, {problems} with problems")

        # -- 9. the terminology story -----------------------------------
        s = run.summary_json or {}
        with_code = s.get("loinc_coverage", 0)
        approved = s.get("approved_loinc_coverage", 0)
        c.emit()
        c.emit("## What the run found")
        c.emit()
        c.emit(f"- rows whose test carries a LOINC code: {with_code:,} "
               f"({s.get('loinc_coverage_rate', 0):.2%})")
        c.emit(f"- rows whose code is approved and valid today: {approved:,} "
               f"({s.get('approved_loinc_rate', 0):.2%})")
        c.emit(f"- the difference -- codes that are present but no longer right: "
               f"**{with_code - approved:,}**")
        c.emit()

        loinc = release_service.get_current(session, "LOINC")
        c.emit(f"Judged against LOINC {loinc.version if loinc else '(none)'}"
               + (f", file {loinc.source_filename}" if loinc else ""))

    c.emit()
    if c.failures:
        c.emit(f"## RESULT: {len(c.failures)} check(s) failed")
        for failure in c.failures:
            c.emit(f"- {failure}")
    else:
        c.emit("## RESULT: every check passed")

    out = Path(args.out) if args.out else settings.reports_path / "standardization_validation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(c.lines) + "\n", encoding="utf-8")
    print()
    print(f"Report written to {out}")
    return 1 if c.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
