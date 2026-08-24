"""Synthetic terminology releases used by the unit tests and the demo.

Everything in this module is invented.  No licensed LOINC or SNOMED CT content
is copied into the repository (Master Instruction 49): the codes are made up,
the descriptions are made up, and the only thing borrowed from the real
terminologies is the *file format* and the published metadata refset ids, which
are part of the specification rather than of the content.

Two releases are produced per terminology so that release-to-release behaviour
can be exercised end to end:

    OLD -> everything ACTIVE (the "historical mapping" world)
    NEW -> a curated set of transitions covering every branch of the decision
           tables: single replacement, multiple replacements, no replacement,
           chained replacement, cyclic replacement, metadata-only drift.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from backend.app.constants import (
    ACCEPTABILITY_ACCEPTABLE,
    ACCEPTABILITY_PREFERRED,
    CONCEPT_INACTIVATION_REFSET,
    DESCRIPTION_TYPE_FSN,
    DESCRIPTION_TYPE_SYNONYM,
    LANGUAGE_REFSET_GB_ENGLISH,
    LANGUAGE_REFSET_US_ENGLISH,
)

# ---------------------------------------------------------------------------
# Version labels. Deliberately NOT real release numbers: the engine must never
# depend on a particular version string.
# ---------------------------------------------------------------------------
LOINC_OLD_VERSION = "9.01"
LOINC_NEW_VERSION = "9.02"
SNOMED_OLD_VERSION = "29990101"
SNOMED_NEW_VERSION = "29990201"

# ---------------------------------------------------------------------------
# LOINC codes used across the tests
# ---------------------------------------------------------------------------
L_ACTIVE = "11111-1"        # ACTIVE in both releases
L_TRIAL = "22222-2"         # TRIAL in both releases
L_DISC_ONE = "33333-3"      # ACTIVE -> DISCOURAGED, exactly one MapTo
L_DISC_MANY = "44444-4"     # ACTIVE -> DISCOURAGED, two MapTo targets
L_DEP_ONE = "55555-5"       # ACTIVE -> DEPRECATED, exactly one MapTo
L_DEP_NONE = "66666-6"      # ACTIVE -> DEPRECATED, no MapTo at all
L_CHAIN_HEAD = "77777-7"    # DEPRECATED -> 88888-8 -> 11111-1
L_CHAIN_MID = "88888-8"     # DEPRECATED -> 11111-1
L_CYCLE_A = "99999-9"       # DEPRECATED -> 10101-0
L_CYCLE_B = "10101-0"       # DEPRECATED -> 99999-9  (cycle)
L_META = "12121-2"          # stays ACTIVE, metadata changes
L_NEW = "13131-3"           # only exists in the NEW release
L_DEP_TO_TRIAL = "14141-4"  # DEPRECATED, its only MapTo target is TRIAL
L_UNKNOWN = "00000-0"       # never present in any release

LOINC_HEADER = [
    "LOINC_NUM",
    "COMPONENT",
    "PROPERTY",
    "TIME_ASPCT",
    "SYSTEM",
    "SCALE_TYP",
    "METHOD_TYP",
    "CLASS",
    "STATUS",
    "SHORTNAME",
    "LONG_COMMON_NAME",
    "VersionFirstReleased",
    "VersionLastChanged",
    "CHNG_TYPE",
    # An extra column no model field maps to: the parser must tolerate it.
    "EXTRA_VENDOR_COLUMN",
]


def _loinc_row(
    code: str,
    *,
    status: str,
    component: str = "Synthetic component",
    long_name: str | None = None,
    short_name: str | None = None,
    system: str = "Ser/Plas",
    method: str = "",
    version_last_changed: str = LOINC_OLD_VERSION,
) -> list[str]:
    return [
        code,
        component,
        "MCnc",
        "Pt",
        system,
        "Qn",
        method,
        "CHEM",
        status,
        short_name or f"Synth {code}",
        long_name or f"Synthetic test term {code}",
        LOINC_OLD_VERSION,
        version_last_changed,
        "MIN",
        "ignored",
    ]


def loinc_old_rows() -> list[list[str]]:
    """The historical release: every mapped code is ACTIVE."""
    return [
        _loinc_row(L_ACTIVE, status="ACTIVE"),
        _loinc_row(L_TRIAL, status="TRIAL"),
        _loinc_row(L_DISC_ONE, status="ACTIVE"),
        _loinc_row(L_DISC_MANY, status="ACTIVE"),
        _loinc_row(L_DEP_ONE, status="ACTIVE"),
        _loinc_row(L_DEP_NONE, status="ACTIVE"),
        _loinc_row(L_CHAIN_HEAD, status="ACTIVE"),
        _loinc_row(L_CHAIN_MID, status="ACTIVE"),
        _loinc_row(L_CYCLE_A, status="ACTIVE"),
        _loinc_row(L_CYCLE_B, status="ACTIVE"),
        _loinc_row(L_DEP_TO_TRIAL, status="ACTIVE"),
        _loinc_row(
            L_META,
            status="ACTIVE",
            component="Haemoglobin",
            long_name="Haemoglobin [Mass/volume] in Blood",
        ),
    ]


def loinc_new_rows() -> list[list[str]]:
    """The current release: the transitions the auditor must discover."""
    return [
        _loinc_row(L_ACTIVE, status="ACTIVE"),
        _loinc_row(L_TRIAL, status="TRIAL"),
        _loinc_row(L_DISC_ONE, status="DISCOURAGED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_DISC_MANY, status="DISCOURAGED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_DEP_ONE, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_DEP_NONE, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_CHAIN_HEAD, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_CHAIN_MID, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_CYCLE_A, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(L_CYCLE_B, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION),
        _loinc_row(
            L_DEP_TO_TRIAL, status="DEPRECATED", version_last_changed=LOINC_NEW_VERSION
        ),
        # Same code, same status -- only the display metadata moved.
        _loinc_row(
            L_META,
            status="ACTIVE",
            component="Hemoglobin",
            long_name="Hemoglobin [Mass/volume] in Blood",
            version_last_changed=LOINC_NEW_VERSION,
        ),
        _loinc_row(L_NEW, status="ACTIVE", version_last_changed=LOINC_NEW_VERSION),
    ]


def loinc_new_map_to() -> list[list[str]]:
    """MapTo.csv of the NEW release: LOINC, MAP_TO, COMMENT."""
    return [
        [L_DISC_ONE, L_ACTIVE, "single official replacement"],
        [L_DISC_MANY, L_ACTIVE, "one of two candidates"],
        [L_DISC_MANY, L_TRIAL, "one of two candidates"],
        [L_DEP_ONE, L_ACTIVE, "single official replacement"],
        [L_CHAIN_HEAD, L_CHAIN_MID, "superseded once"],
        [L_CHAIN_MID, L_ACTIVE, "superseded again"],
        [L_CYCLE_A, L_CYCLE_B, "deliberately cyclic fixture"],
        [L_CYCLE_B, L_CYCLE_A, "deliberately cyclic fixture"],
        [L_DEP_TO_TRIAL, L_TRIAL, "only replacement is itself TRIAL"],
    ]


def loinc_new_changes() -> list[list[str]]:
    """LoincChangeSnapshot.csv of the NEW release.

    This is the *official* change log our computed diff is validated against.
    Newly created terms are intentionally absent -- a new code has no prior
    state to diff.
    """
    rows = [
        [LOINC_NEW_VERSION, code, "STATUS", "ACTIVE", status, "editorial review"]
        for code, status in [
            (L_DISC_ONE, "DISCOURAGED"),
            (L_DISC_MANY, "DISCOURAGED"),
            (L_DEP_ONE, "DEPRECATED"),
            (L_DEP_NONE, "DEPRECATED"),
            (L_CHAIN_HEAD, "DEPRECATED"),
            (L_CHAIN_MID, "DEPRECATED"),
            (L_CYCLE_A, "DEPRECATED"),
            (L_CYCLE_B, "DEPRECATED"),
            (L_DEP_TO_TRIAL, "DEPRECATED"),
        ]
    ]
    rows.append(
        [
            LOINC_NEW_VERSION,
            L_META,
            "LONG_COMMON_NAME",
            "Haemoglobin [Mass/volume] in Blood",
            "Hemoglobin [Mass/volume] in Blood",
            "spelling harmonisation",
        ]
    )
    rows.append(
        [
            LOINC_NEW_VERSION,
            L_META,
            "COMPONENT",
            "Haemoglobin",
            "Hemoglobin",
            "spelling harmonisation",
        ]
    )
    return rows


CHANGE_HEADER = [
    "VERSION",
    "LOINC_NUM",
    "PROPERTY",
    "VALUE_PRIOR",
    "VALUE_CURRENT",
    "CHANGE_REASON",
]
MAP_TO_HEADER = ["LOINC", "MAP_TO", "COMMENT"]


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    # utf-8-sig: real LOINC CSVs ship a BOM, so the parser must cope with one.
    return buffer.getvalue().encode("utf-8-sig")


def write_loinc_release(
    directory: Path,
    *,
    version: str,
    rows: list[list[str]],
    map_to: list[list[str]] | None = None,
    changes: list[list[str]] | None = None,
    include_change_snapshot: bool = True,
) -> Path:
    """Write a synthetic ``Loinc_<version>.zip`` shaped like the real package."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"Loinc_{version}.zip"
    root = f"Loinc_{version}"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/LoincTable/Loinc.csv", _csv_bytes(LOINC_HEADER, rows))
        zf.writestr(
            f"{root}/AccessoryFiles/MapTo/MapTo.csv",
            _csv_bytes(MAP_TO_HEADER, map_to or []),
        )
        if include_change_snapshot:
            zf.writestr(
                f"{root}/AccessoryFiles/ChangeSnapshot/LoincChangeSnapshot.csv",
                _csv_bytes(CHANGE_HEADER, changes or []),
            )
        zf.writestr(f"{root}/read_me.txt", b"synthetic fixture -- not real LOINC")
    return path


