"""End-to-end demonstration on synthetic releases (Master Instruction 54).

    python scripts/demo_end_to_end.py

Runs the whole pipeline -- import two LOINC releases and two SNOMED releases,
load a small local mapping set, audit it, diff the releases, and walk through
Demos 1-7 -- without needing a single licensed file.  Everything it touches is
synthetic and lives in its own throwaway database, so it never disturbs the
real one.

This is the script to run in front of a supervisor when the licensed archives
are not yet available.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Point the application at a throwaway database BEFORE importing it.
_DEMO_DIR = Path(tempfile.mkdtemp(prefix="vas-demo-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_DEMO_DIR / 'demo.db').as_posix()}"
os.environ["REPORTS_DIR"] = str(_DEMO_DIR / "reports")
os.environ["LOG_LEVEL"] = "WARNING"

from backend.app.constants import Decision  # noqa: E402
from backend.app.database import Base, SessionLocal, engine  # noqa: E402
from backend.app.services import (  # noqa: E402
    audit_service,
    loinc_diff,
    mapping_service,
    snomed_diff,
)
from backend.app.services.loinc_ingest import ingest_loinc_release  # noqa: E402
from backend.app.services.loinc_resolver import LoincResolver  # noqa: E402
from backend.app.services.snomed_rf2_parser import ingest_snomed_release  # noqa: E402
from backend.app.services.snomed_resolver import SnomedResolver  # noqa: E402
from tests.fixtures import synthetic as fx  # noqa: E402

DEMO_MAPPINGS = ROOT / "tests" / "fixtures" / "demo_local_mappings.csv"
DEMO_LABITEMS = ROOT / "tests" / "fixtures" / "demo_d_labitems.csv"


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def verdict(label: str, decision: Decision, reason, targets: list[str]) -> None:
    arrow = f"  -> {', '.join(targets)}" if targets else ""
    print(
        f"  {label:<28} {decision.value:<20} "
        f"{(reason.value if reason else ''):<30}{arrow}"
    )


def load_releases(session, release_dir: Path) -> None:
    rule("SETUP  synthetic terminology releases")
    fx.write_loinc_old(release_dir)
    fx.write_loinc_new(release_dir)
    fx.write_snomed_old(release_dir)
    fx.write_snomed_new(release_dir)

    for version, name, current in (
        (fx.LOINC_OLD_VERSION, f"Loinc_{fx.LOINC_OLD_VERSION}.zip", False),
        (fx.LOINC_NEW_VERSION, f"Loinc_{fx.LOINC_NEW_VERSION}.zip", True),
    ):
        report = ingest_loinc_release(
            session,
            file_path=release_dir / name,
            version=version,
            make_current=current,
        )
        print(
            f"  LOINC {version:<8} concepts={report.concepts:<4} "
            f"mapto={report.map_to_rows:<3} changes={report.change_rows:<3} "
            f"current={current}  sha256={report.sha256[:12]}..."
        )

    for version, current in (
        (fx.SNOMED_OLD_VERSION, False),
        (fx.SNOMED_NEW_VERSION, True),
    ):
        name = f"SnomedCT_SyntheticRF2_PRODUCTION_{version}T120000Z.zip"
        report = ingest_snomed_release(
            session,
            file_path=release_dir / name,
            version=version,
            make_current=current,
        )
        print(
            f"  SNOMED {version:<7} concepts={report.concepts:<4} "
            f"assoc={report.associations:<3} inactivations={report.inactivations:<3} "
            f"terms={report.concept_terms:<4} current={current}  "
            f"sha256={report.sha256[:12]}..."
        )


def load_demo_mappings(session) -> None:
    rule("SETUP  local mappings to audit")
    with DEMO_MAPPINGS.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        mapping_service.create_mapping(
            session,
            source_dataset=row["source_dataset"],
            local_code=row["local_code"],
            local_text=row["local_text"],
            target_system=row["target_system"],
            target_code=row["target_code"],
            local_context={"fluid": row["fluid"], "category": row["category"]},
            mapped_against_version=row["mapped_against_version"] or None,
            map_correlation=row["map_correlation"],
        )
    session.commit()
    print(f"  loaded {len(rows)} local mappings from {DEMO_MAPPINGS.name}")


def demos_one_to_six(session) -> None:
    loinc = LoincResolver(session)
    snomed = SnomedResolver(session)

    rule("DEMO 1  valid mapping -> KEEP")
    r = loinc.resolve(fx.L_ACTIVE)
    verdict(f"LOINC {fx.L_ACTIVE}", r.decision, r.reason, [])

    rule("DEMO 2  deprecated LOINC -> official MapTo -> SUGGEST_REPLACEMENT")
    r = loinc.resolve(fx.L_DEP_ONE)
    verdict(
        f"LOINC {fx.L_DEP_ONE}",
        r.decision,
        r.reason,
        [t.code for t in r.suggested_targets],
    )
    print(f"    status in {loinc.version}: {r.raw_status}")

    rule("DEMO 3  multiple official replacements -> MANUAL_REVIEW")
    r = loinc.resolve(fx.L_DISC_MANY)
    verdict(
        f"LOINC {fx.L_DISC_MANY}",
        r.decision,
        r.reason,
        [t.code for t in r.suggested_targets],
    )
    print("    context decides which one applies, so the engine abstains.")

    rule("DEMO 4  active SNOMED -> KEEP")
    r = snomed.resolve(fx.S_ACTIVE)
    verdict(f"SNOMED {fx.S_ACTIVE}", r.decision, r.reason, [])
    print(f"    display term (offline, no Snowstorm): {r.display!r}")

    rule("DEMO 5  inactive SNOMED + REPLACED BY / SAME AS -> SUGGEST_REPLACEMENT")
    for concept_id in (fx.S_REPLACED, fx.S_SAME_AS):
        r = snomed.resolve(concept_id)
        verdict(
            f"SNOMED {concept_id}",
            r.decision,
            r.reason,
            [t.concept_id for t in r.suggested_targets],
        )
        print(
            f"    inactivation={r.inactivation_reason} "
            f"association={r.associations[0].association_type} "
            f"display={r.display!r} -> {r.suggested_targets[0].display!r}"
        )

    rule("DEMO 6  ambiguous SNOMED -> MANUAL_REVIEW")
    for concept_id in (fx.S_POSSIBLY, fx.S_WAS_A, fx.S_MULTI, fx.S_MOVED, fx.S_NO_ASSOC):
        r = snomed.resolve(concept_id)
        verdict(
            f"SNOMED {concept_id}",
            r.decision,
            r.reason,
            [t.concept_id for t in r.suggested_targets],
        )


def demo_seven(session) -> None:
    rule("DEMO 7  real-world shape: D_LABITEMS -> import -> audit -> CSV")
    with DEMO_LABITEMS.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r["LOINC_CODE"] or "").strip()]
    created, skipped = mapping_service.bulk_create_mappings(
        session,
        [
            {
                "source_dataset": "DEMO_LABITEMS",
                "source_system": "synthetic D_LABITEMS",
                "local_code": row["ITEMID"],
                "local_text": row["LABEL"],
                "local_context": {
                    "fluid": row["FLUID"],
                    "category": row["CATEGORY"],
                },
                "target_system": "LOINC",
                "target_code": row["LOINC_CODE"].strip(),
                "mapped_against_version": None,
            }
            for row in rows
        ],
    )
    session.commit()
    print(f"  imported {created} lab item mappings ({skipped} already present)")
    print("  NOTE: with real MIMIC-III these are historical claims, not gold labels.")

    run = audit_service.run_audit(
        session, source_dataset="DEMO_LABITEMS", report_name="demo_labitems_audit.csv"
    )
    print()
    print(audit_service.render_report(run))


def demo_history(session) -> None:
    rule("DEMO 8  approval and history: nothing changes without a reviewer")
    mapping = mapping_service.list_mappings(
        session, source_dataset="MANUAL_TEST", limit=100
    )
    target = next(m for m in mapping if m.target_code == fx.L_DEP_ONE)

    print(f"  before   {target.local_text} -> {target.target_code} "
          f"(mapped against {target.mapped_against_version})")

    run = audit_service.run_audit(
        session, source_dataset="MANUAL_TEST", report_name="demo_manual_audit.csv"
    )
    result = next(
        r
        for r in audit_service.list_results(session, run.id)
        if r.mapping_id == target.id
    )
    print(f"  audit    {result.decision} -> {result.suggested_targets_json[0]['code']}")

    session.refresh(target)
    print(f"  after    {target.target_code}   <- unchanged: an audit only suggests")

    revision = mapping_service.approve_replacement(
        session,
        mapping_id=target.id,
        target_code=fx.L_ACTIVE,
        reviewer="demo-reviewer",
        reason="official MapTo, reviewed for the demo",
        audit_result_id=result.id,
    )
    session.commit()
    print(f"  approved {revision.old_target_code} -> {revision.new_target_code} "
          f"by {revision.approved_by}")
    print(f"  history  {revision.old_target_code}@{revision.old_target_version} -> "
          f"{revision.new_target_code}@{revision.new_target_version}")
    print("           the old code and its release survive forever.")


def demo_diffs(session) -> None:
    rule("DEMO 9  release-to-release validation")
    report = loinc_diff.diff_releases(
        session,
        old_version=fx.LOINC_OLD_VERSION,
        new_version=fx.LOINC_NEW_VERSION,
    )
    print(report.render())

    snomed_report = snomed_diff.diff_releases(
        session,
        old_version=fx.SNOMED_OLD_VERSION,
        new_version=fx.SNOMED_NEW_VERSION,
    )
    print()
    print(snomed_report.render())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary demo database and reports instead of deleting them",
    )
    args = parser.parse_args(argv)

    Base.metadata.create_all(bind=engine)
    release_dir = _DEMO_DIR / "releases"

    try:
        with SessionLocal() as session:
            load_releases(session, release_dir)
            load_demo_mappings(session)
            demos_one_to_six(session)
            demo_seven(session)
            demo_history(session)
            demo_diffs(session)

        rule("DONE")
        print("  Every code, term and release above is synthetic.")
        print("  Nothing licensed was used, and nothing was auto-migrated.")
        print(f"  Reports: {os.environ['REPORTS_DIR']}")
        if args.keep:
            print(f"  Demo database kept at: {_DEMO_DIR}")
        return 0
    finally:
        engine.dispose()
        if not args.keep:
            shutil.rmtree(_DEMO_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
