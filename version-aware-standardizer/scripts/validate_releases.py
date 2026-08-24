"""The strongest correctness experiment, as one command.

    python scripts/validate_releases.py

Put two official LOINC releases and/or two official SNOMED CT RF2 releases in
data/raw/validation/ and run it.  It imports them into a **disposable** database
(your working one is never touched), runs the whole experiment, prints the
numbers and writes data/reports/validation_report.md.

What it measures, and why those measurements are worth anything:

LOINC
  1. Our computed release-to-release diff versus the newer release's own
     LoincChangeSnapshot.csv.  Target: **0 missed official changes** for the
     fields we model.
  2. Every code that went ACTIVE -> DISCOURAGED/DEPRECATED is turned into a
     simulated historical mapping made against the OLD release, and audited
     against the NEW one.  Targets: **100% of the status changes detected** and
     **0 replacements that MapTo does not license**.

SNOMED CT
  3. Every concept that went active=1 -> active=0 is resolved, and the
     associations we extract are compared row for row against the newer
     edition's own historical-association reference set.  Targets: **100%
     inactive-detection recall**, **100% association-extraction accuracy**, and
     **0 unsafe automatic updates**.

The ground truth is never a hand-written expectation: it is always the official
files themselves.  That is the whole point -- a reviewer can re-run this against
the same two archives and get the same numbers.

Exit code 0 only when every target is met.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_DEFAULT_VALIDATION_DIR = ROOT / "data" / "raw" / "validation"

PLACEMENT_HELP = f"""
Place the official archives in {_DEFAULT_VALIDATION_DIR} and run again.

  LOINC   two 'LOINC Complete' ZIPs, e.g. Loinc_<older>.zip and Loinc_<newer>.zip
          (a free account at the official LOINC downloads page)
  SNOMED  two International Edition RF2 ZIPs
          (licensed affiliate/member access -- never bypass the licence)

Terminology archives are git-ignored and are never committed.
Nothing is downloaded for you.
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--validation-dir",
        default=str(_DEFAULT_VALIDATION_DIR),
        help="where the two-of-each archives live",
    )
    parser.add_argument("--loinc-old", default=None, help="explicit older LOINC ZIP")
    parser.add_argument("--loinc-new", default=None, help="explicit newer LOINC ZIP")
    parser.add_argument("--snomed-old", default=None, help="explicit older RF2 ZIP")
    parser.add_argument("--snomed-new", default=None, help="explicit newer RF2 ZIP")
    parser.add_argument(
        "--out",
        default=None,
        help="report path (default: data/reports/validation_report.md)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "where to import the releases. Defaults to a throwaway SQLite file "
            "in a temp directory, deleted on exit."
        ),
    )
    parser.add_argument(
        "--keep-database",
        action="store_true",
        help="keep the throwaway database for inspection",
    )
    parser.add_argument(
        "--max-simulated",
        type=int,
        default=None,
        help="cap how many simulated historical mappings are audited",
    )
    parser.add_argument(
        "--max-snomed",
        type=int,
        default=None,
        help="cap how many newly inactive SNOMED concepts are resolved",
    )
    args = parser.parse_args(argv)
    # A half-specified pair would otherwise be discarded silently and the
    # default directory globbed instead, so the run could report "Every target
    # met" for a release pair the user never named.
    for old, new, label in (
        (args.loinc_old, args.loinc_new, "--loinc-old/--loinc-new"),
        (args.snomed_old, args.snomed_new, "--snomed-old/--snomed-new"),
    ):
        if bool(old) != bool(new):
            parser.error(f"{label} must be given together")
    return args


# Created lazily: building it at import time leaks a directory on every --help
# and on every early exit, and makes --keep-database report a path that never
# held a database.
_TMP_DIR: Path | None = None


