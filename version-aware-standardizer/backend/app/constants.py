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


# ===========================================================================
# Result standardization
#
# The layer above terminology: once we know *which test* a row is, these say
# what kind of answer it carries and how far we got in normalising it.
# ===========================================================================


class LoincScale(StrEnum):
    """LOINC SCALE_TYP -- what shape of answer the test produces.

    This is read from the release, never guessed: it decides whether a value is
    parsed as a number, a category or free text.
    """

    QN = "Qn"            # quantitative: 120, 7.4, <2
    SEMI_QN = "SemiQn"   # buckets or titres: 1+, 1:16
    ORD = "Ord"          # ordered categories: Negative, Trace, Positive
    NOM = "Nom"          # unordered categories: E. coli, Yellow
    ORD_QN = "OrdQn"     # either: "Resistant" or 15 mm
    NAR = "Nar"          # narrative text
    MULTI = "Multi"      # several results in one blob
    DOC = "Doc"          # a document
    SET = "Set"          # a structured attachment


class ValueType(StrEnum):
    """How the standardized value is represented (mirrors FHIR Observation)."""

    QUANTITY = "QUANTITY"                   # -> valueQuantity
    CODEABLE_CONCEPT = "CODEABLE_CONCEPT"   # -> valueCodeableConcept
    STRING = "STRING"                       # -> valueString
    ABSENT = "ABSENT"                       # -> dataAbsentReason


class Comparator(StrEnum):
    """FHIR Quantity.comparator. A censored result keeps its number AND its sign.

    "<2.0" is 2.0 with comparator "<" -- never 2.0 alone, and never 0.
    """

    LESS_THAN = "<"
    LESS_OR_EQUAL = "<="
    GREATER_THAN = ">"
    GREATER_OR_EQUAL = ">="


class UnitStatus(StrEnum):
    """How far a raw unit string got towards being a UCUM code."""

    VALID = "UNIT_VALID"                # already a UCUM code
    NORMALIZED = "UNIT_NORMALIZED"      # spelling fixed; the number is untouched
    CONVERTED = "UNIT_CONVERTED"        # an approved rule changed the number too
    MISSING = "UNIT_MISSING"            # no unit given
    UNKNOWN = "UNIT_UNKNOWN"            # not a unit we have a rule for
    INCOMPATIBLE = "UNIT_INCOMPATIBLE"  # wrong dimension for this test
    REVIEW_REQUIRED = "UNIT_REVIEW_REQUIRED"


class ValueMappingStatus(StrEnum):
    """How far a categorical result got towards a coded concept."""

    CODED = "CODED"
    # Text was recognised and normalised, but no standard code was attached --
    # because SNOMED CT International is not licensed here. Inventing a code
    # would be worse than admitting the gap.
    TEXT_NORMALIZED_CODE_PENDING = "TEXT_NORMALIZED_CODE_PENDING"
    UNMAPPED = "UNMAPPED"


class QualityStatus(StrEnum):
    """The verdict for one standardized row."""

    OK = "OK"
    WARNING = "WARNING"        # usable, but something is worth knowing
    QUARANTINED = "QUARANTINED" # not fit to use; kept, never dropped


class ResultIssue(StrEnum):
    """Named problems. Every one of these is recorded, never silently swallowed."""

    UNKNOWN_ITEMID = "UNKNOWN_ITEMID"
    NO_LOINC_MAPPING = "NO_LOINC_MAPPING"
    LOINC_NOT_APPROVED = "LOINC_NOT_APPROVED"
    LOINC_UNKNOWN_CODE = "LOINC_UNKNOWN_CODE"
    LOINC_TRIAL = "LOINC_TRIAL"

    MISSING_VALUE = "MISSING_VALUE"
    NOT_A_NUMBER = "NOT_A_NUMBER"
    PARSE_ERROR = "PARSE_ERROR"
    BELOW_DETECTION_LIMIT = "BELOW_DETECTION_LIMIT"
    ABOVE_DETECTION_LIMIT = "ABOVE_DETECTION_LIMIT"
    TEXT_RESULT = "TEXT_RESULT"
    VALUE_NUMERIC_MISMATCH = "VALUE_NUMERIC_MISMATCH"
    SCALE_MISMATCH = "SCALE_MISMATCH"

    UNIT_MISSING = "UNIT_MISSING"
    UNIT_UNKNOWN = "UNIT_UNKNOWN"
    UNIT_INCOMPATIBLE = "UNIT_INCOMPATIBLE"
    UNIT_CONVERSION_NOT_AVAILABLE = "UNIT_CONVERSION_NOT_AVAILABLE"

    CATEGORICAL_UNMAPPED = "CATEGORICAL_UNMAPPED"
    CODE_PENDING_LICENCE = "CODE_PENDING_LICENCE"


class DataAbsentReason(StrEnum):
    """FHIR data-absent-reason codes, used instead of inventing a value."""

    UNKNOWN = "unknown"
    NOT_A_NUMBER = "not-a-number"
    ERROR = "error"


# FHIR observation-interpretation. MIMIC's FLAG only ever means "abnormal";
# an empty FLAG means nothing was recorded -- it does NOT mean normal.
INTERPRETATION_ABNORMAL = "A"
INTERPRETATION_ABNORMAL_DISPLAY = "Abnormal"

# Scales whose answers are categories rather than numbers.
CATEGORICAL_SCALES: frozenset[str] = frozenset(
    {LoincScale.ORD.value, LoincScale.NOM.value, LoincScale.SEMI_QN.value}
)

# Scales that legitimately carry either a number or a category, so no
# expectation can be imposed on them.
#
# SemiQn is here because of what LOINC actually does with it. It covers titres
# ("1:16") and dipstick grades ("1+"), which are categorical -- but it also
# covers pH, which is numeric: LOINC models pH as SemiQn with property LsCnc
# because it is a logarithmic quantity, and LOINC 2.83 contains no quantitative
# pH code at all. Treating SemiQn as categorical flagged every one of the 1,537
# real pH results as a mismatch, which was our error and not the data's.
AMBIGUOUS_SCALES: frozenset[str] = frozenset(
    {LoincScale.SEMI_QN.value, LoincScale.ORD_QN.value}
)
NARRATIVE_SCALES: frozenset[str] = frozenset(
    {LoincScale.NAR.value, LoincScale.MULTI.value, LoincScale.DOC.value, LoincScale.SET.value}
)
