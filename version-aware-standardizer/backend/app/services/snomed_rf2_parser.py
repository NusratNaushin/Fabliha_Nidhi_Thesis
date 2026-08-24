"""Parse the parts of a SNOMED CT RF2 Snapshot that the auditor depends on.

Master Instruction 9 is emphatic: *do not depend only on Snowstorm* for the
version-aware logic.  Snowstorm is excellent at search and preferred terms, but
its branch state is mutable and it does not let us diff release A against
release B offline.  So three RF2 Snapshot files are parsed into PostgreSQL and
stamped with the release version:

* ``sct2_Concept_Snapshot*.txt``            -> snomed_concept_version
* association reference sets                -> snomed_historical_association
* Concept Inactivation Indicator refset     -> snomed_inactivation
* descriptions + language reference set     -> snomed_concept_term

Files are located by *filename pattern*, never by assuming a fixed folder name,
because the RF2 folder layout carries the release date in its name.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Sequence

from sqlalchemy.orm import Session

from backend.app.constants import (
    ACCEPTABILITY_PREFERRED,
    CONCEPT_INACTIVATION_REFSET,
    DEFAULT_LANGUAGE_REFSETS,
    DESCRIPTION_TYPE_FSN,
    DESCRIPTION_TYPE_SYNONYM,
    HISTORICAL_ASSOCIATION_REFSETS,
    ImportStatus,
    TerminologySystem,
)
from backend.app.models import (
    SnomedConceptTerm,
    SnomedConceptVersion,
    SnomedHistoricalAssociation,
    SnomedInactivation,
    TerminologyRelease,
)
from backend.app.services import release_service
from backend.app.utils.archive import ArchiveError, ArchiveMember, ReleaseArchive
from backend.app.utils.logging import get_logger

log = get_logger("snomed.rf2")

CONCEPT_PATTERN = "sct2_Concept_Snapshot*.txt"

# Editions have shipped the association refset under several file names.
ASSOCIATION_PATTERNS: tuple[str, ...] = (
    "der2_cRefset_AssociationSnapshot*.txt",
    "der2_cRefset_AssociationReferenceSetSnapshot*.txt",
    "der2_cRefset_Association*Snapshot*.txt",
)
ATTRIBUTE_VALUE_PATTERNS: tuple[str, ...] = (
    "der2_cRefset_AttributeValueSnapshot*.txt",
    "der2_cRefset_AttributeValue*Snapshot*.txt",
)

# Descriptions carry a language sub-element ("-en"); the language reference set
# does too. Neither pattern matches the provisional "xsct2_"/"xder2_" files that
# ALPHA and BETA packages ship, which is deliberate: those must never be
# imported as if they were a production release.
DESCRIPTION_PATTERNS: tuple[str, ...] = ("sct2_Description_Snapshot*.txt",)
LANGUAGE_REFSET_PATTERNS: tuple[str, ...] = ("der2_cRefset_LanguageSnapshot*.txt",)

_INSERT_CHUNK = 10_000


class Rf2ParseError(RuntimeError):
    """Raised when an RF2 archive is missing required Snapshot files."""


# ---------------------------------------------------------------------------
# Release identity
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\d{8}")


def _first_date_like(text: str) -> str | None:
    """First 8-digit run in ``text`` that is a real calendar date.

    Deliberately not anchored to a century: a hard-coded ``20`` prefix would be
    exactly the kind of assumption this project exists to avoid.
    """
    for match in _DATE_RE.finditer(text):
        candidate = match.group(0)
        if version_to_date(candidate) is not None:
            return candidate
    return None


def detect_version(archive: ReleaseArchive) -> str | None:
    """Best-effort release date (``YYYYMMDD``) from the file or folder names."""
    found = _first_date_like(archive.path.name)
    if found:
        return found
    for member in archive.members:
        found = _first_date_like(member.name)
        if found:
            return found
    return None


def version_to_date(version: str) -> date | None:
    """``20260801`` -> ``date(2026, 8, 1)``; anything else -> ``None``."""
    version = version.strip()
    if len(version) == 8 and version.isdigit():
        try:
            return datetime.strptime(version, "%Y%m%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Low level RF2 reading
# ---------------------------------------------------------------------------
def _find_first(archive: ReleaseArchive, patterns: Sequence[str]) -> ArchiveMember | None:
    for pattern in patterns:
        matches = archive.find_by_pattern(pattern)
        if matches:
            matches.sort(key=lambda m: (m.name.count("/"), len(m.name)))
            return matches[0]
    return None


def _find_all(archive: ReleaseArchive, patterns: Sequence[str]) -> list[ArchiveMember]:
    """Every member matching any pattern, de-duplicated and shallowest first.

    Editions may ship more than one description file (one per language), so the
    parser must read all of them rather than only the first.
    """
    seen: dict[str, ArchiveMember] = {}
    for pattern in patterns:
        for match in archive.find_by_pattern(pattern):
            seen[match.name] = match
    return sorted(seen.values(), key=lambda m: (m.name.count("/"), len(m.name)))


def _read_rf2(archive: ReleaseArchive, member: ArchiveMember) -> Iterator[dict[str, str]]:
    """Yield rows of a tab-separated RF2 file keyed by its own header names."""
    log.info("reading %s", member.name)
    with archive.open_text(member, encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = next(reader)
        except StopIteration:
            raise Rf2ParseError(f"{member.name} is empty") from None
        header = [h.strip() for h in header]
        width = len(header)
        for row in reader:
            if not row:
                continue
            if len(row) < width:
                row = row + [""] * (width - len(row))
            yield dict(zip(header, row))


def _column(row: dict[str, str], *names: str) -> str | None:
    """First present, non-empty value among alternative RF2 column spellings."""
    for name in names:
        value = row.get(name)
        if value is None:
            # RF2 headers are camelCase but be tolerant anyway.
            for key in row:
                if key.lower() == name.lower():
                    value = row[key]
                    break
        if value is not None and value.strip():
            return value.strip()
    return None


def _as_bool(value: str | None) -> bool:
    return str(value).strip() == "1"


# ---------------------------------------------------------------------------
# Row iterators
# ---------------------------------------------------------------------------
def _iter_concepts(
    archive: ReleaseArchive, release: TerminologyRelease
) -> Iterator[dict]:
    member = _find_first(archive, (CONCEPT_PATTERN,))
    if member is None:
        raise Rf2ParseError(
            f"No file matching {CONCEPT_PATTERN!r} in {archive.path.name}. "
            f"This does not look like an RF2 Snapshot/Full release package."
        )
    seen: set[str] = set()
    for row in _read_rf2(archive, member):
        concept_id = _column(row, "id")
        if not concept_id or concept_id in seen:
            continue
        seen.add(concept_id)
        yield {
            "release_id": release.id,
            "release_version": release.version,
            "concept_id": concept_id,
            "effective_time": _column(row, "effectiveTime"),
            "active": _as_bool(_column(row, "active")),
            "module_id": _column(row, "moduleId"),
            "definition_status_id": _column(row, "definitionStatusId"),
        }


def _iter_associations(
    archive: ReleaseArchive, release: TerminologyRelease
) -> Iterator[dict]:
    member = _find_first(archive, ASSOCIATION_PATTERNS)
    if member is None:
        log.warning(
            "no association reference set file found in %s; SNOMED replacement "
            "suggestions will be unavailable for this release",
            archive.path.name,
        )
        return
    known = set(HISTORICAL_ASSOCIATION_REFSETS)
    seen: set[str] = set()
    unknown_refsets: set[str] = set()
    for row in _read_rf2(archive, member):
        refset_id = _column(row, "refsetId")
        if not refset_id:
            continue
        if refset_id not in known:
            unknown_refsets.add(refset_id)
            continue
        member_id = _column(row, "id")
        source = _column(row, "referencedComponentId")
        target = _column(row, "targetComponentId", "targetComponent")
        if not source or not target:
            continue
        if member_id and member_id in seen:
            continue
        if member_id:
            seen.add(member_id)
        yield {
            "release_id": release.id,
            "release_version": release.version,
            "member_id": member_id,
            "refset_id": refset_id,
            "referenced_component_id": source,
            "target_component_id": target,
            "effective_time": _column(row, "effectiveTime"),
            "active": _as_bool(_column(row, "active")),
        }
    if unknown_refsets:
        # Not an error: the association file also carries refsets we do not
        # interpret. Log them so an unexpected new association type is visible.
        log.info(
            "association file contained %d refset id(s) this engine does not "
            "interpret: %s",
            len(unknown_refsets),
            sorted(unknown_refsets)[:10],
        )


def _iter_inactivations(
    archive: ReleaseArchive, release: TerminologyRelease
) -> Iterator[dict]:
    member = _find_first(archive, ATTRIBUTE_VALUE_PATTERNS)
    if member is None:
        log.warning(
            "no attribute value reference set file found in %s; inactivation "
            "reasons will be unavailable for this release",
            archive.path.name,
        )
        return
    seen: set[str] = set()
    for row in _read_rf2(archive, member):
        if _column(row, "refsetId") != CONCEPT_INACTIVATION_REFSET:
            continue
        member_id = _column(row, "id")
        concept_id = _column(row, "referencedComponentId")
        value_id = _column(row, "valueId")
        if not concept_id or not value_id:
            continue
        if member_id and member_id in seen:
            continue
        if member_id:
            seen.add(member_id)
        yield {
            "release_id": release.id,
            "release_version": release.version,
            "member_id": member_id,
            "concept_id": concept_id,
            "value_id": value_id,
            "effective_time": _column(row, "effectiveTime"),
            "active": _as_bool(_column(row, "active")),
        }


def _collect_concept_terms(
    archive: ReleaseArchive,
    release: TerminologyRelease,
    language_refsets: Sequence[str] = DEFAULT_LANGUAGE_REFSETS,
) -> Iterator[dict]:
    """Resolve one FSN and one preferred term per concept.

    Two streaming passes, in this order on purpose:

    1. the language reference set, keeping only the description ids that are an
       ACTIVE, PREFERRED member of a configured dialect;
    2. the description file, keeping the FSN of every active concept and the
       term of every synonym whose id survived pass 1.

    Doing it this way holds roughly one entry per concept in memory instead of
    one per description -- the description file has around 1.4 million rows and
    the language reference set around 2.8 million, so the naive order would cost
    several times more memory for the same answer.

    Where a concept is preferred in more than one dialect, the earlier entry in
    ``language_refsets`` wins (US English before GB English, matching
    Snowstorm's own default ordering).
    """
    description_members = _find_all(archive, DESCRIPTION_PATTERNS)
    if not description_members:
        log.warning(
            "no description Snapshot file in %s; concept display terms will be "
            "unavailable offline",
            archive.path.name,
        )
        return

    priority = {refset: index for index, refset in enumerate(language_refsets)}

    # Pass 1 -- preferred description ids, with the dialect that preferred them.
    preferred: dict[str, int] = {}
    language_members = _find_all(archive, LANGUAGE_REFSET_PATTERNS)
    if not language_members:
        log.warning(
            "no language reference set in %s; falling back to the fully "
            "specified name for every concept",
            archive.path.name,
        )
    for member in language_members:
        for row in _read_rf2(archive, member):
            if not _as_bool(_column(row, "active")):
                continue
            if _column(row, "acceptabilityId") != ACCEPTABILITY_PREFERRED:
                continue
            refset_id = _column(row, "refsetId")
            rank = priority.get(refset_id or "")
            if rank is None:
                continue
            description_id = _column(row, "referencedComponentId")
            if not description_id:
                continue
            existing = preferred.get(description_id)
            if existing is None or rank < existing:
                preferred[description_id] = rank

    # Pass 2 -- the descriptions themselves.
    fsn: dict[str, str] = {}
    pt: dict[str, tuple[int, str]] = {}
    for member in description_members:
        for row in _read_rf2(archive, member):
            if not _as_bool(_column(row, "active")):
                continue
            concept_id = _column(row, "conceptId")
            term = _column(row, "term")
            if not concept_id or not term:
                continue
            type_id = _column(row, "typeId")
            if type_id == DESCRIPTION_TYPE_FSN:
                fsn.setdefault(concept_id, term)
            elif type_id == DESCRIPTION_TYPE_SYNONYM:
                description_id = _column(row, "id")
                rank = preferred.get(description_id or "")
                if rank is None:
                    continue
                current = pt.get(concept_id)
                if current is None or rank < current[0]:
                    pt[concept_id] = (rank, term)

    preferred.clear()
    ordered = list(language_refsets)
    for concept_id in sorted(set(fsn) | set(pt)):
        chosen = pt.get(concept_id)
        yield {
            "release_id": release.id,
            "release_version": release.version,
            "concept_id": concept_id,
            "fsn": fsn.get(concept_id),
            "preferred_term": chosen[1] if chosen else None,
            "language_refset_id": (
                ordered[chosen[0]] if chosen and chosen[0] < len(ordered) else None
            ),
        }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
@dataclass
class Rf2IngestReport:
    version: str
    effective_date: date | None
    source_filename: str
    sha256: str
    release_id: int
    concepts: int = 0
    associations: int = 0
    inactivations: int = 0
    concept_terms: int = 0
    warnings: list[str] = field(default_factory=list)

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
            "associations": self.associations,
            "inactivations": self.inactivations,
            "concept_terms": self.concept_terms,
            "warnings": self.warnings,
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


def read_active_associations(
    archive_path: "str | Path", concept_ids: "Sequence[str] | None" = None
) -> dict[str, set[tuple[str, str]]]:
    """Active historical associations read straight from an RF2 archive.

    Returns ``{source concept id: {(association type, target concept id), ...}}``.

    This bypasses the database on purpose.  Validation that compares the parsed
    tables against the resolver is comparing our own code with our own code; to
    mean anything, the ground truth has to come back off the release file.
    """
    wanted = set(concept_ids) if concept_ids is not None else None
    found: dict[str, set[tuple[str, str]]] = {}
    with ReleaseArchive(archive_path) as archive:
        member = _find_first(archive, ASSOCIATION_PATTERNS)
        if member is None:
            return found
        for row in _read_rf2(archive, member):
            if not _as_bool(_column(row, "active")):
                continue
            refset_id = _column(row, "refsetId")
            association = HISTORICAL_ASSOCIATION_REFSETS.get(refset_id or "")
            if association is None:
                continue
            source = _column(row, "referencedComponentId")
            if not source or (wanted is not None and source not in wanted):
                continue
            target = _column(row, "targetComponentId", "targetComponent")
            if not target:
                continue
            found.setdefault(source, set()).add((association, target))
    return found


def validate_rf2_archive(archive: ReleaseArchive) -> list[str]:
    """Return warnings; raise :class:`Rf2ParseError` if unusable."""
    warnings: list[str] = []
    if _find_first(archive, (CONCEPT_PATTERN,)) is None:
        raise Rf2ParseError(
            f"{archive.path.name} contains no {CONCEPT_PATTERN!r}. "
            f"An RF2 Snapshot package is required."
        )
    if _find_first(archive, ASSOCIATION_PATTERNS) is None:
        warnings.append(
            "association reference set Snapshot file missing: SNOMED replacement "
            "suggestion will always fall back to MANUAL_REVIEW"
        )
    if _find_first(archive, ATTRIBUTE_VALUE_PATTERNS) is None:
        warnings.append(
            "attribute value reference set Snapshot file missing: inactivation "
            "reasons will not be recorded"
        )
    if not _find_all(archive, DESCRIPTION_PATTERNS):
        warnings.append(
            "description Snapshot file missing: display terms will only be "
            "available when Snowstorm is running"
        )
    return warnings


def ingest_snomed_release(
    session: Session,
    *,
    file_path: str | Path,
    version: str,
    effective_date: date | None = None,
    make_current: bool = True,
    with_descriptions: bool = True,
    language_refsets: Sequence[str] = DEFAULT_LANGUAGE_REFSETS,
) -> Rf2IngestReport:
    """Parse one SNOMED CT RF2 release into the local tables.

    ``with_descriptions`` resolves one FSN and one preferred term per concept so
    that reports read in clinical language with Snowstorm switched off.  It adds
    a couple of minutes to a full International Edition import; pass False to
    skip it.
    """
    path = Path(file_path)
    with ReleaseArchive(path) as archive:
        warnings = validate_rf2_archive(archive)

        if effective_date is None:
            effective_date = version_to_date(version)

        release = release_service.register_release(
            session,
            system=TerminologySystem.SNOMED_CT.value,
            version=version,
            source_path=path,
            effective_date=effective_date,
            make_current=make_current,
        )
        report = Rf2IngestReport(
            version=release.version,
            effective_date=release.effective_date,
            source_filename=release.source_filename,
            sha256=release.sha256,
            release_id=release.id,
            warnings=warnings,
        )

        report.concepts = _bulk_insert(
            session, SnomedConceptVersion, _iter_concepts(archive, release)
        )
        report.associations = _bulk_insert(
            session, SnomedHistoricalAssociation, _iter_associations(archive, release)
        )
        report.inactivations = _bulk_insert(
            session, SnomedInactivation, _iter_inactivations(archive, release)
        )
        if with_descriptions:
            report.concept_terms = _bulk_insert(
                session,
                SnomedConceptTerm,
                _collect_concept_terms(archive, release, language_refsets),
            )

        release_service.mark_status(
            session,
            release,
            ImportStatus.PARSED,
            notes=(
                f"concepts={report.concepts} associations={report.associations} "
                f"inactivations={report.inactivations} terms={report.concept_terms}"
            ),
        )
        session.commit()

    log.info(
        "SNOMED %s parsed: %d concepts, %d associations, %d inactivations, %d terms",
        report.version,
        report.concepts,
        report.associations,
        report.inactivations,
        report.concept_terms,
    )
    return report


__all__ = [
    "ArchiveError",
    "DEFAULT_LANGUAGE_REFSETS",
    "read_active_associations",
    "Rf2IngestReport",
    "Rf2ParseError",
    "detect_version",
    "ingest_snomed_release",
    "validate_rf2_archive",
    "version_to_date",
]