def write_loinc_old(directory: Path) -> Path:
    return write_loinc_release(
        directory,
        version=LOINC_OLD_VERSION,
        rows=loinc_old_rows(),
        map_to=[],
        changes=[],
    )


def write_loinc_new(directory: Path) -> Path:
    return write_loinc_release(
        directory,
        version=LOINC_NEW_VERSION,
        rows=loinc_new_rows(),
        map_to=loinc_new_map_to(),
        changes=loinc_new_changes(),
    )


# ---------------------------------------------------------------------------
# SNOMED CT RF2
# ---------------------------------------------------------------------------
S_ACTIVE = "100000001"          # active in both releases
S_REPLACED = "100000002"        # active -> inactive, single REPLACED BY
S_SAME_AS = "100000003"         # inactive, single SAME AS
S_POSSIBLY = "100000004"        # inactive, POSSIBLY EQUIVALENT TO
S_WAS_A = "100000005"           # inactive, WAS A
S_MULTI = "100000006"           # inactive, two associations
S_ACTIVE_2 = "100000007"        # active
S_NO_ASSOC = "100000008"        # inactive, no association at all
S_CHAIN_HEAD = "100000009"      # inactive -> 100000010 -> 100000001
S_CHAIN_MID = "100000010"       # inactive -> 100000001
S_CYCLE_A = "100000011"         # inactive -> 100000012
S_CYCLE_B = "100000012"         # inactive -> 100000011  (cycle)
S_MOVED = "100000013"           # inactive, MOVED TO
S_MOVED_TARGET = "100000014"    # active
S_DANGLING = "100000015"        # inactive, REPLACED BY a concept not in release
S_MISSING_TARGET = "199999999"  # never present in any release
S_UNKNOWN = "123456789"         # never present in any release

