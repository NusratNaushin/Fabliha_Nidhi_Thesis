"""Import an official LOINC Complete release.

    python scripts/import_loinc.py \
        --file data/raw/loinc/<LOINC_RELEASE>.zip \
        --version <VERSION> \
        --effective-date <YYYY-MM-DD>

The release ZIP is obtained by the user from the official LOINC downloads page
(a free account is required); this script never downloads terminology content.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.database import SessionLocal  # noqa: E402
from backend.app.services import release_service  # noqa: E402
from backend.app.services.loinc_ingest import (  # noqa: E402
    LoincIngestError,
    detect_version,
    ingest_loinc_release,
)
from backend.app.utils.archive import ArchiveError, ReleaseArchive  # noqa: E402
from backend.app.utils.checksum import sha256_file  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.import_loinc")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(
            f"--effective-date must be YYYY-MM-DD, got {value!r}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="path to the LOINC release ZIP")
    parser.add_argument(
        "--version",
        help=(
            "release version, e.g. 2.82. Omit only with --detect-version, which "
            "reads it from the package itself."
        ),
    )
    parser.add_argument(
        "--detect-version",
        action="store_true",
        help="derive the version from the archive name/contents and print it",
    )
    parser.add_argument("--effective-date", help="release date as YYYY-MM-DD")
    parser.add_argument(
        "--not-current",
        action="store_true",
        help=(
            "import without making this release current -- use it to load an "
            "OLDER release for the release-to-release validation experiment"
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} does not exist.", file=sys.stderr)
        print(
            "Place the official 'LOINC Complete' ZIP in data/raw/loinc/ first.",
            file=sys.stderr,
        )
        return 2

    version = args.version
    if not version or args.detect_version:
        try:
            with ReleaseArchive(path) as archive:
                detected = detect_version(archive)
        except ArchiveError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if detected:
            print(f"Detected LOINC version: {detected}")
        if not version:
            if not detected:
                print(
                    "ERROR: could not detect the LOINC version from the archive; "
                    "pass --version explicitly.",
                    file=sys.stderr,
                )
                return 2
            version = detected

    digest = sha256_file(path)
    print(f"File:      {path}")
    print(f"SHA-256:   {digest}")
    print(f"Version:   {version}")

    with SessionLocal() as session:
        existing = release_service.find_by_checksum(session, "LOINC", digest)
        if existing is not None:
            # Idempotency (Master Instruction 44/45): a re-run of the same
            # content is a no-op, not a duplicate release.
            print(
                f"Already imported as LOINC {existing.version} "
                f"(release id {existing.id}, file {existing.source_filename}). "
                f"Nothing to do."
            )
            return 0
        try:
            report = ingest_loinc_release(
                session,
                file_path=path,
                version=version,
                effective_date=parse_date(args.effective_date),
                make_current=not args.not_current,
            )
        except (LoincIngestError, ArchiveError) as exc:
            session.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except release_service.DuplicateReleaseError as exc:
            session.rollback()
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print()
    print("LOINC import complete")
    print("---------------------")
    print(f"  concepts:        {report.concepts}")
    print(f"  MapTo rows:      {report.map_to_rows}")
    print(f"  change rows:     {report.change_rows}")
    print(f"  current release: {not args.not_current}")
    for note in report.skipped:
        print(f"  NOTE: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
