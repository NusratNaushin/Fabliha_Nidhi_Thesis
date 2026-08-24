"""LOINC resolution rules -- Master Instruction sections 20, 21 and 22.

The decision table implemented here is deliberately *conservative*.  It is the
engineering expression of a finding that runs through the literature: automatic
terminology mapping is only trustworthy when the system is allowed to abstain.
Swaminathan et al. (JAMIA 2024) showed that selective prediction -- letting the
model decline and route the hard cases to a human -- beats forcing a prediction
on every row; and the LLM LOINC-mapping comparison in laboratory medicine found
that only 22.7% of test items were mapped consistently by three LLMs and human
experts, so expert validation stays mandatory.

Concretely, that means this resolver never invents a replacement and never
commits one: it reports ``SUGGEST_REPLACEMENT`` only when the official MapTo
table gives exactly one usable target, and abstains with ``MANUAL_REVIEW``
otherwise.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.constants import (
    Decision,
    LoincStatus,
    Reason,
    TerminologyStatus,
    TerminologySystem,
)
from backend.app.models import LoincConceptVersion, LoincMapTo, TerminologyRelease
from backend.app.services import release_service
from backend.app.utils.logging import get_logger

log = get_logger("loinc.resolver")

# LOINC statuses that a replacement target may still carry and remain usable.
USABLE_TARGET_STATUSES = frozenset({LoincStatus.ACTIVE.value, LoincStatus.TRIAL.value})


@dataclass
class SuggestedTarget:
    """One official replacement candidate, already validated against the
    current release (Master Instruction 21)."""

    code: str
    status: str | None
    display: str | None
    usable: bool
    via: list[str] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoincResolution:
    """The engine's verdict for one LOINC code."""

    code: str
    system: str
    version: str | None
    status: TerminologyStatus
    decision: Decision
    reason: Reason | None = None
    raw_status: str | None = None
    display: str | None = None
    suggested_targets: list[SuggestedTarget] = field(default_factory=list)
    metadata_changed: bool | None = None
    metadata_diff: dict[str, dict[str, str | None]] = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "system": self.system,
            "version": self.version,
            "status": self.status.value,
            "decision": self.decision.value,
            "reason": self.reason.value if self.reason else None,
            "raw_status": self.raw_status,
            "display": self.display,
            "suggested_targets": [t.as_dict() for t in self.suggested_targets],
            "metadata_changed": self.metadata_changed,
            "metadata_diff": self.metadata_diff,
            "details": self.details,
        }