REFSET_POSSIBLY_EQUIVALENT = "900000000000523009"
REFSET_MOVED_TO = "900000000000524003"
REFSET_REPLACED_BY = "900000000000526001"
REFSET_SAME_AS = "900000000000527005"
REFSET_WAS_A = "900000000000528000"

VALUE_OUTDATED = "900000000000483008"
VALUE_AMBIGUOUS = "900000000000484002"
VALUE_ERRONEOUS = "900000000000485001"
VALUE_MOVED_ELSEWHERE = "900000000000487009"

MODULE = "900000000000207008"
DEFINITION_STATUS = "900000000000074008"

CONCEPT_HEADER = ["id", "effectiveTime", "active", "moduleId", "definitionStatusId"]
ASSOCIATION_HEADER = [
    "id",
    "effectiveTime",
    "active",
    "moduleId",
    "refsetId",
    "referencedComponentId",
    "targetComponentId",
]
ATTRIBUTE_VALUE_HEADER = [
    "id",
    "effectiveTime",
    "active",
    "moduleId",
    "refsetId",
    "referencedComponentId",
    "valueId",
]

ALL_CONCEPTS = [
    S_ACTIVE,
    S_REPLACED,
    S_SAME_AS,
    S_POSSIBLY,
    S_WAS_A,
    S_MULTI,
    S_ACTIVE_2,
    S_NO_ASSOC,
    S_CHAIN_HEAD,
    S_CHAIN_MID,
    S_CYCLE_A,
    S_CYCLE_B,
    S_MOVED,
    S_MOVED_TARGET,
    S_DANGLING,
]

