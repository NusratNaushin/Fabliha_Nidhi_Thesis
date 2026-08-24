"""Official terminology constants and the decision vocabulary of this project.

Nothing here is a *release* identifier -- these are stable published artefact
identifiers (SNOMED reference-set concept ids, LOINC status literals) that do
not change from release to release.  Release versions always come from import
metadata (Hard Rules 1-3).
"""

from __future__ import annotations

from enum import StrEnum


class TerminologySystem(StrEnum):
    """Terminology systems handled by this project."""

    LOINC = "LOINC"
    SNOMED_CT = "SNOMED_CT"


class LoincStatus(StrEnum):
    """LOINC published STATUS values (Loinc.csv STATUS column)."""

    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    DISCOURAGED = "DISCOURAGED"
    DEPRECATED = "DEPRECATED"


class TerminologyStatus(StrEnum):
    """Status this engine reports for a mapping target (Master Instruction 20 / 23)."""

    CURRENT_VALID = "CURRENT_VALID"
    CURRENT_TRIAL = "CURRENT_TRIAL"
    DISCOURAGED = "DISCOURAGED"
    DEPRECATED = "DEPRECATED"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"


class Decision(StrEnum):
    """The only decisions the engine may emit (Master Instruction 25)."""

    KEEP = "KEEP"
    KEEP_WITH_WARNING = "KEEP_WITH_WARNING"
    SUGGEST_REPLACEMENT = "SUGGEST_REPLACEMENT"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNKNOWN_CODE = "UNKNOWN_CODE"


class Reason(StrEnum):
    """Machine-readable justification attached to a decision."""

    STATUS_ACTIVE = "STATUS_ACTIVE"
    STATUS_TRIAL = "STATUS_TRIAL"
    SINGLE_OFFICIAL_REPLACEMENT = "SINGLE_OFFICIAL_REPLACEMENT"
    MULTIPLE_REPLACEMENTS = "MULTIPLE_REPLACEMENTS"
    NO_OFFICIAL_REPLACEMENT = "NO_OFFICIAL_REPLACEMENT"
    NO_HISTORICAL_ASSOCIATION = "NO_HISTORICAL_ASSOCIATION"
    AMBIGUOUS_ASSOCIATION_TYPE = "AMBIGUOUS_ASSOCIATION_TYPE"
    REPLACEMENT_TARGET_NOT_CURRENT = "REPLACEMENT_TARGET_NOT_CURRENT"
    REPLACEMENT_CHAIN_CYCLE = "REPLACEMENT_CHAIN_CYCLE"
    REPLACEMENT_CHAIN_TOO_DEEP = "REPLACEMENT_CHAIN_TOO_DEEP"
    CODE_NOT_IN_CURRENT_RELEASE = "CODE_NOT_IN_CURRENT_RELEASE"
    NO_CURRENT_RELEASE = "NO_CURRENT_RELEASE"
    MOVED_TO_OTHER_NAMESPACE = "MOVED_TO_OTHER_NAMESPACE"


class ReviewStatus(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ImportStatus(StrEnum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AuditRunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MapCorrelation(StrEnum):
    """Map correlation recorded per local mapping.

    Recommended by the SNOMED CT mapping guideline literature (Sung et al.,
    JMIR Med Inform 2023, step 7 "classify mapping correlations") so that a
    later audit can tell a genuinely equivalent map from a deliberately
    broader one.
    """

    EXACT_MATCH = "EXACT_MATCH"
    BROAD_TO_NARROW = "BROAD_TO_NARROW"
    NARROW_TO_BROAD = "NARROW_TO_BROAD"
    PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
    NOT_SPECIFIED = "NOT_SPECIFIED"


# SNOMED CT historical association reference sets (Master Instruction 10).
# These concept ids belong to the SNOMED CT metadata hierarchy and are stable
# across International Edition releases.
HISTORICAL_ASSOCIATION_REFSETS: dict[str, str] = {
    "900000000000523009": "POSSIBLY_EQUIVALENT_TO",
    "900000000000524003": "MOVED_TO",
    "900000000000525002": "MOVED_FROM",
    "900000000000526001": "REPLACED_BY",
    "900000000000527005": "SAME_AS",
    "900000000000528000": "WAS_A",
    "900000000000530003": "ALTERNATIVE",
    "900000000000531004": "REFERS_TO",
    "900000000000529008": "SIMILAR_TO",
    "1186924009": "PARTIALLY_EQUIVALENT_TO",
}

# Concept Inactivation Indicator Reference Set (Master Instruction 11).
CONCEPT_INACTIVATION_REFSET = "900000000000489007"

# Human readable inactivation reason values, keyed by SNOMED value id.
INACTIVATION_VALUES: dict[str, str] = {
    "900000000000482003": "DUPLICATE",
    "900000000000483008": "OUTDATED",
    "900000000000484002": "AMBIGUOUS",
    "900000000000485001": "ERRONEOUS",
    "900000000000486000": "LIMITED",
    "900000000000487009": "MOVED_ELSEWHERE",
    "900000000000492006": "PENDING_MOVE",
    "723277005": "NON_CONFORMANCE_TO_EDITORIAL_POLICY",
    "723278000": "NOT_SEMANTICALLY_EQUIVALENT",
    "1186917008": "MEANING_OF_CONCEPT_UNKNOWN",
    "1186919006": "CLASSIFICATION_DERIVED_COMPONENT",
    "1215220019": "GRAMMATICAL_DESCRIPTION_ERROR",
}

# Association types that MAY be auto-suggested when exactly one active target
# exists (Master Instruction 23).  Everything else goes to MANUAL_REVIEW.
SAFE_ASSOCIATION_TYPES: frozenset[str] = frozenset({"REPLACED_BY", "SAME_AS"})


# ---------------------------------------------------------------------------
# SNOMED CT description metadata, for parsing preferred terms offline.
#
# Verified against the SNOMED CT Release File Specification:
#   description typeId is a child of 900000000000446008 |Description type|
#   acceptabilityId is |Preferred| or |Acceptable|
#   the International Edition ships US and GB English language reference sets
# ---------------------------------------------------------------------------
DESCRIPTION_TYPE_FSN = "900000000000003001"        # Fully specified name
DESCRIPTION_TYPE_SYNONYM = "900000000000013009"    # Synonym
DESCRIPTION_TYPE_DEFINITION = "900000000000550004"  # Textual definition

ACCEPTABILITY_PREFERRED = "900000000000548007"
ACCEPTABILITY_ACCEPTABLE = "900000000000549004"

LANGUAGE_REFSET_US_ENGLISH = "900000000000509007"
LANGUAGE_REFSET_GB_ENGLISH = "900000000000508004"

# Preference order when a concept has a preferred synonym in more than one
# dialect. Mirrors Snowstorm's own default Accept-Language ordering
# (en-X-900000000000509007,en-X-900000000000508004,en).
DEFAULT_LANGUAGE_REFSETS: tuple[str, ...] = (
    LANGUAGE_REFSET_US_ENGLISH,
    LANGUAGE_REFSET_GB_ENGLISH,
)
