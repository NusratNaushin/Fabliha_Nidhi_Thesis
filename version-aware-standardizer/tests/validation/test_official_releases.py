"""Validation against REAL official releases (Master Instruction 40 and 41).

This is the strongest correctness evidence the project can produce, and it is
the experiment to run once two genuine releases are on disk:

    LOINC:  place two official Complete ZIPs in data/raw/validation/
    SNOMED: place two official RF2 ZIPs in data/raw/validation/

    pytest tests/validation -m validation -v -s

The tests skip -- loudly, with the exact file placement instructions -- when
those files are absent.  They are never allowed to *pass* vacuously, because a
green suite that silently tested nothing is worse than a skip.

Ground truth is taken exclusively from the official files themselves:

* LOINC: the STATUS column of the newer Loinc.csv, its MapTo.csv, and its
  LoincChangeSnapshot.csv;
* SNOMED: the newer edition's Concept Inactivation Indicator refset and its
  historical association refsets.

Nothing is compared against hand-written expectations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.config import ROOT_DIR
from backend.app.constants import Decision
from backend.app.services import loinc_diff, snomed_diff
from backend.app.services.loinc_ingest import ingest_loinc_release
from backend.app.services.loinc_resolver import LoincResolver
from backend.app.services.snomed_rf2_parser import (
    detect_version as detect_snomed_version,
)
from backend.app.services.snomed_rf2_parser import ingest_snomed_release
from backend.app.services.snomed_resolver import SnomedResolver
from backend.app.utils.archive import ReleaseArchive

pytestmark = pytest.mark.validation

VALIDATION_DIR = ROOT_DIR / "data" / "raw" / "validation"

PLACEMENT_HELP = f"""
Place two official releases in {VALIDATION_DIR} and re-run.

  LOINC  : two 'LOINC Complete' ZIPs, e.g. Loinc_<older>.zip and Loinc_<newer>.zip
           (free account required at the official LOINC downloads page)
  SNOMED : two International Edition RF2 ZIPs, e.g.
           SnomedCT_InternationalRF2_PRODUCTION_<older>T*.zip and <newer>
           (licensed access required -- never bypass the licence)