# Concepts that are inactive in the NEW release.
INACTIVE_IN_NEW = {
    S_REPLACED,
    S_SAME_AS,
    S_POSSIBLY,
    S_WAS_A,
    S_MULTI,
    S_NO_ASSOC,
    S_CHAIN_HEAD,
    S_CHAIN_MID,
    S_CYCLE_A,
    S_CYCLE_B,
    S_MOVED,
    S_DANGLING,
}


def snomed_concept_rows(version: str, *, inactive: set[str]) -> list[list[str]]:
    return [
        [
            concept_id,
            version,
            "0" if concept_id in inactive else "1",
            MODULE,
            DEFINITION_STATUS,
        ]
        for concept_id in ALL_CONCEPTS
    ]


def snomed_association_rows(version: str) -> list[list[str]]:
    """Active association members of the NEW release, plus one inactive member.

    The inactive member exists on purpose: it must be ignored when suggesting a
    current replacement (Master Instruction 10, last line).
    """
    definitions: list[tuple[str, str, str, str]] = [
        # (member id, refset, source, target)
        ("a001", REFSET_REPLACED_BY, S_REPLACED, S_ACTIVE),
        ("a002", REFSET_SAME_AS, S_SAME_AS, S_ACTIVE),
        ("a003", REFSET_POSSIBLY_EQUIVALENT, S_POSSIBLY, S_ACTIVE),
        ("a004", REFSET_WAS_A, S_WAS_A, S_ACTIVE),
        ("a005", REFSET_REPLACED_BY, S_MULTI, S_ACTIVE),
        ("a006", REFSET_SAME_AS, S_MULTI, S_ACTIVE_2),
        ("a007", REFSET_REPLACED_BY, S_CHAIN_HEAD, S_CHAIN_MID),
        ("a008", REFSET_REPLACED_BY, S_CHAIN_MID, S_ACTIVE),
        ("a009", REFSET_REPLACED_BY, S_CYCLE_A, S_CYCLE_B),
        ("a010", REFSET_REPLACED_BY, S_CYCLE_B, S_CYCLE_A),
        ("a011", REFSET_MOVED_TO, S_MOVED, S_MOVED_TARGET),
        ("a012", REFSET_REPLACED_BY, S_DANGLING, S_MISSING_TARGET),
    ]
    rows = [
        [member_id, version, "1", MODULE, refset, source, target]
        for member_id, refset, source, target in definitions
    ]
    # A withdrawn association member for a concept that has no other link.
    rows.append(["a013", version, "0", MODULE, REFSET_REPLACED_BY, S_NO_ASSOC, S_ACTIVE])
    return rows


def snomed_attribute_value_rows(version: str) -> list[list[str]]:
    reasons = {
        S_REPLACED: VALUE_OUTDATED,
        S_SAME_AS: VALUE_OUTDATED,
        S_POSSIBLY: VALUE_AMBIGUOUS,
        S_WAS_A: VALUE_OUTDATED,
        S_MULTI: VALUE_AMBIGUOUS,
        S_NO_ASSOC: VALUE_ERRONEOUS,
        S_CHAIN_HEAD: VALUE_OUTDATED,
        S_CHAIN_MID: VALUE_OUTDATED,
        S_CYCLE_A: VALUE_OUTDATED,
        S_CYCLE_B: VALUE_OUTDATED,
        S_MOVED: VALUE_MOVED_ELSEWHERE,
        S_DANGLING: VALUE_OUTDATED,
    }
    return [
        [
            f"v{index:03d}",
            version,
            "1",
            MODULE,
            CONCEPT_INACTIVATION_REFSET,
            concept_id,
            value_id,
        ]
        for index, (concept_id, value_id) in enumerate(sorted(reasons.items()), start=1)
    ]


