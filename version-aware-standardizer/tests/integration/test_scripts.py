"""The command-line entry points, exercised as real callers would use them.

Scripts are the surface a supervisor actually touches, so they get the same
scrutiny as the library: a wrong exit code or a silently-skipped row would be
invisible in the API tests but obvious here.

Each script is invoked through its ``main(argv)`` so the argument parsing, the
exit codes and the files written are all covered.
"""

from __future__ import annotations

import csv
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from backend.app.constants import Decision, ReviewStatus  # noqa: E402
from backend.app.services import audit_service, mapping_service  # noqa: E402
from tests.fixtures import synthetic as fx  # noqa: E402


def _script(name: str):
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# check_database.py
# ---------------------------------------------------------------------------
def test_check_database_passes_on_a_migrated_schema(session, capsys):
    module = _script("check_database")
    assert module.main(["--skip-revision-check"]) == 0
    out = capsys.readouterr().out
    assert "[ok]   connection" in out
    assert "[ok]   tables" in out
    assert "registry invariants" in out


def test_check_database_reports_an_unreachable_server(capsys):
    module = _script("check_database")
    code = module.main(
        ["--database-url", "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL] connection" in out
    assert "docker compose up -d" in out


def test_check_database_notices_a_missing_release(loinc_session, capsys):
    loinc_session.commit()
    module = _script("check_database")
    assert module.main(["--skip-revision-check"]) == 0
    out = capsys.readouterr().out
    # A LOINC release exists, so the "nothing imported" hint must NOT appear.
    assert "no terminology release has been imported yet" not in out


# ---------------------------------------------------------------------------
# check_coverage.py
# ---------------------------------------------------------------------------
COVERAGE_TEMPLATE = """<?xml version="1.0" ?>
<coverage line-rate="{overall}" version="7.0">
  <packages>
    <package name="services" line-rate="{overall}">
      <classes>
        <class filename="backend/app/services/loinc_resolver.py" line-rate="{loinc}">
          <lines>{loinc_lines}</lines>
        </class>
        <class filename="backend/app/services/snomed_resolver.py" line-rate="{snomed}">
          <lines>{snomed_lines}</lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _lines(hit: int, total: int) -> str:
    return "".join(
        f'<line number="{i + 1}" hits="{1 if i < hit else 0}"/>' for i in range(total)
    )


def _write_coverage(path: Path, *, loinc_pct: int, snomed_pct: int, overall_pct: int) -> Path:
    path.write_text(
        COVERAGE_TEMPLATE.format(
            overall=overall_pct / 100,
            loinc=loinc_pct / 100,
            snomed=snomed_pct / 100,
            loinc_lines=_lines(loinc_pct, 100),
            snomed_lines=_lines(snomed_pct, 100),
        ),
        encoding="utf-8",
    )
    return path


def test_coverage_gate_passes_when_both_floors_are_met(tmp_path, capsys):
    module = _script("check_coverage")
    report = _write_coverage(
        tmp_path / "coverage.xml", loinc_pct=100, snomed_pct=96, overall_pct=95
    )
    assert module.main(["--file", str(report)]) == 0
    assert "Coverage gate passed" in capsys.readouterr().out


def test_coverage_gate_fails_on_a_thin_resolver(tmp_path, capsys):
    """The whole point of the second floor: 90% overall can hide a 60% resolver."""
    module = _script("check_coverage")
    report = _write_coverage(
        tmp_path / "coverage.xml", loinc_pct=60, snomed_pct=99, overall_pct=90
    )
    assert module.main(["--file", str(report)]) == 1
    out = capsys.readouterr().out
    assert "Coverage gate FAILED" in out
    assert "loinc_resolver.py" in out


def test_coverage_gate_fails_on_a_low_overall(tmp_path, capsys):
    module = _script("check_coverage")
    report = _write_coverage(
        tmp_path / "coverage.xml", loinc_pct=100, snomed_pct=100, overall_pct=70
    )
    assert module.main(["--file", str(report)]) == 1
    assert "overall" in capsys.readouterr().out


def test_coverage_gate_reports_a_missing_file(tmp_path, capsys):
    module = _script("check_coverage")
    assert module.main(["--file", str(tmp_path / "nope.xml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_coverage_gate_notices_an_absent_core_module(tmp_path, capsys):
    module = _script("check_coverage")
    report = tmp_path / "coverage.xml"
    report.write_text(
        '<?xml version="1.0" ?><coverage line-rate="0.99"><packages><package>'
        '<classes><class filename="backend/app/main.py" line-rate="0.99">'
        '<lines><line number="1" hits="1"/></lines></class></classes>'
        "</package></packages></coverage>",
        encoding="utf-8",
    )
    assert module.main(["--file", str(report)]) == 1
    assert "not present" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# audit_mappings.py
# ---------------------------------------------------------------------------
@pytest.fixture()
def audited(full_session):
    """A committed mapping set covering several decision branches."""
    for index, (code, system) in enumerate(
        [
            (fx.L_ACTIVE, "LOINC"),
            (fx.L_DEP_ONE, "LOINC"),
            (fx.L_DISC_MANY, "LOINC"),
            (fx.L_UNKNOWN, "LOINC"),
            (fx.S_REPLACED, "SNOMED_CT"),
            (fx.S_POSSIBLY, "SNOMED_CT"),
        ]
    ):
        mapping_service.create_mapping(
            full_session,
            source_dataset="SCRIPT_TEST",
            local_code=f"S-{index}",
            local_text=f"local term {index}",
            target_system=system,
            target_code=code,
            mapped_against_version=(
                fx.LOINC_OLD_VERSION if system == "LOINC" else fx.SNOMED_OLD_VERSION
            ),
        )
    full_session.commit()
    return full_session


def test_audit_script_reports_and_exits_zero(audited, capsys):
    module = _script("audit_mappings")
    assert module.main(["--source-dataset", "SCRIPT_TEST", "--no-csv"]) == 0
    out = capsys.readouterr().out
    assert "Terminology Audit Report" in out
    assert "Mappings audited:        6" in out
    assert "need a human decision" in out


def test_audit_script_refuses_when_nothing_is_imported(session, capsys):
    module = _script("audit_mappings")
    assert module.main([]) == 2
    err = capsys.readouterr().err
    assert "no terminology release has been imported" in err
    assert "import_loinc.py" in err


def test_audit_script_writes_the_named_csv(audited, capsys, tmp_root):
    module = _script("audit_mappings")
    assert (
        module.main(
            ["--source-dataset", "SCRIPT_TEST", "--report-name", "script_audit.csv"]
        )
        == 0
    )
    report = tmp_root / "reports" / "script_audit.csv"
    assert report.is_file()
    with report.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6
    assert {r["decision"] for r in rows} >= {
        Decision.KEEP.value,
        Decision.SUGGEST_REPLACEMENT.value,
        Decision.MANUAL_REVIEW.value,
    }


# ---------------------------------------------------------------------------
# compare_releases.py
# ---------------------------------------------------------------------------
def test_compare_releases_loinc(full_session, capsys):
    full_session.commit()
    module = _script("compare_releases")
    code = module.main(
        [
            "--system",
            "LOINC",
            "--old",
            fx.LOINC_OLD_VERSION,
            "--new",
            fx.LOINC_NEW_VERSION,
            "--no-csv",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "missed_changes         0   (target: 0)" in out


def test_compare_releases_snomed(full_session, capsys):
    full_session.commit()
    module = _script("compare_releases")
    code = module.main(
        [
            "--system",
            "SNOMED",
            "--old",
            fx.SNOMED_OLD_VERSION,
            "--new",
            fx.SNOMED_NEW_VERSION,
            "--no-csv",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Unsafe automatic updates:       0" in out


def test_compare_releases_lists_what_is_available_on_a_bad_version(
    full_session, capsys
):
    full_session.commit()
    module = _script("compare_releases")
    assert (
        module.main(
            ["--system", "LOINC", "--old", "0.01", "--new", fx.LOINC_NEW_VERSION]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "Imported LOINC releases:" in err
    assert fx.LOINC_NEW_VERSION in err


def test_compare_releases_rejects_an_unknown_system(full_session, capsys):
    full_session.commit()
    module = _script("compare_releases")
    assert module.main(["--system", "ICD10", "--old", "a", "--new", "b"]) == 2
    assert "must be LOINC or SNOMED_CT" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# import_mimic_labitems.py
# ---------------------------------------------------------------------------
def test_mimic_importer_loads_only_rows_with_a_code(loinc_session, capsys):
    loinc_session.commit()
    module = _script("import_mimic_labitems")
    demo = ROOT / "tests" / "fixtures" / "demo_d_labitems.csv"
    assert module.main(["--file", str(demo), "--dataset", "MIMIC_TEST"]) == 0
    out = capsys.readouterr().out
    assert "rows without a code:      1 (not imported)" in out
    assert "mappings created:         8" in out

    mappings = mapping_service.list_mappings(loinc_session, source_dataset="MIMIC_TEST")
    assert len(mappings) == 8
    # Context is preserved so a reviewer can see fluid/category later.
    assert mappings[0].local_context_json["fluid"]
    # And the release is left unknown rather than invented.
    assert all(m.mapped_against_version is None for m in mappings)


def test_mimic_importer_is_rerunnable(loinc_session, capsys):
    loinc_session.commit()
    module = _script("import_mimic_labitems")
    demo = ROOT / "tests" / "fixtures" / "demo_d_labitems.csv"
    module.main(["--file", str(demo), "--dataset", "MIMIC_TEST"])
    capsys.readouterr()
    assert module.main(["--file", str(demo), "--dataset", "MIMIC_TEST"]) == 0
    out = capsys.readouterr().out
    assert "mappings created:         0" in out
    assert "already present, skipped: 8" in out


def test_mimic_importer_explains_a_missing_file(loinc_session, capsys, tmp_path):
    module = _script("import_mimic_labitems")
    assert module.main(["--file", str(tmp_path / "nope.csv")]) == 2
    err = capsys.readouterr().err
    assert "PhysioNet" in err


def test_mimic_importer_rejects_a_file_without_loinc_codes(loinc_session, tmp_path):
    module = _script("import_mimic_labitems")
    bad = tmp_path / "wrong.csv"
    bad.write_text("A,B,C\n1,2,3\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--file", str(bad)])
    assert "missing required column" in str(excinfo.value)


# ---------------------------------------------------------------------------
# review_queue.py
# ---------------------------------------------------------------------------
def _read_queue(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader), list(reader.fieldnames or [])


def _write_queue(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture()
def exported_queue(audited, tmp_path):
    """An audit run plus its exported review queue."""
    module = _script("review_queue")
    run = audit_service.run_audit(
        audited, source_dataset="SCRIPT_TEST", export_csv=False
    )
    audited.commit()
    queue = tmp_path / "queue.csv"
    assert module.main(["export", "--run-id", str(run.id), "--out", str(queue)]) == 0
    return module, run, queue


def test_export_never_prefills_the_approval_column(exported_queue):
    """The engine's answer and the human's consent live in different columns.

    Pre-filling approve_target_code would mean an unedited round trip migrated
    everything and stamped a named clinician on changes they never saw.
    """
    module, run, queue = exported_queue
    rows, _ = _read_queue(queue)

    by_code = {r["current_code"]: r for r in rows}
    assert all(r["approve_target_code"] == "" for r in rows)
    # The suggestion is still right there, in a column apply never reads.
    assert by_code[fx.L_DEP_ONE]["engine_suggested_code"] == fx.L_ACTIVE
    assert by_code[fx.S_REPLACED]["engine_suggested_code"] == fx.S_ACTIVE
    # Ambiguous and unknown cases offer nothing.
    assert by_code[fx.L_DISC_MANY]["engine_suggested_code"] == ""
    assert by_code[fx.S_POSSIBLY]["engine_suggested_code"] == ""
    assert by_code[fx.L_UNKNOWN]["engine_suggested_code"] == ""
    # An ACTIVE mapping never reaches the queue at all.
    assert fx.L_ACTIVE not in by_code


def test_an_unedited_export_applies_nothing(exported_queue, audited, tmp_path):
    """The regression guard for the whole design of this script."""
    module, run, queue = exported_queue
    before = {
        m.local_code: m.target_code
        for m in mapping_service.list_mappings(audited, source_dataset="SCRIPT_TEST")
    }

    outcome = tmp_path / "outcome.csv"
    assert (
        module.main(
            [
                "apply",
                "--file",
                str(queue),
                "--reviewer",
                "dr-test",
                "--out",
                str(outcome),
            ]
        )
        == 0
    )

    audited.expire_all()
    after = {
        m.local_code: m.target_code
        for m in mapping_service.list_mappings(audited, source_dataset="SCRIPT_TEST")
    }
    assert after == before
    # And no revision was invented in anybody's name.
    for mapping in mapping_service.list_mappings(audited, source_dataset="SCRIPT_TEST"):
        assert mapping_service.get_revisions(audited, mapping.id) == []


def test_review_queue_export_then_apply_round_trip(
    exported_queue, audited, capsys, tmp_path
):
    module, run, queue = exported_queue
    capsys.readouterr()

    # The reviewer accepts the engine's suggestion for exactly one row.
    rows, fieldnames = _read_queue(queue)
    for row in rows:
        if row["current_code"] == fx.L_DEP_ONE:
            row["approve_target_code"] = row["engine_suggested_code"]
            row["reviewer_note"] = "official MapTo, reviewed"
    _write_queue(queue, rows, fieldnames)

    # Dry run changes nothing.
    outcome = tmp_path / "outcome.csv"
    assert (
        module.main(
            [
                "apply",
                "--file",
                str(queue),
                "--reviewer",
                "dr-test",
                "--dry-run",
                "--out",
                str(outcome),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would apply:  1" in out

    audited.expire_all()
    mapping = next(
        m
        for m in mapping_service.list_mappings(audited, source_dataset="SCRIPT_TEST")
        if m.local_code == "S-1"
    )
    assert mapping.target_code == fx.L_DEP_ONE
    assert mapping_service.get_revisions(audited, mapping.id) == []

    # For real this time.
    assert (
        module.main(
            [
                "apply",
                "--file",
                str(queue),
                "--reviewer",
                "dr-test",
                "--out",
                str(outcome),
            ]
        )
        == 0
    )
    assert "applied:  1" in capsys.readouterr().out

    audited.expire_all()
    mapping = next(
        m
        for m in mapping_service.list_mappings(audited, source_dataset="SCRIPT_TEST")
        if m.local_code == "S-1"
    )
    assert mapping.target_code == fx.L_ACTIVE
    assert mapping.review_status == ReviewStatus.APPROVED.value

    revisions = mapping_service.get_revisions(audited, mapping.id)
    assert len(revisions) == 1
    assert revisions[0].approved_by == "dr-test"
    assert revisions[0].old_target_code == fx.L_DEP_ONE
    assert revisions[0].old_target_version == fx.LOINC_OLD_VERSION
    assert revisions[0].reason == "official MapTo, reviewed"


def test_dry_run_reaches_the_same_verdict_as_the_real_run(
    exported_queue, audited, capsys, tmp_path
):
    """A preview that disagrees with the run it previews is worse than none."""
    module, run, queue = exported_queue
    capsys.readouterr()

    rows, fieldnames = _read_queue(queue)
    for row in rows:
        # A DEPRECATED code typed by mistake: both runs must refuse it.
        if row["current_code"] == fx.L_DISC_MANY:
            row["approve_target_code"] = fx.L_DEP_NONE
    _write_queue(queue, rows, fieldnames)

    dry = tmp_path / "dry.csv"
    real = tmp_path / "real.csv"
    common = ["--file", str(queue), "--reviewer", "dr-test", "--allow-unsuggested"]

    assert module.main(["apply", *common, "--dry-run", "--out", str(dry)]) == 1
    assert module.main(["apply", *common, "--out", str(real)]) == 1

    dry_rows, _ = _read_queue(dry)
    real_rows, _ = _read_queue(real)
    assert [r["outcome"] for r in dry_rows] == ["REJECTED"]
    assert [r["outcome"] for r in real_rows] == ["REJECTED"]
    assert "DEPRECATED" in dry_rows[0]["detail"]


def test_dry_run_does_not_overwrite_a_real_outcome_file(
    exported_queue, audited, tmp_root
):
    module, run, queue = exported_queue

    rows, fieldnames = _read_queue(queue)
    for row in rows:
        if row["current_code"] == fx.L_DEP_ONE:
            row["approve_target_code"] = row["engine_suggested_code"]
    _write_queue(queue, rows, fieldnames)

    assert module.main(["apply", "--file", str(queue), "--reviewer", "dr-test"]) == 0
    real = tmp_root / "reports" / f"{queue.stem}_outcome.csv"
    assert real.is_file()
    real_content = real.read_text(encoding="utf-8")
    assert "APPLIED" in real_content

    # The mapping now points at the target, so a second pass skips it -- but the
    # point is that the dry run writes somewhere else entirely.
    assert (
        module.main(
            ["apply", "--file", str(queue), "--reviewer", "dr-test", "--dry-run"]
        )
        == 0
    )
    assert (tmp_root / "reports" / f"{queue.stem}_dryrun_outcome.csv").is_file()
    assert real.read_text(encoding="utf-8") == real_content


def test_export_backs_up_a_queue_a_human_already_edited(exported_queue, capsys):
    module, run, queue = exported_queue

    rows, fieldnames = _read_queue(queue)
    rows[0]["approve_target_code"] = "typed-by-hand"
    _write_queue(queue, rows, fieldnames)
    capsys.readouterr()

    assert module.main(["export", "--run-id", str(run.id), "--out", str(queue)]) == 0
    assert "kept a copy" in capsys.readouterr().out

    backup = queue.with_suffix(queue.suffix + ".bak")
    assert backup.is_file()
    assert "typed-by-hand" in backup.read_text(encoding="utf-8")
    # The regenerated queue is blank again.
    assert "typed-by-hand" not in queue.read_text(encoding="utf-8")


def test_export_force_skips_the_backup(exported_queue, capsys):
    module, run, queue = exported_queue
    capsys.readouterr()
    assert (
        module.main(["export", "--run-id", str(run.id), "--out", str(queue), "--force"])
        == 0
    )
    assert "kept a copy" not in capsys.readouterr().out
    assert not queue.with_suffix(queue.suffix + ".bak").exists()


def test_export_rejects_an_unrecognised_decision(exported_queue, capsys, tmp_path):
    """A typo must not produce an empty queue and exit 0."""
    module, run, _queue = exported_queue
    capsys.readouterr()
    out = tmp_path / "typo.csv"
    assert (
        module.main(
            [
                "export",
                "--run-id",
                str(run.id),
                "--decisions",
                "MANUAL_REVEIW",
                "--out",
                str(out),
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "unrecognised decision" in err
    assert not out.exists()


def test_review_queue_apply_rejects_an_invalid_target(exported_queue, capsys, tmp_path):
    module, run, queue = exported_queue
    capsys.readouterr()

    rows, fieldnames = _read_queue(queue)
    for row in rows:
        row["approve_target_code"] = (
            fx.L_DEP_NONE if row["current_code"] == fx.L_DISC_MANY else ""
        )
    _write_queue(queue, rows, fieldnames)

    outcome = tmp_path / "outcome.csv"
    code = module.main(
        [
            "apply",
            "--file",
            str(queue),
            "--reviewer",
            "dr-test",
            "--allow-unsuggested",
            "--out",
            str(outcome),
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "rejected: 1" in out
    outcomes, _ = _read_queue(outcome)
    assert outcomes[0]["outcome"] == "REJECTED"
    assert "DEPRECATED" in outcomes[0]["detail"]


def test_review_queue_apply_requires_a_reviewer(audited, tmp_path, capsys):
    module = _script("review_queue")
    queue = tmp_path / "queue.csv"
    queue.write_text("mapping_id,approve_target_code\n1,X\n", encoding="utf-8")
    assert module.main(["apply", "--file", str(queue), "--reviewer", "  "]) == 2
    assert "attributable" in capsys.readouterr().err


def test_review_queue_apply_rejects_a_malformed_file(audited, tmp_path, capsys):
    module = _script("review_queue")
    bad = tmp_path / "bad.csv"
    bad.write_text("something,else\n1,2\n", encoding="utf-8")
    assert module.main(["apply", "--file", str(bad), "--reviewer", "dr-test"]) == 2
    assert "missing column" in capsys.readouterr().err


def test_review_queue_export_needs_an_audit_run(session, capsys):
    module = _script("review_queue")
    assert module.main(["export", "--latest"]) == 1
    assert "no audit run exists yet" in capsys.readouterr().err


def test_review_queue_export_rejects_an_unknown_run(full_session, capsys):
    full_session.commit()
    module = _script("review_queue")
    assert module.main(["export", "--run-id", "4242"]) == 1
    assert "no audit run with id 4242" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate_releases.py
#
# Run as a subprocess, because the script deliberately chooses its database
# before importing the application -- which is only possible in a fresh
# interpreter, and is exactly how a user invokes it.
# ---------------------------------------------------------------------------
def _run_validator(*args: str, cwd: Path = ROOT):
    import subprocess

    return subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_releases.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.fixture(scope="module")
def synthetic_validation_dir(tmp_path_factory) -> Path:
    """Two 'official' release pairs, entirely synthetic."""
    directory = tmp_path_factory.mktemp("validation-archives")
    fx.write_loinc_old(directory)
    fx.write_loinc_new(directory)
    fx.write_snomed_old(directory)
    fx.write_snomed_new(directory)
    return directory


def test_validator_meets_every_target_on_a_known_release_pair(
    synthetic_validation_dir, tmp_path
):
    """The experiment must actually pass when the engine is correct."""
    report = tmp_path / "validation_report.md"
    result = _run_validator(
        "--validation-dir",
        str(synthetic_validation_dir),
        "--out",
        str(report),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    text = report.read_text(encoding="utf-8")
    assert "Every target met." in text
    # The four claims the thesis will quote.
    assert "official changes reproduced; 0 missed" in text
    assert "status changes detected (0 reported as still valid)" in text
    assert "0 invented" in text
    assert "0 unsafe automatic updates" in text
    assert "association extraction accuracy 100.00%" in text
    # And it says which releases produced them.
    assert fx.LOINC_OLD_VERSION in text
    assert fx.LOINC_NEW_VERSION in text
    assert fx.SNOMED_NEW_VERSION in text


def test_validator_skips_loudly_with_no_archives(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = tmp_path / "report.md"
    result = _run_validator(
        "--validation-dir", str(empty), "--out", str(report)
    )
    # Exit 2 = "nothing was validated", which must never be mistaken for a pass.
    assert result.returncode == 2, result.stdout
    text = report.read_text(encoding="utf-8")
    assert "Nothing was validated" in text
    assert "LOINC Complete" in text
    assert "never bypass the licence" in text


def test_validator_reports_a_missed_official_change(tmp_path):
    """If the engine regressed, the experiment has to FAIL rather than pass.

    A doctored release pair declares a STATUS change in the Change Snapshot that
    the concept table does not actually contain, so a correct engine cannot
    detect it -- which is precisely what the gate is for.
    """
    directory = tmp_path / "doctored"
    fx.write_loinc_release(
        directory,
        version="7.01",
        rows=fx.loinc_old_rows(),
        map_to=[],
        changes=[],
    )
    phantom = fx.loinc_new_changes() + [
        ["7.02", fx.L_ACTIVE, "STATUS", "ACTIVE", "DEPRECATED", "phantom change"]
    ]
    fx.write_loinc_release(
        directory,
        version="7.02",
        rows=fx.loinc_new_rows(),
        map_to=fx.loinc_new_map_to(),
        changes=phantom,
    )

    report = tmp_path / "report.md"
    result = _run_validator(
        "--validation-dir", str(directory), "--out", str(report)
    )
    assert result.returncode == 1, result.stdout
    text = report.read_text(encoding="utf-8")
    assert "official change(s) not detected" in text
    assert "target(s) MISSED" in text


def test_validator_refuses_to_run_in_a_loaded_interpreter(capsys):
    """In-process it cannot choose its database, so it must refuse, not guess."""
    module = _script("validate_releases")
    assert module.main(["--validation-dir", "."]) == 2
    err = capsys.readouterr().err
    assert "already imported" in err
    assert "as a script" in err


# ---------------------------------------------------------------------------
# fetch_mimic_demo.py
#
# The network is never touched: the download is stubbed so the *integrity*
# logic -- which is the entire reason the script exists -- is what gets tested.
# ---------------------------------------------------------------------------
REAL_MIMIC_HEADER = "row_id,itemid,label,fluid,category,loinc_code\n"
REAL_MIMIC_ROW = '1,50801,Alveolar-arterial Gradient,Blood,Blood Gas,19991-9\n'


@pytest.fixture()
def fetcher(monkeypatch):
    module = _script("fetch_mimic_demo")

    def install(payload: bytes, published: str | None):
        import hashlib

        def fake_download(url: str, destination: Path, timeout: float) -> None:
            if url.endswith("LICENSE.txt"):
                destination.write_bytes(b"ODC Open Database License (ODbL)")
            else:
                destination.write_bytes(payload)

        monkeypatch.setattr(module, "_download", fake_download)
        monkeypatch.setattr(
            module, "_published_checksum", lambda timeout: published
        )
        return hashlib.sha256(payload).hexdigest()

    module.install = install  # type: ignore[attr-defined]
    return module


def test_fetcher_accepts_a_file_matching_both_checksums(fetcher, tmp_path, capsys):
    payload = (REAL_MIMIC_HEADER + REAL_MIMIC_ROW).encode("utf-8")
    digest = fetcher.install(payload, published=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fetcher, "EXPECTED_SHA256", digest)
        patch.setattr(fetcher, "_published_checksum", lambda timeout: digest)
        assert fetcher.main(["--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Open Database License" in out
    assert "matches PhysioNet's published" in out
    assert (tmp_path / "D_LABITEMS.csv").read_bytes() == payload
    assert (tmp_path / "LICENSE.txt").is_file()
    assert "historical claims to be audited, not gold labels" in out


def test_fetcher_deletes_a_corrupt_download(fetcher, tmp_path, capsys):
    """If it does not match what PhysioNet published, it is not kept."""
    fetcher.install(b"truncated", published="0" * 64)
    assert fetcher.main(["--dest", str(tmp_path)]) == 1
    assert "does not match PhysioNet's published" in capsys.readouterr().err
    assert not (tmp_path / "D_LABITEMS.csv").exists()


def test_fetcher_refuses_a_silently_changed_upstream_file(fetcher, tmp_path, capsys):
    payload = b"row_id,itemid,label,fluid,category,loinc_code\n"
    digest = fetcher.install(payload, published=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fetcher, "EXPECTED_SHA256", "1" * 64)
        patch.setattr(fetcher, "_published_checksum", lambda timeout: digest)
        assert fetcher.main(["--dest", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "pinned in this script" in err
    assert "--accept-upstream-change" in err
    # A refused file must not be left where the importer is told to read.
    assert not (tmp_path / "D_LABITEMS.csv").exists()


def test_fetcher_can_accept_an_upstream_change_deliberately(fetcher, tmp_path, capsys):
    payload = b"row_id,itemid,label,fluid,category,loinc_code\n"
    digest = fetcher.install(payload, published=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fetcher, "EXPECTED_SHA256", "1" * 64)
        patch.setattr(fetcher, "_published_checksum", lambda timeout: digest)
        assert (
            fetcher.main(["--dest", str(tmp_path), "--accept-upstream-change"]) == 0
        )
    out = capsys.readouterr().out
    assert "accepted as requested" in out
    assert f"to {digest}" in out


def test_fetcher_skips_an_already_verified_file(fetcher, tmp_path, capsys):
    payload = (REAL_MIMIC_HEADER + REAL_MIMIC_ROW).encode("utf-8")
    digest = fetcher.install(payload, published=None)
    (tmp_path / "D_LABITEMS.csv").write_bytes(payload)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fetcher, "EXPECTED_SHA256", digest)
        calls: list[str] = []
        patch.setattr(
            fetcher,
            "_download",
            lambda url, destination, timeout: calls.append(url),
        )
        assert fetcher.main(["--dest", str(tmp_path)]) == 0
        assert calls == []  # nothing re-downloaded
    assert "Already present and verified" in capsys.readouterr().out


def test_fetcher_reports_a_failed_download(fetcher, tmp_path, capsys, monkeypatch):
    def boom(url: str, destination: Path, timeout: float) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr(fetcher, "_download", boom)
    assert fetcher.main(["--dest", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "download failed" in err
    assert "physionet.org/content/mimiciii-demo" in err


def test_the_real_demo_file_parses_if_it_has_been_fetched():
    """When the open-access file is present, the importer must handle it.

    Skipped when it is absent -- the file is git-ignored, so CI never has it.
    """
    real = ROOT / "data" / "raw" / "validation" / "D_LABITEMS.csv"
    if not real.is_file():
        pytest.skip("run scripts/fetch_mimic_demo.py to enable this check")

    with real.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # Lowercase headers, despite the uppercase file name and uppercase docs.
    assert set(rows[0]) == {
        "row_id",
        "itemid",
        "label",
        "fluid",
        "category",
        "loinc_code",
    }
    assert len(rows) == 753
    coded = [r for r in rows if (r["loinc_code"] or "").strip()]
    assert len(coded) == 585
    # A label containing a comma survives, so the parser is quote-aware.
    assert any("," in r["label"] for r in rows)


# ---------------------------------------------------------------------------
# upload_loinc_to_snowstorm.py
#
# The upload itself needs a licensed LOINC ZIP, a running Snowstorm and a
# third-party Java CLI, so what is tested here is everything AROUND it: the
# checks that decide whether the upload is even attempted, and the command that
# would be run.
# ---------------------------------------------------------------------------
@pytest.fixture()
def uploader():
    return _script("upload_loinc_to_snowstorm")


@pytest.fixture()
def fake_loinc_zip(tmp_path) -> Path:
    import zipfile

    path = tmp_path / "Loinc_test.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("LoincTable/Loinc.csv", "LOINC_NUM\n1-1\n")
    return path


def test_build_command_matches_the_documented_invocation(uploader, tmp_path):
    command = uploader.build_command(
        Path("hapi-fhir-cli"), tmp_path / "Loinc.zip", "http://localhost:8080/fhir/"
    )
    assert command[:2] == ["hapi-fhir-cli", "upload-terminology"]
    assert "-d" in command and "-v" in command and "-t" in command and "-u" in command
    assert command[command.index("-v") + 1] == "r4"
    # The trailing slash is trimmed rather than doubled.
    assert command[command.index("-t") + 1] == "http://localhost:8080/fhir"
    assert command[command.index("-u") + 1] == "http://loinc.org"


def test_build_command_refuses_a_versioned_system_url(uploader, tmp_path):
    """-u http://loinc.org|2.83 looks reasonable and silently loads nothing."""
    with pytest.raises(uploader.UploadError) as excinfo:
        uploader.build_command(
            Path("cli"),
            tmp_path / "Loinc.zip",
            "http://localhost:8080/fhir",
            system_url="http://loinc.org|2.83",
        )
    assert "concepts.csv" in str(excinfo.value)


@pytest.mark.parametrize(
    "output,expected",
    [
        ('openjdk version "17.0.17" 2026-01-20', 17),
        ('openjdk version "21.0.1" 2023-10-17', 21),
        ('java version "1.8.0_401"', 8),
        ("no version here", None),
    ],
)
def test_java_version_parsing(uploader, monkeypatch, output, expected):
    import subprocess

    monkeypatch.setattr(uploader.shutil, "which", lambda name: "java")
    monkeypatch.setattr(
        uploader.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", output),
    )
    assert uploader.java_major_version() == expected


def test_java_absent_is_reported_not_assumed(uploader, monkeypatch):
    monkeypatch.setattr(uploader.shutil, "which", lambda name: None)
    assert uploader.java_major_version() is None


def test_find_cli_prefers_an_explicit_path(uploader, tmp_path):
    cli = tmp_path / "hapi-fhir-cli.cmd"
    cli.write_text("@echo off", encoding="utf-8")
    assert uploader.find_cli(str(cli)) == cli
    assert uploader.find_cli(str(tmp_path / "missing")) is None


def test_missing_loinc_zip_is_explained(uploader, tmp_path, capsys):
    assert uploader.main(["--file", str(tmp_path / "nope.zip")]) == 2
    err = capsys.readouterr().err
    assert "LOINC Complete" in err


def test_a_non_zip_is_refused(uploader, tmp_path, capsys):
    plain = tmp_path / "Loinc.zip"
    plain.write_text("not a zip", encoding="utf-8")
    assert uploader.main(["--file", str(plain)]) == 2
    assert "not a ZIP archive" in capsys.readouterr().err


def test_old_java_is_refused(uploader, fake_loinc_zip, monkeypatch, capsys):
    monkeypatch.setattr(uploader, "java_major_version", lambda: 11)
    assert uploader.main(["--file", str(fake_loinc_zip)]) == 2
    assert "needs 17 or newer" in capsys.readouterr().err


def test_missing_cli_names_the_download(uploader, fake_loinc_zip, monkeypatch, capsys):
    monkeypatch.setattr(uploader, "java_major_version", lambda: 17)
    monkeypatch.setattr(uploader, "find_cli", lambda explicit=None: None)
    assert uploader.main(["--file", str(fake_loinc_zip)]) == 2
    err = capsys.readouterr().err
    assert "--download-cli" in err
    assert "hapi-fhir" in err


def test_a_down_snowstorm_stops_before_the_upload(
    uploader, fake_loinc_zip, monkeypatch, capsys, tmp_path
):
    """A several-hundred-megabyte upload must not start into a dead server."""
    cli = tmp_path / "hapi-fhir-cli.cmd"
    cli.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(uploader, "java_major_version", lambda: 17)
    monkeypatch.setattr(uploader, "find_cli", lambda explicit=None: cli)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        uploader.subprocess, "run", lambda cmd, **k: ran.append(cmd)
    )
    assert uploader.main(["--file", str(fake_loinc_zip)]) == 1
    assert ran == []
    err = capsys.readouterr().err
    assert "Snowstorm is not reachable" in err
    assert "docker compose up -d" in err


def test_dry_run_prints_the_command_without_uploading(
    uploader, fake_loinc_zip, monkeypatch, capsys, tmp_path
):
    cli = tmp_path / "hapi-fhir-cli.cmd"
    cli.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(uploader, "java_major_version", lambda: 17)
    monkeypatch.setattr(uploader, "find_cli", lambda explicit=None: cli)

    class FakeHealth:
        available = True
        version = "10.4.2"
        detail = None

    class FakeClient:
        base_url = "http://localhost:8080"

        def health(self):
            return FakeHealth()

        def close(self):
            pass

    monkeypatch.setattr(uploader, "SnowstormClient", lambda *a, **k: FakeClient())
    ran: list[list[str]] = []
    monkeypatch.setattr(uploader.subprocess, "run", lambda cmd, **k: ran.append(cmd))

    assert uploader.main(["--file", str(fake_loinc_zip), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "upload-terminology" in out
    assert "Dry run: nothing uploaded." in out
    assert ran == []


def test_verification_code_comes_from_the_imported_release(uploader, loinc_session):
    """The post-upload check must not hard-code a famous LOINC code."""
    loinc_session.commit()
    code, version = uploader.verification_code(loinc_session)
    assert version == fx.LOINC_NEW_VERSION
    assert code in {fx.L_ACTIVE, fx.L_NEW}


def test_verification_code_is_none_without_a_release(uploader, session):
    assert uploader.verification_code(session) == (None, None)


# ---------------------------------------------------------------------------
# check_no_hardcoded_versions.py
#
# The guard that keeps a "version-aware" system version-aware. It has to catch
# the realistic violation (a string constant) while leaving prose alone, so both
# directions are tested.
# ---------------------------------------------------------------------------
@pytest.fixture()
def version_guard():
    return _script("check_no_hardcoded_versions")


def _scan(guard, source: str):
    return guard.scan_source(source, Path("sample.py"))


def test_a_hard_coded_loinc_version_string_is_caught(version_guard):
    violations = _scan(version_guard, 'CURRENT = "2.82"\n')
    assert len(violations) == 1
    assert violations[0].value == "2.82"
    assert violations[0].context == "string literal"


def test_a_hard_coded_snomed_date_is_caught(version_guard):
    for source in ('RELEASE = "20260801"\n', "RELEASE = 20260801\n"):
        violations = _scan(version_guard, source)
        assert len(violations) == 1, source
        assert violations[0].value == "20260801"


def test_a_version_buried_in_an_f_string_is_caught(version_guard):
    violations = _scan(version_guard, 'url = f"/loinc/2.83/{code}"\n')
    assert len(violations) == 1


def test_a_version_in_a_default_argument_is_caught(version_guard):
    violations = _scan(version_guard, 'def load(version="2.82"):\n    return version\n')
    assert len(violations) == 1


def test_a_docstring_may_cite_a_version(version_guard):
    source = '"""Import a LOINC release, for example 2.82 or 20260801."""\n\nX = 1\n'
    assert _scan(version_guard, source) == []


def test_a_function_docstring_may_cite_a_version(version_guard):
    source = 'def f():\n    """Defaults to 2.82."""\n    return 1\n'
    assert _scan(version_guard, source) == []


def test_argparse_help_may_cite_a_version(version_guard):
    source = (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        'p.add_argument("--version", help="release version, e.g. 2.82")\n'
    )
    assert _scan(version_guard, source) == []


def test_a_comment_is_not_scanned(version_guard):
    assert _scan(version_guard, "# release 2.82 was the last biannual one\nX = 1\n") == []


def test_unrelated_numbers_are_not_flagged(version_guard):
    source = (
        "TIMEOUT = 2.5\n"
        "CHUNK = 10000\n"
        'REFSET = "900000000000523009"\n'
        'SHA = "c573653bd06915e48a5fb5f3db01d75554350ec1a628aa91d01ef36daa4eae7f"\n'
        "PORT = 5432\n"
    )
    assert _scan(version_guard, source) == []


def test_a_snomed_refset_id_is_not_mistaken_for_a_date(version_guard):
    """900000000000509007 contains 8-digit runs but is not a release date."""
    assert _scan(version_guard, 'US = "900000000000509007"\n') == []


def test_the_real_source_tree_is_clean(version_guard, capsys):
    assert version_guard.main([]) == 0
    assert "No hard-coded release identifiers" in capsys.readouterr().out


def test_a_violation_makes_the_guard_fail(version_guard, tmp_path, capsys):
    offender = tmp_path / "bad.py"
    offender.write_text('LOINC_VERSION = "2.82"\n', encoding="utf-8")
    assert version_guard.main(["--path", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "bad.py:1" in out
    assert "Hard Rules 1-3" in out


def test_a_missing_path_is_reported(version_guard, tmp_path, capsys):
    assert version_guard.main(["--path", str(tmp_path / "nope")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_an_unparsable_file_warns_without_crashing(version_guard, tmp_path, capsys):
    broken = tmp_path / "broken.py"
    broken.write_text("def (\n", encoding="utf-8")
    assert version_guard.main(["--path", str(tmp_path)]) == 0
    assert "could not parse" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# mimic_impact_report.py
#
# Turns a code count into a data-volume number. The arithmetic has to be right,
# and it must never read anything but itemid out of the result table.
# ---------------------------------------------------------------------------
@pytest.fixture()
def impact(tmp_path):
    module = _script("mimic_impact_report")

    audit = tmp_path / "audit.csv"
    with audit.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["mapping_id", "source_dataset", "local_code", "local_text",
             "target_system", "old_code", "target_display",
             "mapped_against_version", "current_version", "terminology_status",
             "decision", "reason", "suggested_targets", "metadata_changed", "notes"]
        )
        writer.writerow([1, "MIMIC_III", "100", "Common valid test", "LOINC",
                         "1111-1", "", "", "9.02", "CURRENT_VALID", "KEEP",
                         "STATUS_ACTIVE", "", "", ""])
        writer.writerow([2, "MIMIC_III", "200", "Busy deprecated test", "LOINC",
                         "2222-2", "", "", "9.02", "DEPRECATED",
                         "SUGGEST_REPLACEMENT", "SINGLE_OFFICIAL_REPLACEMENT",
                         "3333-3", "", ""])
        writer.writerow([3, "MIMIC_III", "300", "Ambiguous test", "LOINC",
                         "4444-4", "", "", "9.02", "DISCOURAGED",
                         "MANUAL_REVIEW", "MULTIPLE_REPLACEMENTS",
                         "5555-5;6666-6", "", ""])

    def make_labevents(counts: dict[str, int], as_zip: bool = False) -> Path:
        rows = ["row_id,subject_id,hadm_id,itemid,charttime,value,valuenum,valueuom,flag"]
        n = 0
        for itemid, times in counts.items():
            for _ in range(times):
                n += 1
                rows.append(f"{n},99,88,{itemid},2100-01-01,1.0,1.0,mg/dL,")
        body = "\n".join(rows) + "\n"
        if as_zip:
            import zipfile

            path = tmp_path / "mimic.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("some/folder/LABEVENTS.csv", body)
            return path
        path = tmp_path / "LABEVENTS.csv"
        path.write_text(body, encoding="utf-8")
        return path

    module.audit_csv = audit
    module.make_labevents = make_labevents
    return module


def test_impact_weights_the_audit_by_observed_use(impact, tmp_path, capsys):
    labevents = impact.make_labevents({"100": 90, "200": 8, "300": 2})
    out = tmp_path / "impact.md"
    assert (
        impact.main(
            ["--labevents", str(labevents), "--audit", str(impact.audit_csv),
             "--out", str(out), "--csv-out", str(tmp_path / "impact.csv")]
        )
        == 0
    )
    text = out.read_text(encoding="utf-8")
    # 10 of 100 rows rest on a stale mapping; 8 auto-suggestable, 2 for a human.
    assert "10 result rows (10.00%) rest on a mapping that is no longer valid" in text
    assert "8 (8.00%) have exactly one official replacement" in text
    assert "2 (2.00%) need a human decision" in text
    # Ordered by volume, so the busy one leads.
    assert text.index("Busy deprecated test") < text.index("Ambiguous test")


def test_impact_counts_unmapped_itemids_separately(impact, tmp_path):
    labevents = impact.make_labevents({"100": 50, "999": 50})
    out = tmp_path / "impact.md"
    assert (
        impact.main(
            ["--labevents", str(labevents), "--audit", str(impact.audit_csv),
             "--out", str(out), "--csv-out", str(tmp_path / "impact.csv")]
        )
        == 0
    )
    text = out.read_text(encoding="utf-8")
    assert "no LOINC code at all: **50,000**" not in text
    assert "(no LOINC mapping) | 50 | 50.00%" in text


def test_impact_reads_labevents_from_inside_a_zip(impact, tmp_path):
    labevents = impact.make_labevents({"100": 5, "200": 5}, as_zip=True)
    out = tmp_path / "impact.md"
    assert (
        impact.main(
            ["--labevents", str(labevents), "--audit", str(impact.audit_csv),
             "--out", str(out), "--csv-out", str(tmp_path / "impact.csv")]
        )
        == 0
    )
    assert "5 result rows (50.00%)" in out.read_text(encoding="utf-8")


def test_impact_per_itemid_csv_is_written(impact, tmp_path):
    labevents = impact.make_labevents({"100": 3, "200": 1})
    csv_out = tmp_path / "impact.csv"
    impact.main(
        ["--labevents", str(labevents), "--audit", str(impact.audit_csv),
         "--out", str(tmp_path / "impact.md"), "--csv-out", str(csv_out)]
    )
    rows = {r["itemid"]: r for r in csv.DictReader(csv_out.open(encoding="utf-8"))}
    assert rows["100"]["result_rows"] == "3"
    assert rows["200"]["decision"] == "SUGGEST_REPLACEMENT"
    assert rows["200"]["suggested_replacements"] == "3333-3"


def test_impact_refuses_a_table_without_itemid(impact, tmp_path, capsys):
    bad = tmp_path / "LABEVENTS.csv"
    bad.write_text("row_id,value\n1,2\n", encoding="utf-8")
    assert (
        impact.main(
            ["--labevents", str(bad), "--audit", str(impact.audit_csv),
             "--out", str(tmp_path / "o.md"), "--csv-out", str(tmp_path / "o.csv")]
        )
        == 1
    )
    assert "no 'itemid' column" in capsys.readouterr().err


def test_impact_explains_a_missing_audit(impact, tmp_path, capsys):
    labevents = impact.make_labevents({"100": 1})
    assert (
        impact.main(
            ["--labevents", str(labevents), "--audit", str(tmp_path / "nope.csv"),
             "--out", str(tmp_path / "o.md")]
        )
        == 1
    )
    assert "audit_mappings.py" in capsys.readouterr().err


def test_impact_reports_a_missing_labevents_file(impact, tmp_path, capsys):
    assert (
        impact.main(
            ["--labevents", str(tmp_path / "nope.csv"), "--audit", str(impact.audit_csv)]
        )
        == 2
    )
    assert "does not exist" in capsys.readouterr().err