Terminology archives are git-ignored and are never committed.
"""


def _loinc_archives() -> list[Path]:
    # Recursive: people organise their downloads into subfolders, and this
    # project's whole premise is not to assume a layout.
    if not VALIDATION_DIR.exists():
        return []
    return sorted(VALIDATION_DIR.rglob("Loinc*.zip"))


def _snomed_archives() -> list[Path]:
    if not VALIDATION_DIR.exists():
        return []
    return sorted(VALIDATION_DIR.rglob("SnomedCT*.zip"))


def _loinc_version(path: Path) -> str:
    from backend.app.services.loinc_ingest import detect_version

    with ReleaseArchive(path) as archive:
        version = detect_version(archive)
    if not version:
        pytest.fail(f"Could not detect a LOINC version from {path.name}")
    return version


# ---------------------------------------------------------------------------
# LOINC
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_loinc(session_factory_module):
    archives = _loinc_archives()
    if len(archives) < 2:
        pytest.skip(
            f"Need two official LOINC releases, found {len(archives)}.\n"
            + PLACEMENT_HELP
        )
    session = session_factory_module
    versions = []
    for index, path in enumerate(archives[:2]):
        version = _loinc_version(path)
        ingest_loinc_release(
            session,
            file_path=path,
            version=version,
            make_current=(index == len(archives[:2]) - 1),
        )
        versions.append(version)
    versions.sort(key=lambda v: [int(p) for p in v.split(".")])
    return session, versions[0], versions[1]


def test_computed_diff_reproduces_every_official_change(real_loinc):
    """Target: missed official changes = 0 for the supported fields."""
    session, old_version, new_version = real_loinc
    report = loinc_diff.diff_releases(
        session,
        old_version=old_version,
        new_version=new_version,
        report_name=f"validation_loinc_{old_version}_to_{new_version}.csv",
    )
    print("\n" + report.render())

    validation = report.validation
    if not validation.change_snapshot_available:
        pytest.skip(
            f"LOINC {new_version} shipped no LoincChangeSnapshot.csv; "
            f"the official-change comparison cannot run."
        )
    assert validation.official_changes > 0, (
        "The Change Snapshot contained no comparable rows -- the comparison "
        "would pass vacuously."
    )
    assert validation.missed_count == 0, (
        f"{validation.missed_count} official change(s) were not detected, "
        f"e.g. {validation.missed_changes[:5]}"
    )


def test_status_changed_codes_are_all_rediscovered(real_loinc):
    """Simulate historical mappings with the OLD release, audit against the NEW."""
    session, old_version, new_version = real_loinc
    transitions = loinc_diff.status_change_codes(
        session, old_version=old_version, new_version=new_version
    )
    became_obsolete = [
        code
        for transition, codes in transitions.items()
        if transition.startswith("ACTIVE ->")
        and transition.split("-> ")[1] in {"DISCOURAGED", "DEPRECATED"}
        for code in codes
    ]
    if not became_obsolete:
        pytest.skip(
            f"No ACTIVE -> DISCOURAGED/DEPRECATED transition between "
            f"{old_version} and {new_version}; pick a wider release pair."
        )

    resolver = LoincResolver(session)
    resolver.preload(became_obsolete)

    missed: list[str] = []
    invented: list[str] = []
    for code in became_obsolete:
        result = resolver.resolve(code, mapped_against_version=old_version)
        if result.decision in (Decision.KEEP, Decision.KEEP_WITH_WARNING):
            missed.append(code)
        official_targets = {m.target_loinc for m in resolver.get_map_to(code)}
        for target in result.suggested_targets:
            # Chained targets are legitimate; the chain must still start from
            # an officially declared MapTo row.
            if target.via and target.via[1] not in official_targets:
                invented.append(f"{code} -> {target.code}")

    print(
        f"\nchecked {len(became_obsolete)} ACTIVE -> obsolete transitions "
        f"between LOINC {old_version} and {new_version}"
    )
    assert missed == [], f"{len(missed)} status changes were not detected: {missed[:5]}"
    assert invented == [], f"replacements not backed by MapTo: {invented[:5]}"


def test_no_replacement_is_invented_anywhere(real_loinc):
    """Every suggested target must exist in the official MapTo of that release."""
    session, _old_version, new_version = real_loinc
    from sqlalchemy import select

    from backend.app.models import LoincConceptVersion

    obsolete = list(
        session.scalars(
            select(LoincConceptVersion.loinc_num).where(
                LoincConceptVersion.release_version == new_version,
                LoincConceptVersion.status.in_(["DISCOURAGED", "DEPRECATED"]),
            )
        )
    )
    if not obsolete:
        pytest.skip(f"LOINC {new_version} has no DISCOURAGED/DEPRECATED codes.")

    resolver = LoincResolver(session)
    resolver.preload(obsolete)
    offenders: list[str] = []
    suggested_count = 0
    for code in obsolete:
        result = resolver.resolve(code)
        if result.decision is not Decision.SUGGEST_REPLACEMENT:
            continue
        suggested_count += 1
        first_hop = result.suggested_targets[0].via[1]
        if first_hop not in {m.target_loinc for m in resolver.get_map_to(code)}:
            offenders.append(f"{code} -> {first_hop}")

    print(
        f"\n{suggested_count} of {len(obsolete)} obsolete codes had exactly one "
        f"official replacement"
    )
    assert offenders == [], f"invented replacements: {offenders[:5]}"


# ---------------------------------------------------------------------------
# SNOMED CT
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_snomed(session_factory_module):
    archives = _snomed_archives()
    if len(archives) < 2:
        pytest.skip(
            f"Need two official SNOMED CT RF2 releases, found {len(archives)}.\n"
            + PLACEMENT_HELP
        )
    session = session_factory_module
    versions = []
    for index, path in enumerate(archives[:2]):
        with ReleaseArchive(path) as archive:
            version = detect_snomed_version(archive)
        if not version:
            pytest.fail(f"Could not detect a release date from {path.name}")
        ingest_snomed_release(
            session,
            file_path=path,
            version=version,
            make_current=(index == len(archives[:2]) - 1),
        )
        versions.append(version)
    versions.sort()
    return session, versions[0], versions[1]


def test_every_newly_inactive_concept_is_detected(real_snomed):
    session, old_version, new_version = real_snomed
    report = snomed_diff.diff_releases(
        session,
        old_version=old_version,
        new_version=new_version,
        report_name=f"validation_snomed_{old_version}_to_{new_version}.csv",
    )
    print("\n" + report.render())

    if not report.became_inactive:
        pytest.skip(
            f"No concept became inactive between {old_version} and "
            f"{new_version}; pick a wider release pair."
        )
    assert report.inactive_detection_recall == 1.0
    assert report.unsafe_auto_update == 0


def test_association_extraction_matches_the_official_refset(real_snomed):
    """What the engine reports must equal what the RELEASE FILE says.

    Ground truth is re-read from the archive rather than from the tables we
    populated from it: comparing the database with the resolver would compare
    our own parser with itself and report 100% however badly it behaved.
    """
    session, old_version, new_version = real_snomed
    from backend.app.services.snomed_rf2_parser import read_active_associations

    archives = _snomed_archives()
    newer_archive = archives[1]

    report = snomed_diff.diff_releases(
        session,
        old_version=old_version,
        new_version=new_version,
        export_csv=False,
    )
    if not report.became_inactive:
        pytest.skip("No newly inactive concept to check.")

    sample = report.became_inactive[:500]
    official = read_active_associations(newer_archive, sample)

    resolver = SnomedResolver(session)
    resolver.preload(sample)

    mismatches: list[str] = []
    for concept_id in sample:
        expected = official.get(concept_id, set())
        extracted = {
            (a.association_type, a.target_component_id)
            for a in resolver.resolve(concept_id).associations
        }
        if expected != extracted:
            mismatches.append(concept_id)

    print(
        f"\nchecked historical associations for {len(sample)} concepts "
        f"against {newer_archive.name}"
    )
    assert mismatches == [], f"association extraction mismatch: {mismatches[:5]}"


def test_only_replaced_by_and_same_as_are_ever_auto_suggested(real_snomed):
    session, old_version, new_version = real_snomed
    report = snomed_diff.diff_releases(
        session, old_version=old_version, new_version=new_version, export_csv=False
    )
    if not report.became_inactive:
        pytest.skip("No newly inactive concept to check.")

    resolver = SnomedResolver(session)
    sample = report.became_inactive[:500]
    resolver.preload(sample)

    offenders: list[str] = []
    for concept_id in sample:
        result = resolver.resolve(concept_id)
        if result.decision is not Decision.SUGGEST_REPLACEMENT:
            continue
        if len(result.associations) != 1:
            offenders.append(f"{concept_id}: {len(result.associations)} associations")
        elif result.associations[0].association_type not in {"REPLACED_BY", "SAME_AS"}:
            offenders.append(
                f"{concept_id}: {result.associations[0].association_type}"
            )
    assert offenders == [], f"unsafe auto-suggestions: {offenders[:5]}"


# ---------------------------------------------------------------------------
# Column-name coverage against the real archives
#
# Regression cover for a fault only real data exposed: the Change Snapshot does
# NOT use the SCREAMING_SNAKE names the rest of the LOINC table uses. Its real
# header is VersionEffective, LOINC_NUM, Property, ValuePrior, ValueCurrent,
# ChangeReason -- so aliases spelled VALUE_PRIOR silently matched nothing and
# three columns imported as NULL.
# ---------------------------------------------------------------------------
def test_every_modelled_column_resolves_in_the_real_archives():
    import csv as _csv

    from backend.app.services.loinc_ingest import (
        CHANGE_COLUMN_ALIASES,
        CHANGE_SNAPSHOT_FILE,
        LOINC_COLUMN_ALIASES,
        LOINC_TABLE_FILE,
        MAP_TO_COLUMN_ALIASES,
        MAP_TO_FILE,
        REQUIRED_CHANGE_COLUMNS,
        REQUIRED_LOINC_COLUMNS,
        REQUIRED_MAP_TO_COLUMNS,
        resolve_columns,
    )

    archives = _loinc_archives()
    if not archives:
        pytest.skip("No official LOINC archive present.\n" + PLACEMENT_HELP)

    cases = [
        (LOINC_TABLE_FILE, LOINC_COLUMN_ALIASES, REQUIRED_LOINC_COLUMNS),
        (MAP_TO_FILE, MAP_TO_COLUMN_ALIASES, REQUIRED_MAP_TO_COLUMNS),
        (CHANGE_SNAPSHOT_FILE, CHANGE_COLUMN_ALIASES, REQUIRED_CHANGE_COLUMNS),
    ]

    unresolved: list[str] = []
    for path in archives:
        with ReleaseArchive(path) as archive:
            for filename, aliases, required in cases:
                if not archive.find_by_basename(filename):
                    continue
                member = archive.require_basename(filename)
                with archive.open_text(member) as fh:
                    header = next(_csv.reader(fh))
                resolved = resolve_columns(header, aliases, required, filename)
                missing = sorted(set(aliases) - set(resolved))
                if missing:
                    unresolved.append(f"{path.name}/{filename}: {missing}")
                print(f"  {path.name}/{filename}: {len(resolved)}/{len(aliases)} columns")

    assert unresolved == [], (
        "Modelled columns that real releases do not match, so they would import "
        f"as NULL: {unresolved}"
    )


def test_the_canonical_table_is_chosen_when_a_name_appears_twice():
    """Real LOINC ships Loinc.csv twice: LoincTable/ and AccessoryFiles/PanelsAndForms/.

    The shallowest-path rule has to pick the canonical full table, not the
    panels subset -- silently importing the wrong one would corrupt everything
    downstream while looking entirely healthy.
    """
    archives = _loinc_archives()
    if not archives:
        pytest.skip("No official LOINC archive present.\n" + PLACEMENT_HELP)

    with ReleaseArchive(archives[0]) as archive:
        matches = [m.name for m in archive.find_by_basename("Loinc.csv")]
        chosen = archive.require_basename("Loinc.csv").name
        print(f"\n  candidates: {matches}\n  chosen:     {chosen}")
        if len(matches) > 1:
            assert chosen.startswith("LoincTable/"), (
                f"picked {chosen}, which is not the canonical table"
            )
        assert not chosen.startswith("AccessoryFiles/")
