"""SNOMED CT resolution rules -- Master Instruction sections 23 and 24.

SNOMED never repurposes a concept id: an id that stops being valid becomes
``active = 0`` and is connected to its successors through the historical
association reference sets.  Those refsets carry very different semantic
strength, and the whole safety of this engine rests on honouring that
difference:

* ``REPLACED BY`` and ``SAME AS`` assert a specific successor -- a single
  active target may be *suggested*;
* ``POSSIBLY EQUIVALENT TO``, ``WAS A``, ``ALTERNATIVE`` and ``MOVED TO`` do
  not assert equivalence at all, so they always abstain to human review, even
  when exactly one row exists.

Everything is computed from the locally parsed RF2 refsets, so a result is
reproducible from the release files alone -- Snowstorm is used only to enrich
the display term.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.constants import (
    HISTORICAL_ASSOCIATION_REFSETS,
    INACTIVATION_VALUES,
    SAFE_ASSOCIATION_TYPES,
    Decision,
    Reason,
    TerminologyStatus,
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
from backend.app.utils.logging import get_logger

log = get_logger("snomed.resolver")


@dataclass
class Association:
    """One active historical association row, decoded to a readable type."""

    association_type: str
    refset_id: str
    target_component_id: str
    target_active: bool | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnomedSuggestedTarget:
    concept_id: str
    active: bool | None
    display: str | None
    association_type: str | None
    usable: bool
    via: list[str] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnomedResolution:
    concept_id: str
    system: str
    version: str | None
    status: TerminologyStatus
    decision: Decision
    reason: Reason | None = None
    active: bool | None = None
    display: str | None = None
    inactivation_reason: str | None = None
    inactivation_value_id: str | None = None
    associations: list[Association] = field(default_factory=list)
    suggested_targets: list[SnomedSuggestedTarget] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "concept_id": self.concept_id,
            "system": self.system,
            "version": self.version,
            "status": self.status.value,
            "decision": self.decision.value,
            "reason": self.reason.value if self.reason else None,
            "active": self.active,
            "display": self.display,
            "inactivation_reason": self.inactivation_reason,
            "inactivation_value_id": self.inactivation_value_id,
            "historical_associations": [a.as_dict() for a in self.associations],
            "suggested_targets": [t.as_dict() for t in self.suggested_targets],
            "details": self.details,
        }


class SnomedResolver:
    """Resolves SNOMED concept ids against the current parsed RF2 release."""

    def __init__(
        self,
        session: Session,
        release: TerminologyRelease | None = None,
        max_depth: int | None = None,
    ) -> None:
        self.session = session
        self.release = release or release_service.get_current(
            session, TerminologySystem.SNOMED_CT.value
        )
        self.version = self.release.version if self.release else None
        self.max_depth = max_depth or settings.max_replacement_chain_depth
        self._concepts: dict[str, SnomedConceptVersion | None] = {}
        self._assoc: dict[str, list[Association]] = {}
        self._assoc_loaded: set[str] = set()
        self._inactivation: dict[str, SnomedInactivation | None] = {}
        self._terms: dict[str, SnomedConceptTerm | None] = {}

    # -- data access -------------------------------------------------------
    def preload(self, concept_ids: list[str]) -> None:
        """Batch-fetch concepts, associations and inactivation reasons."""
        if not self.version:
            return
        wanted = sorted({c for c in concept_ids if c and c not in self._concepts})
        for i in range(0, len(wanted), 900):
            chunk = wanted[i : i + 900]

            rows = self.session.scalars(
                select(SnomedConceptVersion).where(
                    SnomedConceptVersion.release_version == self.version,
                    SnomedConceptVersion.concept_id.in_(chunk),
                )
            )
            found = {r.concept_id: r for r in rows}
            for concept_id in chunk:
                self._concepts[concept_id] = found.get(concept_id)

            assoc_rows = self.session.scalars(
                select(SnomedHistoricalAssociation).where(
                    SnomedHistoricalAssociation.release_version == self.version,
                    SnomedHistoricalAssociation.referenced_component_id.in_(chunk),
                    SnomedHistoricalAssociation.active.is_(True),
                )
            )
            grouped: dict[str, list[Association]] = defaultdict(list)
            for row in assoc_rows:
                grouped[row.referenced_component_id].append(_to_association(row))
            for concept_id in chunk:
                self._assoc[concept_id] = _dedupe(grouped.get(concept_id, []))
                self._assoc_loaded.add(concept_id)

            inact_rows = self.session.scalars(
                select(SnomedInactivation).where(
                    SnomedInactivation.release_version == self.version,
                    SnomedInactivation.concept_id.in_(chunk),
                    SnomedInactivation.active.is_(True),
                )
            )
            inact = {r.concept_id: r for r in inact_rows}
            for concept_id in chunk:
                self._inactivation[concept_id] = inact.get(concept_id)

            term_rows = self.session.scalars(
                select(SnomedConceptTerm).where(
                    SnomedConceptTerm.release_version == self.version,
                    SnomedConceptTerm.concept_id.in_(chunk),
                )
            )
            terms = {r.concept_id: r for r in term_rows}
            for concept_id in chunk:
                self._terms[concept_id] = terms.get(concept_id)

    def get_concept(self, concept_id: str) -> SnomedConceptVersion | None:
        if not self.version:
            return None
        if concept_id not in self._concepts:
            self._concepts[concept_id] = self.session.scalar(
                select(SnomedConceptVersion).where(
                    SnomedConceptVersion.release_version == self.version,
                    SnomedConceptVersion.concept_id == concept_id,
                )
            )
        return self._concepts[concept_id]

    def get_associations(self, concept_id: str) -> list[Association]:
        """Active association refset members only (Master Instruction 10)."""
        if not self.version:
            return []
        if concept_id not in self._assoc_loaded:
            rows = self.session.scalars(
                select(SnomedHistoricalAssociation).where(
                    SnomedHistoricalAssociation.release_version == self.version,
                    SnomedHistoricalAssociation.referenced_component_id == concept_id,
                    SnomedHistoricalAssociation.active.is_(True),
                )
            )
            self._assoc[concept_id] = _dedupe([_to_association(r) for r in rows])
            self._assoc_loaded.add(concept_id)
        return self._assoc.get(concept_id, [])

    def get_inactivation(self, concept_id: str) -> SnomedInactivation | None:
        if not self.version:
            return None
        if concept_id not in self._inactivation:
            self._inactivation[concept_id] = self.session.scalar(
                select(SnomedInactivation).where(
                    SnomedInactivation.release_version == self.version,
                    SnomedInactivation.concept_id == concept_id,
                    SnomedInactivation.active.is_(True),
                )
            )
        return self._inactivation[concept_id]

    def get_term(self, concept_id: str) -> SnomedConceptTerm | None:
        if not self.version:
            return None
        if concept_id not in self._terms:
            self._terms[concept_id] = self.session.scalar(
                select(SnomedConceptTerm).where(
                    SnomedConceptTerm.release_version == self.version,
                    SnomedConceptTerm.concept_id == concept_id,
                )
            )
        return self._terms[concept_id]

    def display_for(self, concept_id: str) -> str | None:
        """Preferred term, else fully specified name, else nothing.

        Answered entirely from the parsed release, so an audit report reads in
        clinical language with Snowstorm switched off.
        """
        term = self.get_term(concept_id)
        return term.display if term else None

    # -- target verification (Master Instruction 24) ------------------------
    def chase(
        self, concept_id: str, association_type: str
    ) -> tuple[SnomedSuggestedTarget, Reason | None]:
        """Follow a successor until an active concept or a stopping condition."""
        via: list[str] = [concept_id]
        visited: set[str] = {concept_id}
        current = concept_id
        current_type = association_type

        for _ in range(self.max_depth):
            concept = self.get_concept(current)
            if concept is None:
                return (
                    SnomedSuggestedTarget(
                        concept_id=current,
                        active=None,
                        display=None,
                        association_type=current_type,
                        usable=False,
                        via=list(via),
                        note="target concept is not present in the current release",
                    ),
                    Reason.REPLACEMENT_TARGET_NOT_CURRENT,
                )
            if concept.active:
                return (
                    SnomedSuggestedTarget(
                        concept_id=current,
                        active=True,
                        display=None,
                        association_type=current_type,
                        usable=True,
                        via=list(via),
                    ),
                    None,
                )

            # The suggested target is itself inactive: keep going, but only
            # along an unambiguous, semantically strong association.
            onward = [
                a
                for a in self.get_associations(current)
                if a.association_type in SAFE_ASSOCIATION_TYPES
            ]
            if not onward:
                return (
                    SnomedSuggestedTarget(
                        concept_id=current,
                        active=False,
                        display=None,
                        association_type=current_type,
                        usable=False,
                        via=list(via),
                        note="target is inactive and has no REPLACED BY / SAME AS successor",
                    ),
                    Reason.NO_HISTORICAL_ASSOCIATION,
                )
            targets = sorted({a.target_component_id for a in onward})
            if len(targets) > 1:
                return (
                    SnomedSuggestedTarget(
                        concept_id=current,
                        active=False,
                        display=None,
                        association_type=current_type,
                        usable=False,
                        via=list(via),
                        note=f"target is inactive and forks into {len(targets)} successors",
                    ),
                    Reason.MULTIPLE_REPLACEMENTS,
                )
            nxt = targets[0]
            if nxt in visited:
                return (
                    SnomedSuggestedTarget(
                        concept_id=current,
                        active=False,
                        display=None,
                        association_type=current_type,
                        usable=False,
                        via=list(via),
                        note=f"historical association chain cycles back to {nxt}",
                    ),
                    Reason.REPLACEMENT_CHAIN_CYCLE,
                )
            current_type = next(
                a.association_type for a in onward if a.target_component_id == nxt
            )
            visited.add(nxt)
            via.append(nxt)
            current = nxt

        return (
            SnomedSuggestedTarget(
                concept_id=current,
                active=None,
                display=None,
                association_type=current_type,
                usable=False,
                via=list(via),
                note=f"chain exceeded the safety depth of {self.max_depth}",
            ),
            Reason.REPLACEMENT_CHAIN_TOO_DEEP,
        )

    # -- public API --------------------------------------------------------
    def resolve(self, concept_id: str) -> SnomedResolution:
        concept_id = (concept_id or "").strip()
        base = SnomedResolution(
            concept_id=concept_id,
            system=TerminologySystem.SNOMED_CT.value,
            version=self.version,
            status=TerminologyStatus.UNKNOWN,
            decision=Decision.UNKNOWN_CODE,
        )

        if not self.release:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.NO_CURRENT_RELEASE
            base.details["message"] = (
                "No current SNOMED CT release is imported; nothing can be validated."
            )
            return base

        concept = self.get_concept(concept_id)
        if concept is None:
            base.decision = Decision.UNKNOWN_CODE
            base.reason = Reason.CODE_NOT_IN_CURRENT_RELEASE
            base.details["message"] = (
                f"{concept_id!r} is absent from SNOMED CT {self.version}. It may "
                f"belong to an extension/namespace that was not imported."
            )
            return base

        base.active = concept.active
        base.display = self.display_for(concept_id)

        if concept.active:
            base.status = TerminologyStatus.CURRENT_VALID
            base.decision = Decision.KEEP
            base.reason = Reason.STATUS_ACTIVE
            return base

        # ---- inactive ----------------------------------------------------
        base.status = TerminologyStatus.INACTIVE
        inactivation = self.get_inactivation(concept_id)
        if inactivation is not None:
            base.inactivation_value_id = inactivation.value_id
            base.inactivation_reason = INACTIVATION_VALUES.get(
                inactivation.value_id, "UNRECOGNISED_INACTIVATION_VALUE"
            )

        associations = self.get_associations(concept_id)
        base.associations = associations
        for association in associations:
            association.target_active = self._target_active(
                association.target_component_id
            )

        if not associations:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.NO_HISTORICAL_ASSOCIATION
            base.details["message"] = (
                f"{concept_id} is inactive in {self.version} and carries no active "
                f"historical association; a human must choose a successor."
            )
            return base

        if len(associations) > 1:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.MULTIPLE_REPLACEMENTS
            base.suggested_targets = [
                SnomedSuggestedTarget(
                    concept_id=a.target_component_id,
                    active=self._target_active(a.target_component_id),
                    display=None,
                    association_type=a.association_type,
                    usable=False,
                    via=[concept_id, a.target_component_id],
                    note="one of several candidates -- listed for review only",
                )
                for a in associations
            ]
            base.details["message"] = (
                f"{concept_id} has {len(associations)} active historical "
                f"associations; automatic migration is unsafe."
            )
            self._fill_target_displays(base)
            return base

        association = associations[0]

        if association.association_type not in SAFE_ASSOCIATION_TYPES:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = (
                Reason.MOVED_TO_OTHER_NAMESPACE
                if association.association_type == "MOVED_TO"
                else Reason.AMBIGUOUS_ASSOCIATION_TYPE
            )
            base.suggested_targets = [
                SnomedSuggestedTarget(
                    concept_id=association.target_component_id,
                    active=self._target_active(association.target_component_id),
                    display=None,
                    association_type=association.association_type,
                    usable=False,
                    via=[concept_id, association.target_component_id],
                    note=(
                        "MOVED TO points at a namespace/module, not at a clinical "
                        "replacement"
                        if association.association_type == "MOVED_TO"
                        else f"{association.association_type} does not assert "
                        f"equivalence, even as the only row"
                    ),
                )
            ]
            base.details["message"] = (
                f"{concept_id} is linked by {association.association_type}, which "
                f"is not strong enough to suggest an automatic replacement."
            )
            self._fill_target_displays(base)
            return base

        # Single REPLACED BY / SAME AS -- the only auto-suggestable case.
        candidate, stop_reason = self.chase(
            association.target_component_id, association.association_type
        )
        candidate.via = [concept_id] + candidate.via
        base.suggested_targets = [candidate]

        if candidate.usable and stop_reason is None:
            base.decision = Decision.SUGGEST_REPLACEMENT
            base.reason = Reason.SINGLE_OFFICIAL_REPLACEMENT
        else:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = stop_reason or Reason.REPLACEMENT_TARGET_NOT_CURRENT
            base.details["message"] = candidate.note or (
                f"the {association.association_type} target of {concept_id} is not "
                f"an active concept in {self.version}"
            )
        self._fill_target_displays(base)
        return base

    def _target_active(self, concept_id: str) -> bool | None:
        concept = self.get_concept(concept_id)
        return None if concept is None else concept.active

    def _fill_target_displays(self, resolution: "SnomedResolution") -> None:
        for target in resolution.suggested_targets:
            if target.display is None:
                target.display = self.display_for(target.concept_id)

    # -- lookup ------------------------------------------------------------
    def lookup(self, concept_id: str) -> dict | None:
        """Raw current-release record for ``GET /api/v1/snomed/{concept_id}``."""
        concept = self.get_concept((concept_id or "").strip())
        if concept is None:
            return None
        inactivation = self.get_inactivation(concept.concept_id)
        term = self.get_term(concept.concept_id)
        return {
            "concept_id": concept.concept_id,
            "version": self.version,
            "active": concept.active,
            "fsn": term.fsn if term else None,
            "preferred_term": term.preferred_term if term else None,
            "language_refset_id": term.language_refset_id if term else None,
            "display": term.display if term else None,
            "effective_time": concept.effective_time,
            "module_id": concept.module_id,
            "definition_status_id": concept.definition_status_id,
            "inactivation_value_id": inactivation.value_id if inactivation else None,
            "inactivation_reason": (
                INACTIVATION_VALUES.get(inactivation.value_id) if inactivation else None
            ),
            "historical_associations": [
                a.as_dict() for a in self.get_associations(concept.concept_id)
            ],
        }


def _to_association(row: SnomedHistoricalAssociation) -> Association:
    return Association(
        association_type=HISTORICAL_ASSOCIATION_REFSETS.get(
            row.refset_id, "UNRECOGNISED_ASSOCIATION"
        ),
        refset_id=row.refset_id,
        target_component_id=row.target_component_id,
    )


def _dedupe(associations: list[Association]) -> list[Association]:
    """Collapse identical (type, target) rows; keep genuinely distinct ones."""
    seen: set[tuple[str, str]] = set()
    out: list[Association] = []
    for a in associations:
        key = (a.association_type, a.target_component_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return sorted(out, key=lambda a: (a.association_type, a.target_component_id))


__all__ = [
    "Association",
    "SnomedResolution",
    "SnomedResolver",
    "SnomedSuggestedTarget",
]