# ---------------------------------------------------------------------------
# Descriptions and the language reference set
#
# Enough shape to exercise every branch of the offline preferred-term
# resolution: a US/GB disagreement, a GB-only preference, an ACCEPTABLE synonym
# that must lose to the PREFERRED one, an INACTIVE preferred row that must be
# ignored, and a concept with no synonym at all so display falls back to the FSN.
# ---------------------------------------------------------------------------
DESCRIPTION_HEADER = [
    "id",
    "effectiveTime",
    "active",
    "moduleId",
    "conceptId",
    "languageCode",
    "typeId",
    "term",
    "caseSignificanceId",
]
LANGUAGE_HEADER = [
    "id",
    "effectiveTime",
    "active",
    "moduleId",
    "refsetId",
    "referencedComponentId",
    "acceptabilityId",
]
CASE_SIGNIFICANCE = "900000000000448009"

# concept -> the preferred term each dialect should end up with.
US_PREFERRED_TERMS = {
    S_ACTIVE: "Synthetic organism alpha",
    S_REPLACED: "Synthetic superseded finding",
    S_SAME_AS: "Synthetic renamed finding",
    S_POSSIBLY: "Synthetic ambiguous finding",
    S_CHAIN_HEAD: "Synthetic chained finding",
    S_MOVED_TARGET: "Synthetic moved target",
}
GB_ONLY_PREFERRED_TERMS = {
    # No US preference at all: the GB dialect must supply the display term.
    S_ACTIVE_2: "Synthetic organism beta (GB)",
}
# A US/GB disagreement on the same concept: US must win.
GB_COMPETING_TERMS = {
    S_ACTIVE: "Synthetic organism alpha (GB spelling)",
}
# Preferred in the refset, but the refset row is inactive -> must be ignored.
WITHDRAWN_PREFERRED = {S_WAS_A: "Withdrawn preferred term"}
# FSN only, no synonym -> display falls back to the FSN.
FSN_ONLY = {S_NO_ASSOC}


