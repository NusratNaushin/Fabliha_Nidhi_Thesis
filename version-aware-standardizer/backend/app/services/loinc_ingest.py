"""Parse an official LOINC Complete release into the database.

Design notes tied to the hard rules:

* Files are found *recursively by basename* -- the folder layout inside the
  official ZIP changes between releases (Master Instruction 13).
* Column names are resolved through alias tables, case-insensitively, and a
  column that an older release does not ship simply stays NULL.  Extra columns
  are ignored rather than fatal (Master Instruction 14).
* Nothing is inferred: STATUS, MapTo targets and Change Snapshot rows are taken
  verbatim from the official files (Hard Rules 16-17).
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy.orm import Session

from backend.app.constants import ImportStatus, TerminologySystem
from backend.app.models import (
    LoincChange,
    LoincConceptVersion,
    LoincMapTo,
    TerminologyRelease,
)
from backend.app.services import release_service
from backend.app.utils.archive import ArchiveError, ReleaseArchive
from backend.app.utils.logging import get_logger

log = get_logger("loinc.ingest")

# Official LOINC CSVs contain long free-text fields (SURVEY_QUEST_TEXT etc.).
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

LOINC_TABLE_FILE = "Loinc.csv"
MAP_TO_FILE = "MapTo.csv"
CHANGE_SNAPSHOT_FILE = "LoincChangeSnapshot.csv"

_INSERT_CHUNK = 5_000


class LoincIngestError(RuntimeError):
    """Raised when a LOINC release cannot be parsed."""


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------
# model attribute -> accepted header spellings, most preferred first.
LOINC_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "loinc_num": ("LOINC_NUM", "LOINC", "LoincNumber"),
    "status": ("STATUS",),
    "long_common_name": ("LONG_COMMON_NAME", "LongCommonName"),
    "short_name": ("SHORTNAME", "SHORT_NAME", "ShortName"),
    "component": ("COMPONENT",),
    "property": ("PROPERTY",),
    "time_aspect": ("TIME_ASPCT", "TIME_ASPECT"),
    "system": ("SYSTEM",),
    "scale_type": ("SCALE_TYP", "SCALE_TYPE"),
    "method_type": ("METHOD_TYP", "METHOD_TYPE"),
    "class_name": ("CLASS",),
    "change_type": ("CHNG_TYPE", "CHANGE_TYPE"),
    "version_first_released": ("VersionFirstReleased", "VERSION_FIRST_RELEASED"),
    "version_last_changed": ("VersionLastChanged", "VERSION_LAST_CHANGED"),
}

MAP_TO_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "source_loinc": ("LOINC", "LOINC_NUM", "SOURCE_LOINC"),
    "target_loinc": ("MAP_TO", "MAPTO", "TARGET_LOINC"),
    "comment": ("COMMENT", "COMMENTS"),
}

# The Change Snapshot does NOT follow the SCREAMING_SNAKE convention the rest of
# the LOINC table uses: the real header is
#   VersionEffective, LOINC_NUM, Property, ValuePrior, ValueCurrent, ChangeReason
# Matching is case-insensitive, so "ValuePrior" and "VALUEPRIOR" both work, but
# "VALUE_PRIOR" does not -- the underscore is a different string. Both spellings
# are listed because older releases used the underscored form.
CHANGE_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "loinc_num": ("LOINC_NUM", "LOINC", "LoincNumber"),
    "property": ("Property", "PROPERTY", "FIELD", "ATTRIBUTE", "PROPERTY_NAME"),
    "value_prior": ("ValuePrior", "VALUE_PRIOR", "PRIOR_VALUE", "OLD_VALUE"),
    "value_current": ("ValueCurrent", "VALUE_CURRENT", "CURRENT_VALUE", "NEW_VALUE"),
    "change_reason": ("ChangeReason", "CHANGE_REASON", "REASON", "COMMENT"),
}

# Only these attributes are mandatory; the rest degrade to NULL.
REQUIRED_LOINC_COLUMNS = ("loinc_num",)
REQUIRED_MAP_TO_COLUMNS = ("source_loinc", "target_loinc")
REQUIRED_CHANGE_COLUMNS = ("loinc_num", "property")


def resolve_columns(
    header: Iterable[str],
    aliases: dict[str, tuple[str, ...]],
    required: tuple[str, ...],
    filename: str,
) -> dict[str, str]:
    """Map model attribute -> actual header name present in this release."""
    present = {h.strip(): h for h in header if h is not None}
    lowered = {h.strip().lower(): h for h in present}

    resolved: dict[str, str] = {}
    for attr, candidates in aliases.items():
        for candidate in candidates:
            actual = lowered.get(candidate.lower())
            if actual is not None:
                resolved[attr] = actual
                break

    missing = [c for c in required if c not in resolved]
    if missing:
        expected = {m: aliases[m] for m in missing}
        raise LoincIngestError(
            f"{filename}: required column(s) {missing} not found. "
            f"Accepted spellings: {expected}. "
            f"Header actually present: {sorted(present)[:25]}"
        )
    absent = [a for a in aliases if a not in resolved]
    if absent:
        log.warning(
            "%s: columns not present in this release, stored as NULL: %s",
            filename,
            absent,
        )
    return resolved


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------
_VERSION_RE = re.compile(r"loinc[_\- ]?v?(\d+\.\d+)", re.IGNORECASE)


def detect_version(archive: ReleaseArchive) -> str | None:
    """Best-effort LOINC version from the file name or internal folder names.

    Returned as a *suggestion* only -- the caller still passes the version
    explicitly, because Hard Rule 1 forbids inferring a release identity that
    then silently becomes authoritative.
    """
    match = _VERSION_RE.search(archive.path.name)
    if match:
        return match.group(1)
    for member in archive.members:
        match = _VERSION_RE.search(member.name)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
@dataclass
class LoincIngestReport:
    version: str
    effective_date: date | None
    source_filename: str
    sha256: str
    release_id: int
    concepts: int = 0
    map_to_rows: int = 0
    change_rows: int = 0
    change_snapshot_present: bool = False
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_date": (
                self.effective_date.isoformat() if self.effective_date else None
            ),
            "source_filename": self.source_filename,
            "sha256": self.sha256,
            "release_id": self.release_id,
            "concepts": self.concepts,
            "map_to_rows": self.map_to_rows,
            "change_rows": self.change_rows,
            "change_snapshot_present": self.change_snapshot_present,
            "skipped": self.skipped,
        }


def _chunks(rows: Iterator[dict], size: int = _INSERT_CHUNK) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _bulk_insert(session: Session, model, rows: Iterator[dict]) -> int:
    total = 0
    for batch in _chunks(rows):
        session.execute(model.__table__.insert(), batch)
        total += len(batch)
    return total


def _iter_concepts(
    archive: ReleaseArchive, release: TerminologyRelease
) -> Iterator[dict]:
    member = archive.require_basename(LOINC_TABLE_FILE)
    log.info("reading %s", member.name)
    with archive.open_text(member) as fh:
        reader = csv.DictReader(fh)
        cols = resolve_columns(
            reader.fieldnames or [],
            LOINC_COLUMN_ALIASES,
            REQUIRED_LOINC_COLUMNS,
            LOINC_TABLE_FILE,
        )
        seen: set[str] = set()
        for row in reader:
            code = _clean(row.get(cols["loinc_num"]))
            if not code or code in seen:
                continue
            seen.add(code)
            record: dict = {
                "release_id": release.id,
                "release_version": release.version,
                "loinc_num": code,
            }
            for attr, header in cols.items():
                if attr == "loinc_num":
                    continue
                record[attr] = _clean(row.get(header))
            yield record


def _iter_map_to(archive: ReleaseArchive, release: TerminologyRelease) -> Iterator[dict]:
    member = archive.require_basename(MAP_TO_FILE)
    log.info("reading %s", member.name)
    with archive.open_text(member) as fh:
        reader = csv.DictReader(fh)
        cols = resolve_columns(
            reader.fieldnames or [],
            MAP_TO_COLUMN_ALIASES,
            REQUIRED_MAP_TO_COLUMNS,
            MAP_TO_FILE,
        )
        seen: set[tuple[str, str]] = set()
        for row in reader:
            source = _clean(row.get(cols["source_loinc"]))
            target = _clean(row.get(cols["target_loinc"]))
            if not source or not target:
                continue
            key = (source, target)
            if key in seen:
                # A duplicated source->target pair inside one release would
                # violate the unique constraint. Distinct targets are kept.
                continue
            seen.add(key)
            yield {
                "release_id": release.id,
                "release_version": release.version,
                "source_loinc": source,
                "target_loinc": target,
                "comment": (
                    _clean(row.get(cols["comment"])) if "comment" in cols else None
                ),
            }


def _iter_changes(archive: ReleaseArchive, release: TerminologyRelease) -> Iterator[dict]:
    member = archive.require_basename(CHANGE_SNAPSHOT_FILE)
    log.info("reading %s", member.name)
    with archive.open_text(member) as fh:
        reader = csv.DictReader(fh)
        cols = resolve_columns(
            reader.fieldnames or [],
            CHANGE_COLUMN_ALIASES,
            REQUIRED_CHANGE_COLUMNS,
            CHANGE_SNAPSHOT_FILE,
        )
        for row in reader:
            code = _clean(row.get(cols["loinc_num"]))
            prop = _clean(row.get(cols["property"]))
            if not code or not prop:
                continue
            yield {
                "release_id": release.id,
                "release_version": release.version,
                "loinc_num": code,
                "property": prop,
                "value_prior": (
                    _clean(row.get(cols["value_prior"]))
                    if "value_prior" in cols
                    else None
                ),
                "value_current": (
                    _clean(row.get(cols["value_current"]))
                    if "value_current" in cols
                    else None
                ),
                "change_reason": (
                    _clean(row.get(cols["change_reason"]))
                    if "change_reason" in cols
                    else None
                ),
            }


def ingest_loinc_release(
    session: Session,
    *,
    file_path: str | Path,
    version: str,
    effective_date: date | None = None,
    make_current: bool = True,
) -> LoincIngestReport:
    """Import one LOINC Complete release. Idempotent by SHA-256."""
    path = Path(file_path)
    with ReleaseArchive(path) as archive:
        # Fail early and loudly if the archive is not a LOINC release.
        for required in (LOINC_TABLE_FILE, MAP_TO_FILE):
            if not archive.find_by_basename(required):
                raise LoincIngestError(
                    f"{path.name} does not look like a LOINC Complete release: "
                    f"{required!r} is missing. Download 'LOINC Complete' from "
                    f"the official LOINC downloads page."
                )

        release = release_service.register_release(
            session,
            system=TerminologySystem.LOINC.value,
            version=version,
            source_path=path,
            effective_date=effective_date,
            make_current=make_current,
        )
        report = LoincIngestReport(
            version=release.version,
            effective_date=release.effective_date,
            source_filename=release.source_filename,
            sha256=release.sha256,
            release_id=release.id,
        )

        report.concepts = _bulk_insert(
            session, LoincConceptVersion, _iter_concepts(archive, release)
        )
        report.map_to_rows = _bulk_insert(
            session, LoincMapTo, _iter_map_to(archive, release)
        )

        if archive.find_by_basename(CHANGE_SNAPSHOT_FILE):
            report.change_snapshot_present = True
            report.change_rows = _bulk_insert(
                session, LoincChange, _iter_changes(archive, release)
            )
        else:
            # Optional for older releases (Master Instruction 13).
            msg = (
                f"{CHANGE_SNAPSHOT_FILE} not present in {path.name}; "
                f"release-to-release validation will rely on the computed diff only."
            )
            log.warning(msg)
            report.skipped.append(msg)

        release_service.mark_status(
            session,
            release,
            ImportStatus.COMPLETED,
            notes=(
                f"concepts={report.concepts} map_to={report.map_to_rows} "
                f"changes={report.change_rows}"
            ),
        )
        session.commit()

    log.info(
        "LOINC %s imported: %d concepts, %d MapTo rows, %d change rows",
        report.version,
        report.concepts,
        report.map_to_rows,
        report.change_rows,
    )
    return report


__all__ = [
    "ArchiveError",
    "LoincIngestError",
    "LoincIngestReport",
    "detect_version",
    "ingest_loinc_release",
    "resolve_columns",
]
