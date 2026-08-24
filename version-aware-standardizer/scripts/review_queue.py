"""The human half of the loop: export what needs a decision, apply the answers.

    python scripts/review_queue.py export --latest
    #   -> data/reports/review_queue.csv, one row per mapping the engine
    #      declined to decide or wants to change, with a blank
    #      approve_target_code column

    #   ... a person opens it and copies engine_suggested_code into
    #       approve_target_code for the rows they actually agree with ...

    python scripts/review_queue.py apply --file data/reports/review_queue.csv \
        --reviewer "dr-name" --dry-run
    python scripts/review_queue.py apply --file data/reports/review_queue.csv \
        --reviewer "dr-name"

Why a CSV round trip rather than an interactive prompt: the decision has to be
attributable and re-readable months later.  A file that a named person edited,
kept alongside the audit it came from, is evidence; a terminal session is not.

Two columns, deliberately separate:

``engine_suggested_code``   what the engine would propose -- informational only,
                            never read back by ``apply``
``approve_target_code``     what the reviewer decided -- **always exported
                            blank**

Pre-filling the second one would mean an unedited round trip silently migrated
every mapping and stamped a named clinician's approval on changes they never
looked at.  Consent has to be an affirmative act, so the reviewer copies the
suggestion across for the rows they accept.

Nothing here weakens the safety contract.  Every applied row goes through
``mapping_service.approve_replacement``, so the target must still be valid in
the current release, the old code and its release version are preserved on a new
revision, and a blank ``approve_target_code`` means "leave it alone".
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.constants import Decision  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import audit_service, mapping_service  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.review_queue")

QUEUE_COLUMNS = [
    "mapping_id",
    "audit_result_id",
    "source_dataset",
    "local_code",
    "local_text",
    "target_system",
    "current_code",
    "mapped_against_version",
    "current_version",
    "terminology_status",
    "decision",
    "reason",
    "suggested_targets",
    # Informational: what the engine would propose. Never read back.
    "engine_suggested_code",
    "engine_note",
    # The two columns a human fills in:
    "approve_target_code",
    "reviewer_note",
]

OUTCOME_COLUMNS = [
    "mapping_id",
    "old_code",
    "requested_target_code",
    "outcome",
    "detail",
]

DEFAULT_DECISIONS = (
    Decision.MANUAL_REVIEW.value,
    Decision.SUGGEST_REPLACEMENT.value,
    Decision.UNKNOWN_CODE.value,
)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def _parse_decisions(raw: str) -> list[str]:
    """Split and validate ``--decisions``.

    An unrecognised value would otherwise match no row and produce a
    header-only queue with exit code 0 -- a silent "nothing to review" that is
    indistinguishable from a genuinely clean audit.
    """
    decisions = [d.strip().upper() for d in raw.split(",") if d.strip()]
    if not decisions:
        raise ValueError("--decisions must name at least one decision.")
    known = {d.value for d in Decision}
    unknown = [d for d in decisions if d not in known]
    if unknown:
        raise ValueError(
            f"unrecognised decision(s) {unknown}. Valid values: {sorted(known)}"
        )
    return decisions


def cmd_export(args: argparse.Namespace) -> int:
    try:
        decisions = _parse_decisions(args.decisions)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else settings.reports_path / "review_queue.csv"

    # A queue a human has already filled in must not be silently clobbered.
    if out_path.exists() and not args.force:
        backup = out_path.with_suffix(out_path.suffix + ".bak")
        shutil.copyfile(out_path, backup)
        print(f"NOTE: {out_path.name} already existed; kept a copy at {backup.name}.")
        print("      Pass --force to overwrite without a backup.")

    with SessionLocal() as session:
        if args.run_id:
            run = audit_service.get_run(session, args.run_id)
            if run is None:
                print(f"ERROR: no audit run with id {args.run_id}.", file=sys.stderr)
                return 1
        else:
            runs = audit_service.list_runs(session, limit=1)
            if not runs:
                print(
                    "ERROR: no audit run exists yet. Run "
                    "scripts/audit_mappings.py first.",
                    file=sys.stderr,
                )
                return 1
            run = runs[0]

        print(f"Audit run:   {run.id}")
        print(f"LOINC:       {run.loinc_version or '(none)'}")
        print(f"SNOMED_CT:   {run.snomed_version or '(none)'}")
        print(f"Decisions:   {', '.join(decisions)}")

        rows: list[dict] = []
        for decision in decisions:
            for result in audit_service.list_results(
                session, run.id, decision=decision, limit=100_000
            ):
                mapping = (
                    mapping_service.get_mapping(session, result.mapping_id)
                    if result.mapping_id
                    else None
                )
                targets = result.suggested_targets_json or []
                usable = [
                    str(t.get("code") or t.get("concept_id"))
                    for t in targets
                    if t.get("usable")
                ]
                details = (result.metadata_json or {}).get("details", {}) or {}
                rows.append(
                    {
                        "mapping_id": result.mapping_id or "",
                        "audit_result_id": result.id,
                        "source_dataset": mapping.source_dataset if mapping else "",
                        "local_code": mapping.local_code if mapping else "",
                        "local_text": mapping.local_text if mapping else "",
                        "target_system": result.target_system,
                        "current_code": result.old_code,
                        "mapped_against_version": (
                            mapping.mapped_against_version if mapping else ""
                        )
                        or "",
                        "current_version": result.current_version or "",
                        "terminology_status": result.terminology_status,
                        "decision": result.decision,
                        "reason": result.reason or "",
                        "suggested_targets": ";".join(
                            str(t.get("code") or t.get("concept_id") or "")
                            for t in targets
                        ),
                        # What the engine would propose, for the reviewer to
                        # copy across if they agree. Never read back by apply.
                        "engine_suggested_code": (
                            usable[0]
                            if result.decision == Decision.SUGGEST_REPLACEMENT.value
                            and len(usable) == 1
                            else ""
                        ),
                        "engine_note": details.get("message")
                        or details.get("warning")
                        or "",
                        # Always blank. Consent is an affirmative act.
                        "approve_target_code": "",
                        "reviewer_note": "",
                    }
                )

        rows.sort(key=lambda r: (str(r["decision"]), str(r["mapping_id"])))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=QUEUE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    offered = sum(1 for r in rows if r["engine_suggested_code"])
    print()
    print(f"Wrote {len(rows)} row(s) to {out_path}")
    print(f"  with a single official replacement on offer: {offered}")
    print(f"  needing a code you choose yourself:          {len(rows) - offered}")
    print()
    print("approve_target_code is blank on every row, on purpose. Copy")
    print("engine_suggested_code across where you agree, type your own code where")
    print("you do not, and leave it blank to keep the existing mapping. Then:")
    print(
        f'  python scripts/review_queue.py apply --file "{out_path}" '
        f'--reviewer "your name" --dry-run'
    )
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def cmd_apply(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        return 2

    reviewer = args.reviewer.strip()
    if not reviewer:
        print(
            "ERROR: --reviewer is required; an approval must be attributable.",
            file=sys.stderr,
        )
        return 2

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [
            c
            for c in ("mapping_id", "approve_target_code")
            if c not in (reader.fieldnames or [])
        ]
        if missing:
            print(
                f"ERROR: {path.name} is missing column(s) {missing}. "
                f"Export the queue with: python scripts/review_queue.py export --latest",
                file=sys.stderr,
            )
            return 2
        rows = list(reader)

    requested = [r for r in rows if (r.get("approve_target_code") or "").strip()]
    print(f"File:      {path}")
    print(f"Reviewer:  {reviewer}")
    print(f"Rows:      {len(rows)} total, {len(requested)} with a target to apply")
    if args.dry_run:
        print("Mode:      DRY RUN -- every check runs, nothing is written")
    print()

    outcomes: list[dict] = []
    applied = rejected = skipped = 0

    with SessionLocal() as session:
        for row in requested:
            raw_id = (row.get("mapping_id") or "").strip()
            target = (row.get("approve_target_code") or "").strip()
            note = (row.get("reviewer_note") or "").strip()
            audit_result_id = (row.get("audit_result_id") or "").strip()

            if not raw_id.isdigit():
                rejected += 1
                outcomes.append(
                    {
                        "mapping_id": raw_id,
                        "old_code": row.get("current_code", ""),
                        "requested_target_code": target,
                        "outcome": "REJECTED",
                        "detail": f"mapping_id {raw_id!r} is not an integer",
                    }
                )
                continue

            mapping_id = int(raw_id)
            try:
                mapping = mapping_service.get_mapping(session, mapping_id)
            except mapping_service.MappingNotFoundError as exc:
                rejected += 1
                outcomes.append(
                    {
                        "mapping_id": mapping_id,
                        "old_code": "",
                        "requested_target_code": target,
                        "outcome": "REJECTED",
                        "detail": str(exc),
                    }
                )
                continue

            old_code = mapping.target_code
            if old_code == target:
                skipped += 1
                outcomes.append(
                    {
                        "mapping_id": mapping_id,
                        "old_code": old_code,
                        "requested_target_code": target,
                        "outcome": "SKIPPED",
                        "detail": "already points at that code",
                    }
                )
                continue

            # A dry run takes the identical path, including every pre-approval
            # check, and rolls back instead of committing. Anything less and the
            # preview could disagree with the real run -- which is exactly what
            # a preview exists to rule out.
            try:
                revision = mapping_service.approve_replacement(
                    session,
                    mapping_id=mapping_id,
                    target_code=target,
                    reviewer=reviewer,
                    reason=note or None,
                    audit_result_id=(
                        int(audit_result_id) if audit_result_id.isdigit() else None
                    ),
                    allow_unsuggested=args.allow_unsuggested,
                )
                detail = (
                    f"{revision.old_target_code}@{revision.old_target_version} -> "
                    f"{revision.new_target_code}@{revision.new_target_version}"
                )
                if args.dry_run:
                    session.rollback()
                    outcome = "WOULD_APPLY"
                else:
                    session.commit()
                    outcome = "APPLIED"
                applied += 1
                outcomes.append(
                    {
                        "mapping_id": mapping_id,
                        "old_code": old_code,
                        "requested_target_code": target,
                        "outcome": outcome,
                        "detail": detail,
                    }
                )
            except mapping_service.ReplacementRejected as exc:
                # Refused on purpose. Roll back this row only and keep going, so
                # one bad line does not discard a whole review session.
                session.rollback()
                rejected += 1
                outcomes.append(
                    {
                        "mapping_id": mapping_id,
                        "old_code": old_code,
                        "requested_target_code": target,
                        "outcome": "REJECTED",
                        "detail": str(exc),
                    }
                )

    # A dry run must never overwrite the outcome file of a real run.
    if args.out:
        out_path = Path(args.out)
    elif args.dry_run:
        out_path = settings.reports_path / f"{path.stem}_dryrun_outcome.csv"
    else:
        out_path = settings.reports_path / f"{path.stem}_outcome.csv"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTCOME_COLUMNS)
        writer.writeheader()
        writer.writerows(outcomes)

    verb = "would apply" if args.dry_run else "applied"
    print(f"  {verb}:  {applied}")
    print(f"  skipped:  {skipped}")
    print(f"  rejected: {rejected}")
    print()
    print(f"Outcome written to {out_path}")

    if rejected:
        print()
        print("Rejections (the engine refused, on purpose):")
        for outcome in outcomes:
            if outcome["outcome"] == "REJECTED":
                print(f"  mapping {outcome['mapping_id']}: {outcome['detail']}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="write the pending decisions to a CSV")
    group = export.add_mutually_exclusive_group()
    group.add_argument("--run-id", type=int, help="which audit run to export")
    group.add_argument(
        "--latest", action="store_true", help="use the most recent audit run (default)"
    )
    export.add_argument("--out", default=None, help="output CSV path")
    export.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing queue CSV without keeping a .bak copy",
    )
    export.add_argument(
        "--decisions",
        default=",".join(DEFAULT_DECISIONS),
        help="comma-separated decisions to include",
    )
    export.set_defaults(func=cmd_export)

    apply_cmd = sub.add_parser("apply", help="apply the decisions a human filled in")
    apply_cmd.add_argument("--file", required=True, help="the edited review CSV")
    apply_cmd.add_argument(
        "--reviewer", required=True, help="who is approving; recorded on every revision"
    )
    apply_cmd.add_argument("--out", default=None, help="outcome CSV path")
    apply_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and report what would happen, writing nothing",
    )
    apply_cmd.add_argument(
        "--allow-unsuggested",
        action="store_true",
        help=(
            "permit targets the engine never suggested; they must still be valid "
            "in the current release"
        ),
    )
    apply_cmd.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    configure_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