def snomed_description_rows(version: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, concept_id in enumerate(ALL_CONCEPTS, start=1):
        rows.append(
            [
                f"d{index:03d}f",
                version,
                "1",
                MODULE,
                concept_id,
                "en",
                DESCRIPTION_TYPE_FSN,
                f"Synthetic concept {concept_id} (finding)",
                CASE_SIGNIFICANCE,
            ]
        )
        if concept_id in FSN_ONLY:
            continue
        term = US_PREFERRED_TERMS.get(concept_id)
        if term:
            rows.append(
                [
                    f"d{index:03d}p",
                    version,
                    "1",
                    MODULE,
                    concept_id,
                    "en",
                    DESCRIPTION_TYPE_SYNONYM,
                    term,
                    CASE_SIGNIFICANCE,
                ]
            )
        gb_term = GB_ONLY_PREFERRED_TERMS.get(concept_id) or GB_COMPETING_TERMS.get(
            concept_id
        )
        if gb_term:
            rows.append(
                [
                    f"d{index:03d}g",
                    version,
                    "1",
                    MODULE,
                    concept_id,
                    "en",
                    DESCRIPTION_TYPE_SYNONYM,
                    gb_term,
                    CASE_SIGNIFICANCE,
                ]
            )
        withdrawn = WITHDRAWN_PREFERRED.get(concept_id)
        if withdrawn:
            rows.append(
                [
                    f"d{index:03d}w",
                    version,
                    "1",
                    MODULE,
                    concept_id,
                    "en",
                    DESCRIPTION_TYPE_SYNONYM,
                    withdrawn,
                    CASE_SIGNIFICANCE,
                ]
            )
        # An ACCEPTABLE synonym exists for every concept that has any synonym.
        rows.append(
            [
                f"d{index:03d}a",
                version,
                "1",
                MODULE,
                concept_id,
                "en",
                DESCRIPTION_TYPE_SYNONYM,
                f"Acceptable alias for {concept_id}",
                CASE_SIGNIFICANCE,
            ]
        )
    # An inactive description must never surface, even if preferred.
    rows.append(
        [
            "d999x",
            version,
            "0",
            MODULE,
            S_ACTIVE,
            "en",
            DESCRIPTION_TYPE_SYNONYM,
            "Retired spelling that must not appear",
            CASE_SIGNIFICANCE,
        ]
    )
    return rows


def snomed_language_rows(version: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, concept_id in enumerate(ALL_CONCEPTS, start=1):
        if concept_id in FSN_ONLY:
            continue
        if US_PREFERRED_TERMS.get(concept_id):
            rows.append(
                [
                    f"l{index:03d}p",
                    version,
                    "1",
                    MODULE,
                    LANGUAGE_REFSET_US_ENGLISH,
                    f"d{index:03d}p",
                    ACCEPTABILITY_PREFERRED,
                ]
            )
        if GB_ONLY_PREFERRED_TERMS.get(concept_id) or GB_COMPETING_TERMS.get(concept_id):
            rows.append(
                [
                    f"l{index:03d}g",
                    version,
                    "1",
                    MODULE,
                    LANGUAGE_REFSET_GB_ENGLISH,
                    f"d{index:03d}g",
                    ACCEPTABILITY_PREFERRED,
                ]
            )
        if WITHDRAWN_PREFERRED.get(concept_id):
            rows.append(
                [
                    f"l{index:03d}w",
                    version,
                    "0",  # inactive refset member
                    MODULE,
                    LANGUAGE_REFSET_US_ENGLISH,
                    f"d{index:03d}w",
                    ACCEPTABILITY_PREFERRED,
                ]
            )
        rows.append(
            [
                f"l{index:03d}a",
                version,
                "1",
                MODULE,
                LANGUAGE_REFSET_US_ENGLISH,
                f"d{index:03d}a",
                ACCEPTABILITY_ACCEPTABLE,
            ]
        )
    # A preferred member pointing at an inactive description.
    rows.append(
        [
            "l999x",
            version,
            "1",
            MODULE,
            LANGUAGE_REFSET_US_ENGLISH,
            "d999x",
            ACCEPTABILITY_PREFERRED,
        ]
    )
    # A dialect this engine does not read must be ignored entirely.
    rows.append(
        [
            "l998z",
            version,
            "1",
            MODULE,
            "900000000000999999",
            "d001a",
            ACCEPTABILITY_PREFERRED,
        ]
    )
    return rows


def _tsv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def write_snomed_release(
    directory: Path,
    *,
    version: str,
    inactive: set[str],
    with_associations: bool = True,
    with_attribute_values: bool = True,
    with_descriptions: bool = True,
) -> Path:
    """Write a synthetic RF2 Snapshot ZIP with the real folder/file conventions."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"SnomedCT_SyntheticRF2_PRODUCTION_{version}T120000Z.zip"
    root = f"SnomedCT_SyntheticRF2_PRODUCTION_{version}T120000Z"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{root}/Snapshot/Terminology/sct2_Concept_Snapshot_SYN_{version}.txt",
            _tsv_bytes(CONCEPT_HEADER, snomed_concept_rows(version, inactive=inactive)),
        )
        if with_associations:
            zf.writestr(
                f"{root}/Snapshot/Refset/Content/"
                f"der2_cRefset_AssociationSnapshot_SYN_{version}.txt",
                _tsv_bytes(ASSOCIATION_HEADER, snomed_association_rows(version)),
            )
        if with_attribute_values:
            zf.writestr(
                f"{root}/Snapshot/Refset/Content/"
                f"der2_cRefset_AttributeValueSnapshot_SYN_{version}.txt",
                _tsv_bytes(
                    ATTRIBUTE_VALUE_HEADER, snomed_attribute_value_rows(version)
                ),
            )
        if with_descriptions:
            zf.writestr(
                f"{root}/Snapshot/Terminology/"
                f"sct2_Description_Snapshot-en_SYN_{version}.txt",
                _tsv_bytes(DESCRIPTION_HEADER, snomed_description_rows(version)),
            )
            zf.writestr(
                f"{root}/Snapshot/Refset/Language/"
                f"der2_cRefset_LanguageSnapshot-en_SYN_{version}.txt",
                _tsv_bytes(LANGUAGE_HEADER, snomed_language_rows(version)),
            )
        zf.writestr(f"{root}/Readme.txt", b"synthetic fixture -- not real SNOMED CT")
    return path


def write_snomed_old(directory: Path) -> Path:
    """Historical release: every concept is active."""
    return write_snomed_release(
        directory, version=SNOMED_OLD_VERSION, inactive=set()
    )


def write_snomed_new(directory: Path) -> Path:
    return write_snomed_release(
        directory, version=SNOMED_NEW_VERSION, inactive=INACTIVE_IN_NEW
    )


__all__ = [
    "DESCRIPTION_HEADER",
    "LANGUAGE_HEADER",
    "LOINC_NEW_VERSION",
    "LOINC_OLD_VERSION",
    "SNOMED_NEW_VERSION",
    "SNOMED_OLD_VERSION",
    "write_loinc_new",
    "write_loinc_old",
    "write_loinc_release",
    "write_snomed_new",
    "write_snomed_old",
    "write_snomed_release",
]