def _configure_environment(args: argparse.Namespace) -> int:
    """Point the application at the database this run should use.

    The application binds its engine at import time, so the choice has to be
    made before ``backend.app.database`` is first imported.  If it is already
    loaded we refuse rather than quietly importing two real terminology releases
    into whatever database happened to be bound -- possibly the developer's
    working one.
    """
    if "backend.app.database" in sys.modules:
        print(
            "ERROR: backend.app.database is already imported, so the database "
            "for this run cannot be chosen safely.",
            file=sys.stderr,
        )
        print(
            "       Run this file as a script: python scripts/validate_releases.py",
            file=sys.stderr,
        )
        return 2
    global _TMP_DIR
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    else:
        _TMP_DIR = Path(tempfile.mkdtemp(prefix="vas-validate-"))
        os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'validation.db').as_posix()}"
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    problem = _configure_environment(args)
    if problem:
        return problem

    # Imported after the environment is set, so the engine binds to the
    # disposable database rather than the developer's working one.
    from backend.app.config import settings
    from backend.app.constants import Decision, TerminologySystem
    from backend.app.database import Base, SessionLocal, engine
    from backend.app import models  # noqa: F401
    from backend.app.services import (
        audit_service,
        loinc_diff,
        mapping_service,
        snomed_diff,
    )
    from backend.app.services.loinc_ingest import detect_version as detect_loinc_version
    from backend.app.services.loinc_ingest import ingest_loinc_release
    from backend.app.services.loinc_resolver import LoincResolver
    from backend.app.services.snomed_rf2_parser import (
        detect_version as detect_snomed_version,
    )
    from backend.app.services.snomed_rf2_parser import ingest_snomed_release
    from backend.app.utils.archive import ReleaseArchive
    from backend.app.utils.logging import configure_logging

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))

    validation_dir = Path(args.validation_dir)
    report: list[str] = []
    failures: list[str] = []
    ran_anything = False

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    def rule(title: str) -> None:
        emit()
        emit("=" * 78)
        emit(title)
        emit("=" * 78)

    def pick(explicit: str | None, pattern: str) -> list[Path]:
        if explicit:
            return [Path(explicit)]
        if not validation_dir.is_dir():
            return []
        # Recursive on purpose: downloads commonly land in per-terminology
        # subfolders, and assuming a flat layout is exactly the kind of
        # assumption this project exists to avoid.
        return sorted(validation_dir.rglob(pattern))

    loinc_paths = (
        [Path(args.loinc_old), Path(args.loinc_new)]
        if args.loinc_old and args.loinc_new
        else pick(None, "Loinc*.zip")
    )
    snomed_paths = (
        [Path(args.snomed_old), Path(args.snomed_new)]
        if args.snomed_old and args.snomed_new
        else pick(None, "SnomedCT*.zip")
    )

    Base.metadata.create_all(bind=engine)
    started = datetime.now(timezone.utc)

    emit("# Version-aware validation report")
    emit()
    emit(f"Generated: {started.isoformat(timespec='seconds')}")
    emit(f"Database:  {engine.url.render_as_string(hide_password=True)}")
    emit()
    emit(
        "Ground truth in every section below is taken from the official release "
        "files themselves, never from a hand-written expectation."
    )

    try:
        with SessionLocal() as session:
            # ---------------------------------------------------------------
            # LOINC
            # ---------------------------------------------------------------
            rule("LOINC")
            if len(loinc_paths) < 2:
                emit()
                emit(
                    f"SKIPPED: found {len(loinc_paths)} LOINC archive(s) in "
                    f"{validation_dir}; two are required."
                )
                emit("```")
                emit(PLACEMENT_HELP.strip())
                emit("```")
            else:
                ran_anything = True
                versions: list[str] = []
                for index, path in enumerate(loinc_paths[:2]):
                    with ReleaseArchive(path) as archive:
                        version = detect_loinc_version(archive)
                    if not version:
                        emit(f"ERROR: could not detect a version from {path.name}")
                        failures.append(f"LOINC version detection failed for {path.name}")
                        break
                    emit()
                    emit(f"Importing {path.name} as LOINC {version} ...")
                    ingest_loinc_release(
                        session,
                        file_path=path,
                        version=version,
                        make_current=(index == 1),
                    )
                    versions.append(version)

                if len(versions) == 2:

                    def _key(v: str) -> list[int]:
                        return [int(p) for p in v.split(".") if p.isdigit()] or [0]

                    old_version, new_version = sorted(versions, key=_key)
                    # The newer release must be the current one.
                    from backend.app.services import release_service

                    newer = release_service.find_by_version(
                        session, TerminologySystem.LOINC.value, new_version
                    )
                    release_service.set_current(session, newer)
                    session.commit()

                    emit(f"Older release: {old_version}")
                    emit(f"Newer release: {new_version}  (current)")

                    # -- experiment 1: diff vs official Change Snapshot -----
                    emit()
                    emit("## 1. Computed diff versus the official Change Snapshot")
                    emit()
                    diff = loinc_diff.diff_releases(
                        session,
                        old_version=old_version,
                        new_version=new_version,
                        report_name=(
                            f"validation_loinc_{old_version}_to_{new_version}.csv"
                        ),
                    )
                    emit("```")
                    for line in diff.render().splitlines():
                        emit(line)
                    emit("```")

                    validation = diff.validation
                    if not validation.change_snapshot_available:
                        emit()
                        emit(
                            f"NOTE: LOINC {new_version} shipped no "
                            f"LoincChangeSnapshot.csv, so this comparison could "
                            f"not run. The computed diff above stands alone."
                        )
                    elif validation.official_changes == 0:
                        failures.append(
                            "LOINC Change Snapshot contained no comparable rows "
                            "-- the comparison would have passed vacuously"
                        )
                        emit()
                        emit("FAIL: no comparable official changes; refusing to claim a pass.")
                    elif validation.missed_count:
                        failures.append(
                            f"LOINC: {validation.missed_count} official change(s) missed"
                        )
                        emit()
                        emit(
                            f"FAIL: {validation.missed_count} official change(s) not "
                            f"detected, e.g. {validation.missed_changes[:5]}"
                        )
                    else:
                        emit()
                        emit(
                            f"PASS: all {validation.official_changes} official "
                            f"changes reproduced; 0 missed."
                        )

                    # -- experiment 2: simulated historical mappings --------
                    emit()
                    emit("## 2. Simulated historical mappings audited against the new release")
                    emit()
                    transitions = loinc_diff.status_change_codes(
                        session, old_version=old_version, new_version=new_version
                    )
                    obsolete = [
                        code
                        for transition, codes in transitions.items()
                        if transition.startswith("ACTIVE ->")
                        and transition.split("-> ")[1]
                        in {"DISCOURAGED", "DEPRECATED"}
                        for code in codes
                    ]
                    if args.max_simulated:
                        obsolete = obsolete[: args.max_simulated]

                    if not obsolete:
                        emit(
                            f"SKIPPED: no ACTIVE -> DISCOURAGED/DEPRECATED transition "
                            f"between {old_version} and {new_version}. Choose a wider "
                            f"release pair for this experiment to mean anything."
                        )
                    else:
                        emit(
                            f"{len(obsolete)} code(s) went ACTIVE -> obsolete between "
                            f"the two releases. Each becomes a mapping made against "
                            f"{old_version}."
                        )
                        rows = [
                            {
                                "source_dataset": "SIMULATED_HISTORICAL",
                                "source_system": f"LOINC {old_version}",
                                "local_code": f"sim-{code}",
                                "local_text": f"simulated historical mapping to {code}",
                                "target_system": TerminologySystem.LOINC.value,
                                "target_code": code,
                                "mapped_against_version": old_version,
                            }
                            for code in obsolete
                        ]
                        created, skipped = mapping_service.bulk_create_mappings(
                            session, rows
                        )
                        session.commit()
                        emit(f"  created {created} simulated mappings ({skipped} skipped)")

                        run = audit_service.run_audit(
                            session,
                            source_dataset="SIMULATED_HISTORICAL",
                            report_name=(
                                f"validation_simulated_{old_version}_to_{new_version}.csv"
                            ),
                        )
                        summary = run.summary_json or {}
                        emit()
                        emit("```")
                        for line in audit_service.render_report(run).splitlines():
                            emit(line)
                        emit("```")

                        still_valid = summary.get("valid", 0) + summary.get(
                            "trial_warning", 0
                        )
                        emit()
                        if still_valid:
                            failures.append(
                                f"LOINC: {still_valid} status change(s) were not detected"
                            )
                            emit(
                                f"FAIL: {still_valid} mapping(s) were reported as still "
                                f"valid although the official STATUS changed."
                            )
                        else:
                            emit(
                                f"PASS: all {len(obsolete)} status changes detected "
                                f"(0 reported as still valid)."
                            )

                        # Every suggestion must start from an official MapTo row.
                        resolver = LoincResolver(session)
                        resolver.preload(obsolete)
                        invented: list[str] = []
                        suggested = 0
                        for code in obsolete:
                            result = resolver.resolve(code)
                            if result.decision is not Decision.SUGGEST_REPLACEMENT:
                                continue
                            suggested += 1
                            official = {
                                m.target_loinc for m in resolver.get_map_to(code)
                            }
                            first_hop = result.suggested_targets[0].via[1]
                            if first_hop not in official:
                                invented.append(f"{code} -> {first_hop}")
                        emit()
                        if invented:
                            failures.append(
                                f"LOINC: {len(invented)} replacement(s) not licensed by MapTo"
                            )
                            emit(f"FAIL: invented replacements: {invented[:5]}")
                        else:
                            emit(
                                f"PASS: {suggested} replacement(s) suggested, every one "
                                f"backed by an official MapTo row; 0 invented."
                            )

            # ---------------------------------------------------------------
            # SNOMED CT
            # ---------------------------------------------------------------
            rule("SNOMED CT")
            if len(snomed_paths) < 2:
                emit()
                emit(
                    f"SKIPPED: found {len(snomed_paths)} SNOMED archive(s) in "
                    f"{validation_dir}; two are required."
                )
            else:
                ran_anything = True
                versions = []
                for index, path in enumerate(snomed_paths[:2]):
                    with ReleaseArchive(path) as archive:
                        version = detect_snomed_version(archive)
                    if not version:
                        emit(f"ERROR: could not detect a release date from {path.name}")
                        failures.append(
                            f"SNOMED version detection failed for {path.name}"
                        )
                        break
                    emit()
                    emit(f"Importing {path.name} as SNOMED_CT {version} ...")
                    ingest_snomed_release(
                        session,
                        file_path=path,
                        version=version,
                        make_current=(index == 1),
                    )
                    versions.append(version)

                if len(versions) == 2:
                    old_version, new_version = sorted(versions)
                    from backend.app.services import release_service

                    newer = release_service.find_by_version(
                        session, TerminologySystem.SNOMED_CT.value, new_version
                    )
                    release_service.set_current(session, newer)
                    session.commit()

                    emit(f"Older release: {old_version}")
                    emit(f"Newer release: {new_version}  (current)")
                    emit()
                    emit("## 3. Newly inactive concepts versus the official refsets")
                    emit()

                    snomed_report = snomed_diff.diff_releases(
                        session,
                        old_version=old_version,
                        new_version=new_version,
                        report_name=(
                            f"validation_snomed_{old_version}_to_{new_version}.csv"
                        ),
                        limit=args.max_snomed,
                    )
                    emit("```")
                    for line in snomed_report.render().splitlines():
                        emit(line)
                    emit("```")
                    emit()

                    if not snomed_report.became_inactive:
                        emit(
                            f"SKIPPED: no concept became inactive between "
                            f"{old_version} and {new_version}. Choose a wider "
                            f"release pair."
                        )
                    else:
                        if snomed_report.inactive_detection_recall < 1.0:
                            failures.append(
                                f"SNOMED: inactive detection recall "
                                f"{snomed_report.inactive_detection_recall:.4f} < 1.0"
                            )
                            emit(
                                f"FAIL: inactive detection recall "
                                f"{snomed_report.inactive_detection_recall * 100:.2f}%"
                            )
                        else:
                            emit(
                                f"PASS: all {len(snomed_report.became_inactive)} "
                                f"active -> inactive transitions detected."
                            )

                        if snomed_report.unsafe_auto_update:
                            failures.append(
                                f"SNOMED: {snomed_report.unsafe_auto_update} unsafe "
                                f"automatic update(s)"
                            )
                            emit(
                                f"FAIL: {snomed_report.unsafe_auto_update} unsafe "
                                f"automatic update(s)."
                            )
                        else:
                            emit(
                                "PASS: 0 unsafe automatic updates -- nothing in this "
                                "codebase migrates a mapping without an approval call."
                            )

                        accuracy, mismatches = _association_accuracy(
                            session,
                            new_version,
                            snomed_report.became_inactive,
                            snomed_paths[1],
                        )
                        emit()
                        if mismatches:
                            failures.append(
                                f"SNOMED: association extraction mismatched on "
                                f"{len(mismatches)} concept(s)"
                            )
                            emit(
                                f"FAIL: association extraction accuracy "
                                f"{accuracy * 100:.2f}%, mismatches e.g. {mismatches[:5]}"
                            )
                        else:
                            emit(
                                f"PASS: association extraction accuracy 100.00% over "
                                f"{len(snomed_report.became_inactive)} concept(s), "
                                f"checked against the release file itself."
                            )

            # ---------------------------------------------------------------
            rule("RESULT")
            emit()
            if not ran_anything:
                emit("Nothing was validated: no release pair was available.")
                emit("```")
                emit(PLACEMENT_HELP.strip())
                emit("```")
            elif failures:
                emit(f"{len(failures)} target(s) MISSED:")
                for failure in failures:
                    emit(f"  - {failure}")
            else:
                emit("Every target met.")

            out_path = (
                Path(args.out)
                if args.out
                else settings.reports_path / "validation_report.md"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
            print()
            print(f"Report written to {out_path}")

    finally:
        url = engine.url.render_as_string(hide_password=True)
        engine.dispose()
        if args.keep_database:
            print(f"Database kept at {url}")
        elif _TMP_DIR is not None:
            shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if not ran_anything:
        return 2
    return 1 if failures else 0


def _association_accuracy(
    session, new_version: str, concept_ids: list[str], archive_path
):
    """Compare what the engine reports against the RELEASE FILE, row for row.

    Ground truth is re-read from the archive, not from the tables we populated
    from it.  Reading the database back would compare our parser with itself and
    report 100% no matter what the parser did.
    """
    from backend.app.services.snomed_rf2_parser import read_active_associations
    from backend.app.services.snomed_resolver import SnomedResolver

    official = read_active_associations(archive_path, concept_ids)

    resolver = SnomedResolver(session)
    resolver.preload(concept_ids)

    mismatches: list[str] = []
    for concept_id in concept_ids:
        expected = official.get(concept_id, set())
        extracted = {
            (a.association_type, a.target_component_id)
            for a in resolver.resolve(concept_id).associations
        }
        if expected != extracted:
            mismatches.append(concept_id)

    total = len(concept_ids) or 1
    return (total - len(mismatches)) / total, mismatches


if __name__ == "__main__":
    raise SystemExit(main())
