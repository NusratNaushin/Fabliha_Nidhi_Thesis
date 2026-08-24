"""Import an authorized SNOMED CT International Edition RF2 release.

    python scripts/import_snomed.py \
        --file data/raw/snomed/<RF2_FILE>.zip \
        --version <RELEASE_VERSION>

Two things happen, in this order:

1. the RF2 Snapshot is parsed locally into PostgreSQL (this is what makes the
   version-aware logic reproducible and Snowstorm-independent);
2. optionally, the same archive is handed to Snowstorm as a SNAPSHOT import so
   that term search and preferred terms become available.

SNOMED CT content is licence-controlled. This script never downloads it: the
user obtains the release through their affiliate/member access and places it in
data/raw/snomed/, which is git-ignored.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.constants import ImportStatus  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import release_service  # noqa: E402
from backend.app.services.snomed_rf2_parser import (  # noqa: E402
    Rf2ParseError,
    detect_version,
    ingest_snomed_release,
    version_to_date,
)
from backend.app.services.snowstorm_client import (  # noqa: E402
    SnowstormClient,
    SnowstormError,
)
from backend.app.utils.archive import ArchiveError, ReleaseArchive  # noqa: E402
from backend.app.utils.checksum import sha256_file  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.import_snomed")

# Snowstorm's ImportJob.ImportStatus has exactly four values:
# WAITING_FOR_FILE, RUNNING, COMPLETED, FAILED.
TERMINAL_STATUSES = {"COMPLETED", "FAILED"}
WAITING_FOR_FILE = "WAITING_FOR_FILE"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--effective-date must be YYYY-MM-DD, got {value!r}") from exc


def push_to_snowstorm(
    path: Path,
    branch: str,
    poll_seconds: int,
    timeout_seconds: int,
    waiting_grace_seconds: int = 180,
) -> str | None:
    """Create a SNAPSHOT import job, upload the archive and poll to completion."""
    client = SnowstormClient(branch=branch)
    try:
        health = client.require_available()
        print(f"Snowstorm {health.version or ''} is up at {client.base_url}")

        import_id = client.create_import(
            branch_path=branch, import_type="SNAPSHOT", create_code_system_version=True
        )
        print(f"Import job:  {import_id}")
        print("Uploading RF2 archive (this can take a while) ...")
        client.upload_import_archive(import_id, str(path))

        started = time.monotonic()
        deadline = started + timeout_seconds
        last = None
        while time.monotonic() < deadline:
            payload = client.get_import_status(import_id) or {}
            status = payload.get("status")
            if status != last:
                print(f"  status: {status}")
                last = status
            if status in TERMINAL_STATUSES:
                if status == "FAILED":
                    raise SnowstormError(
                        f"Snowstorm import {import_id} FAILED: "
                        f"{payload.get('errorMessage')}"
                    )
                return import_id

            # A job that is still WAITING_FOR_FILE long after the upload
            # returned means the archive never actually reached Snowstorm.
            # Nothing else will ever move it off that state, so polling for the
            # full timeout would just be a two-hour silence.
            if (
                status == WAITING_FOR_FILE
                and time.monotonic() - started > waiting_grace_seconds
            ):
                raise SnowstormError(
                    f"Snowstorm import {import_id} is still {WAITING_FOR_FILE} "
                    f"{waiting_grace_seconds}s after the upload returned. The "
                    f"archive did not reach the server -- check Snowstorm's "
                    f"logs and its multipart upload size limits "
                    f"(spring.servlet.multipart.max-file-size / "
                    f"max-request-size), then retry."
                )
            if status is None:
                raise SnowstormError(
                    f"Snowstorm no longer knows about import {import_id} "
                    f"(the job endpoint returned nothing). It may have been "
                    f"restarted; re-run the import."
                )
            time.sleep(poll_seconds)
        raise SnowstormError(
            f"Snowstorm import {import_id} did not complete within "
            f"{timeout_seconds}s (last status: {last}). It may still be "
            f"running; check {client.base_url}/imports/{import_id}."
        )
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="path to the RF2 release ZIP")
    parser.add_argument(
        "--version",
        help="release version, normally the RF2 date as YYYYMMDD (e.g. 20260801)",
    )
    parser.add_argument("--effective-date", help="release date as YYYY-MM-DD")
    parser.add_argument(
        "--not-current",
        action="store_true",
        help="import an OLDER release without making it current",
    )
    parser.add_argument(
        "--skip-snowstorm",
        action="store_true",
        help="parse RF2 locally only; do not push the archive to Snowstorm",
    )
    parser.add_argument(
        "--skip-descriptions",
        action="store_true",
        help=(
            "do not resolve preferred terms from the description and language "
            "reference set files. Saves a couple of minutes, at the cost of "
            "reports showing bare concept ids unless Snowstorm is running."
        ),
    )
    parser.add_argument("--branch", default=None, help="Snowstorm branch (default MAIN)")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=7200,
        help="a full International Edition import commonly takes 30-60 minutes",
    )
    parser.add_argument(
        "--waiting-grace-seconds",
        type=int,
        default=180,
        help=(
            "how long the job may stay WAITING_FOR_FILE after the upload before "
            "we treat the upload as having failed"
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        print(
            "Place your licensed SNOMED CT International RF2 ZIP in "
            "data/raw/snomed/ first.",
            file=sys.stderr,
        )
        return 2

    version = args.version
    if not version:
        try:
            with ReleaseArchive(path) as archive:
                version = detect_version(archive)
        except ArchiveError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if not version:
            print(
                "ERROR: could not detect the release date from the archive; "
                "pass --version explicitly.",
                file=sys.stderr,
            )
            return 2
        print(f"Detected SNOMED release version: {version}")

    digest = sha256_file(path)
    print(f"File:      {path}")
    print(f"SHA-256:   {digest}")
    print(f"Version:   {version}")

    with SessionLocal() as session:
        existing = release_service.find_by_checksum(session, "SNOMED_CT", digest)
        if existing is not None:
            print(
                f"Already imported as SNOMED_CT {existing.version} "
                f"(release id {existing.id}). Nothing to do."
            )
            return 0
        try:
            report = ingest_snomed_release(
                session,
                file_path=path,
                version=version,
                effective_date=parse_date(args.effective_date)
                or version_to_date(version),
                make_current=not args.not_current,
                with_descriptions=not args.skip_descriptions,
            )
        except (Rf2ParseError, ArchiveError) as exc:
            session.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except release_service.DuplicateReleaseError as exc:
            session.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print()
    print("SNOMED RF2 parsed locally")
    print("-------------------------")
    print(f"  concepts:        {report.concepts}")
    print(f"  associations:    {report.associations}")
    print(f"  inactivations:   {report.inactivations}")
    if args.skip_descriptions:
        print("  concept terms:   skipped (--skip-descriptions)")
    else:
        print(f"  concept terms:   {report.concept_terms}")
    for warning in report.warnings:
        print(f"  WARNING: {warning}")

    if args.skip_snowstorm:
        print()
        print("Skipping the Snowstorm import (--skip-snowstorm).")
        print(
            "Term search and preferred terms will be unavailable until the "
            "archive is imported into Snowstorm."
        )
        return 0

    print()
    try:
        import_id = push_to_snowstorm(
            path,
            args.branch or "MAIN",
            args.poll_seconds,
            args.timeout_seconds,
            args.waiting_grace_seconds,
        )
    except SnowstormError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "The local RF2 parse succeeded, so audits still work. "
            "Re-run with --skip-snowstorm to suppress this step.",
            file=sys.stderr,
        )
        return 1

    with SessionLocal() as session:
        release = release_service.find_by_checksum(session, "SNOMED_CT", digest)
        if release is not None:
            release_service.mark_status(
                session,
                release,
                ImportStatus.COMPLETED,
                notes=f"{release.notes or ''} snowstorm_import={import_id}".strip(),
            )
            session.commit()

    # Verification that the import is queryable, not merely reported COMPLETED.
    with SnowstormClient(branch=args.branch or "MAIN") as client:
        sample = client.search_concepts("staphylococcus", limit=1)
        print()
        if sample:
            print(f"Verification search returned: {sample[0].get('fsn', {}).get('term')}")
        else:
            print(
                "WARNING: Snowstorm reported COMPLETED but a verification search "
                "returned nothing. Check the branch and the import log."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