class LoincResolver:
    """Resolves LOINC codes against the *current* imported LOINC release.

    A single instance is reused for a whole audit run: concepts and MapTo rows
    are fetched in batches and cached, which is what keeps a 10,000-mapping
    audit free of N+1 queries (Master Instruction 47).
    """

    def __init__(
        self,
        session: Session,
        release: TerminologyRelease | None = None,
        max_depth: int | None = None,
    ) -> None:
        self.session = session
        self.release = release or release_service.get_current(
            session, TerminologySystem.LOINC.value
        )
        self.version = self.release.version if self.release else None
        self.max_depth = max_depth or settings.max_replacement_chain_depth
        self._concepts: dict[str, LoincConceptVersion | None] = {}
        self._map_to: dict[str, list[LoincMapTo]] = {}
        self._map_to_loaded: set[str] = set()
        # (release_version, code) -> concept, for metadata-drift baselines.
        self._baseline: dict[tuple[str, str], LoincConceptVersion | None] = {}

    # -- data access -------------------------------------------------------
    def preload(self, codes: list[str]) -> None:
        """Batch-fetch concepts and MapTo rows for the codes about to be audited."""
        if not self.version:
            return
        wanted = sorted({c for c in codes if c and c not in self._concepts})
        for i in range(0, len(wanted), 900):
            chunk = wanted[i : i + 900]
            rows = self.session.scalars(
                select(LoincConceptVersion).where(
                    LoincConceptVersion.release_version == self.version,
                    LoincConceptVersion.loinc_num.in_(chunk),
                )
            )
            found = {r.loinc_num: r for r in rows}
            for code in chunk:
                self._concepts[code] = found.get(code)

            map_rows = self.session.scalars(
                select(LoincMapTo).where(
                    LoincMapTo.release_version == self.version,
                    LoincMapTo.source_loinc.in_(chunk),
                )
            )
            grouped: dict[str, list[LoincMapTo]] = defaultdict(list)
            for row in map_rows:
                grouped[row.source_loinc].append(row)
            for code in chunk:
                self._map_to[code] = grouped.get(code, [])
                self._map_to_loaded.add(code)

    def get_concept(self, code: str) -> LoincConceptVersion | None:
        if not self.version:
            return None
        if code not in self._concepts:
            self._concepts[code] = self.session.scalar(
                select(LoincConceptVersion).where(
                    LoincConceptVersion.release_version == self.version,
                    LoincConceptVersion.loinc_num == code,
                )
            )
        return self._concepts[code]

    def get_map_to(self, code: str) -> list[LoincMapTo]:
        if not self.version:
            return []
        if code not in self._map_to_loaded:
            rows = list(
                self.session.scalars(
                    select(LoincMapTo).where(
                        LoincMapTo.release_version == self.version,
                        LoincMapTo.source_loinc == code,
                    )
                )
            )
            self._map_to[code] = rows
            self._map_to_loaded.add(code)
        return self._map_to.get(code, [])

    def preload_baseline(self, version: str, codes: list[str]) -> None:
        """Batch-fetch an OLDER release's rows for metadata-drift comparison.

        Without this the audit issues one extra SELECT per mapping that carries
        a ``mapped_against_version`` -- the classic N+1 the performance test
        guards against.
        """
        if not version:
            return
        wanted = sorted(
            {c for c in codes if c and (version, c) not in self._baseline}
        )
        for i in range(0, len(wanted), 900):
            chunk = wanted[i : i + 900]
            rows = self.session.scalars(
                select(LoincConceptVersion).where(
                    LoincConceptVersion.release_version == version,
                    LoincConceptVersion.loinc_num.in_(chunk),
                )
            )
            found = {r.loinc_num: r for r in rows}
            for code in chunk:
                self._baseline[(version, code)] = found.get(code)

    def concept_in_release(
        self, code: str, version: str
    ) -> LoincConceptVersion | None:
        """Look a code up in *any* imported release (used for metadata diffing)."""
        key = (version, code)
        if key not in self._baseline:
            self._baseline[key] = self.session.scalar(
                select(LoincConceptVersion).where(
                    LoincConceptVersion.release_version == version,
                    LoincConceptVersion.loinc_num == code,
                )
            )
        return self._baseline[key]

    # -- replacement chain -------------------------------------------------
    def _describe_target(
        self, code: str, via: list[str], note: str | None = None
    ) -> SuggestedTarget:
        concept = self.get_concept(code)
        if concept is None:
            return SuggestedTarget(
                code=code,
                status=None,
                display=None,
                usable=False,
                via=via,
                note=note
                or "target is not present in the current LOINC release",
            )
        return SuggestedTarget(
            code=code,
            status=concept.status,
            display=concept.long_common_name or concept.short_name,
            usable=(concept.status in USABLE_TARGET_STATUSES),
            via=via,
            note=note
            or (
                "target itself is TRIAL -- usable but not yet fully published"
                if concept.status == LoincStatus.TRIAL.value
                else None
            ),
        )

    def chase(self, code: str) -> tuple[SuggestedTarget, Reason | None]:
        """Follow a MapTo chain until a usable target or a stopping condition.

        Returns the terminal candidate plus the reason it stopped being
        followable, if it is not usable.  Cycles and over-long chains are
        stopping conditions, never infinite loops (Master Instruction 21.6-7).
        """
        via: list[str] = [code]
        visited: set[str] = {code}
        current = code

        for _ in range(self.max_depth):
            candidate = self._describe_target(current, via=list(via))
            if candidate.usable:
                return candidate, None
            if candidate.status is None:
                return candidate, Reason.REPLACEMENT_TARGET_NOT_CURRENT

            # Target is itself DISCOURAGED/DEPRECATED: keep resolving.
            next_targets = sorted({m.target_loinc for m in self.get_map_to(current)})
            if not next_targets:
                candidate.note = (
                    f"target is {candidate.status} and has no further MapTo entry"
                )
                return candidate, Reason.NO_OFFICIAL_REPLACEMENT
            if len(next_targets) > 1:
                candidate.note = (
                    f"target is {candidate.status} and forks into "
                    f"{len(next_targets)} further candidates"
                )
                return candidate, Reason.MULTIPLE_REPLACEMENTS
            nxt = next_targets[0]
            if nxt in visited:
                candidate.note = f"MapTo chain cycles back to {nxt}"
                return candidate, Reason.REPLACEMENT_CHAIN_CYCLE
            visited.add(nxt)
            via.append(nxt)
            current = nxt

        candidate = self._describe_target(
            current,
            via=list(via),
            note=f"MapTo chain exceeded the safety depth of {self.max_depth}",
        )
        return candidate, Reason.REPLACEMENT_CHAIN_TOO_DEEP

    # -- public API --------------------------------------------------------
    def resolve(
        self, code: str, mapped_against_version: str | None = None
    ) -> LoincResolution:
        """Apply the LOINC decision table to one code."""
        code = (code or "").strip()
        base = LoincResolution(
            code=code,
            system=TerminologySystem.LOINC.value,
            version=self.version,
            status=TerminologyStatus.UNKNOWN,
            decision=Decision.UNKNOWN_CODE,
        )

        if not self.release:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.NO_CURRENT_RELEASE
            base.details["message"] = (
                "No current LOINC release is imported; nothing can be validated."
            )
            return base

        concept = self.get_concept(code)
        if concept is None:
            base.decision = Decision.UNKNOWN_CODE
            base.reason = Reason.CODE_NOT_IN_CURRENT_RELEASE
            base.details["message"] = (
                f"{code!r} is absent from LOINC {self.version}. LOINC never "
                f"deletes codes, so an absent code is either a typo, a local "
                f"code, or a code from a newer release than the one imported."
            )
            return base

        base.raw_status = concept.status
        base.display = concept.long_common_name or concept.short_name

        # Section 22 -- metadata drift on a code that is still valid.
        if mapped_against_version and mapped_against_version != self.version:
            previous = self.concept_in_release(code, mapped_against_version)
            if previous is not None:
                diff = _metadata_diff(previous, concept)
                base.metadata_changed = bool(diff)
                base.metadata_diff = diff
            else:
                base.details["metadata_baseline_missing"] = (
                    f"release {mapped_against_version} is not imported, "
                    f"metadata drift could not be computed"
                )

        status = (concept.status or "").strip().upper()

        if status == LoincStatus.ACTIVE.value:
            base.status = TerminologyStatus.CURRENT_VALID
            base.decision = Decision.KEEP
            base.reason = Reason.STATUS_ACTIVE
            return base

        if status == LoincStatus.TRIAL.value:
            base.status = TerminologyStatus.CURRENT_TRIAL
            base.decision = Decision.KEEP_WITH_WARNING
            base.reason = Reason.STATUS_TRIAL
            base.details["message"] = (
                "TRIAL terms may still change; keep the mapping but flag it. "
                "Never replace a TRIAL code automatically."
            )
            return base

        if status in (LoincStatus.DISCOURAGED.value, LoincStatus.DEPRECATED.value):
            base.status = (
                TerminologyStatus.DISCOURAGED
                if status == LoincStatus.DISCOURAGED.value
                else TerminologyStatus.DEPRECATED
            )
            self._apply_map_to(base, code)
            if status == LoincStatus.DEPRECATED.value:
                base.details["new_mapping_allowed"] = False
            return base

        # A status the release uses but this engine does not recognise:
        # abstain rather than guess (Hard Rule 17).
        base.status = TerminologyStatus.UNKNOWN
        base.decision = Decision.MANUAL_REVIEW
        base.reason = Reason.CODE_NOT_IN_CURRENT_RELEASE
        base.details["message"] = (
            f"{code} carries an unrecognised STATUS {concept.status!r} in "
            f"LOINC {self.version}; refusing to interpret it."
        )
        return base

    def _apply_map_to(self, base: LoincResolution, code: str) -> None:
        """Decide what to do with a DISCOURAGED / DEPRECATED code."""
        targets = sorted({m.target_loinc for m in self.get_map_to(code)})

        if not targets:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.NO_OFFICIAL_REPLACEMENT
            base.details["message"] = (
                f"{code} is {base.status.value} and the official MapTo table of "
                f"LOINC {self.version} offers no replacement."
            )
            return

        if len(targets) > 1:
            # Master Instruction 30: never auto-pick between official
            # alternatives -- the right one depends on local context.
            base.decision = Decision.MANUAL_REVIEW
            base.reason = Reason.MULTIPLE_REPLACEMENTS
            base.suggested_targets = [
                self._describe_target(t, via=[code, t]) for t in targets
            ]
            base.details["message"] = (
                f"MapTo offers {len(targets)} official replacements for {code}; "
                f"the correct one depends on local test context."
            )
            return

        candidate, stop_reason = self.chase(targets[0])
        # Present the chain as code -> ... -> candidate.
        candidate.via = [code] + candidate.via
        base.suggested_targets = [candidate]

        if candidate.usable and stop_reason is None:
            base.decision = Decision.SUGGEST_REPLACEMENT
            base.reason = Reason.SINGLE_OFFICIAL_REPLACEMENT
            if candidate.status == LoincStatus.TRIAL.value:
                base.details["warning"] = (
                    "the only official replacement is itself TRIAL"
                )
        else:
            base.decision = Decision.MANUAL_REVIEW
            base.reason = stop_reason or Reason.REPLACEMENT_TARGET_NOT_CURRENT
            base.details["message"] = (
                candidate.note
                or f"the official replacement for {code} is not currently usable"
            )

    # -- lookup ------------------------------------------------------------
    def lookup(self, code: str) -> dict | None:
        """Raw current-release record for ``GET /api/v1/loinc/{code}``."""
        concept = self.get_concept((code or "").strip())
        if concept is None:
            return None
        return {
            "code": concept.loinc_num,
            "version": self.version,
            "status": concept.status,
            "long_common_name": concept.long_common_name,
            "short_name": concept.short_name,
            "component": concept.component,
            "property": concept.property,
            "time_aspect": concept.time_aspect,
            "system": concept.system,
            "scale_type": concept.scale_type,
            "method_type": concept.method_type,
            "class_name": concept.class_name,
            "change_type": concept.change_type,
            "version_first_released": concept.version_first_released,
            "version_last_changed": concept.version_last_changed,
            "map_to": [
                {"target": m.target_loinc, "comment": m.comment}
                for m in self.get_map_to(concept.loinc_num)
            ],
        }


def _metadata_diff(
    old: LoincConceptVersion, new: LoincConceptVersion
) -> dict[str, dict[str, str | None]]:
    """Field-level differences between the same code in two releases."""
    diff: dict[str, dict[str, str | None]] = {}
    for field_name in LoincConceptVersion.COMPARED_FIELDS:
        before = getattr(old, field_name)
        after = getattr(new, field_name)
        if before != after:
            diff[field_name] = {"prior": before, "current": after}
    return diff


__all__ = ["LoincResolution", "LoincResolver", "SuggestedTarget"]
