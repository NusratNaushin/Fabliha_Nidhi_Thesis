"""Mandatory safety test (Master Instruction 42).

The single claim this file defends: **the system never changes a mapping by
itself.**  Ambiguity always abstains, and even an unambiguous suggestion is
inert until a named human approves it.

This is the property that makes the engine publishable.  The LLM comparison in
laboratory medicine found only 22.7% agreement between three LLMs and human
experts on LOINC assignment; the selective-prediction literature shows that a
system which abstains on the hard cases is both safer and more efficient than
one that always answers.  So abstention is not a limitation here -- it is the
designed behaviour, and it is tested.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.constants import Decision, Reason
from backend.app.models import LocalMapping, MappingRevision
from backend.app.services import audit_service, mapping_service
from backend.app.services.loinc_resolver import LoincResolver
from backend.app.services.snomed_resolver import SnomedResolver
from tests.fixtures import synthetic as fx

AMBIGUOUS_LOINC = [
    (fx.L_DISC_MANY, Reason.MULTIPLE_REPLACEMENTS),
    (fx.L_DEP_NONE, Reason.NO_OFFICIAL_REPLACEMENT),
    (fx.L_CYCLE_A, Reason.REPLACEMENT_CHAIN_CYCLE),
]

AMBIGUOUS_SNOMED = [
    (fx.S_POSSIBLY, Reason.AMBIGUOUS_ASSOCIATION_TYPE),
    (fx.S_WAS_A, Reason.AMBIGUOUS_ASSOCIATION_TYPE),
    (fx.S_MULTI, Reason.MULTIPLE_REPLACEMENTS),
    (fx.S_NO_ASSOC, Reason.NO_HISTORICAL_ASSOCIATION),
    (fx.S_MOVED, Reason.MOVED_TO_OTHER_NAMESPACE),
    (fx.S_CYCLE_A, Reason.REPLACEMENT_CHAIN_CYCLE),
    (fx.S_DANGLING, Reason.REPLACEMENT_TARGET_NOT_CURRENT),
]


@pytest.mark.parametrize("code,expected_reason", AMBIGUOUS_LOINC)
def test_ambiguous_loinc_always_abstains(loinc_session, code, expected_reason):
    result = LoincResolver(loinc_session).resolve(code)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is expected_reason


@pytest.mark.parametrize("concept_id,expected_reason", AMBIGUOUS_SNOMED)
def test_ambiguous_snomed_always_abstains(snomed_session, concept_id, expected_reason):
    result = SnomedResolver(snomed_session).resolve(concept_id)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is expected_reason


def test_an_audit_never_mutates_a_target_code(full_session):
    """Even for the clean single-replacement case, the audit only suggests."""
    mapping = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="T-1",
        local_text="deprecated with one official replacement",
        target_system="LOINC",
        target_code=fx.L_DEP_ONE,
        mapped_against_version=fx.LOINC_OLD_VERSION,
    )
    full_session.commit()

    run = audit_service.run_audit(full_session, export_csv=False)
    results = audit_service.list_results(full_session, run.id)
    assert len(results) == 1
    assert results[0].decision == Decision.SUGGEST_REPLACEMENT.value
    assert results[0].suggested_targets_json[0]["code"] == fx.L_ACTIVE

    full_session.refresh(mapping)
    assert mapping.target_code == fx.L_DEP_ONE  # unchanged
    assert mapping.mapped_against_version == fx.LOINC_OLD_VERSION
    assert full_session.scalars(select(MappingRevision)).all() == []


def test_approval_requires_a_named_reviewer(full_session):
    mapping = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="T-2",
        local_text="deprecated",
        target_system="LOINC",
        target_code=fx.L_DEP_ONE,
    )
    full_session.commit()
    audit_service.run_audit(full_session, export_csv=False)

    with pytest.raises(mapping_service.ReplacementRejected) as excinfo:
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.L_ACTIVE,
            reviewer="",
        )
    assert "reviewer is required" in str(excinfo.value)


def test_approval_refuses_a_code_the_engine_never_suggested(full_session):
    mapping = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="T-3",
        local_text="deprecated",
        target_system="LOINC",
        target_code=fx.L_DEP_ONE,
    )
    full_session.commit()
    audit_service.run_audit(full_session, export_csv=False)

    with pytest.raises(mapping_service.ReplacementRejected) as excinfo:
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.L_TRIAL,  # valid code, but never suggested for this mapping
            reviewer="dr-reviewer",
        )
    assert "never suggested" in str(excinfo.value)


def test_approval_refuses_a_target_that_is_not_currently_valid(full_session):
    mapping = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="T-4",
        local_text="ambiguous replacement",
        target_system="LOINC",
        target_code=fx.L_DISC_MANY,
    )
    full_session.commit()
    audit_service.run_audit(full_session, export_csv=False)

    # L_DEP_NONE is deprecated: it must never become anybody's new target.
    with pytest.raises(mapping_service.ReplacementRejected):
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.L_DEP_NONE,
            reviewer="dr-reviewer",
            allow_unsuggested=True,
        )


def test_deliberate_manual_override_is_possible_but_explicit(full_session):
    """A reviewer may go outside the suggestions -- never by accident."""
    mapping = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="T-5",
        local_text="ambiguous, human picks one",
        target_system="LOINC",
        target_code=fx.L_DISC_MANY,
    )
    full_session.commit()

    revision = mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_ACTIVE,
        reviewer="dr-reviewer",
        reason="clinically reviewed against local test context",
        allow_unsuggested=True,
    )
    full_session.commit()
    assert revision.approved is True
    assert revision.approved_by == "dr-reviewer"
    assert mapping.target_code == fx.L_ACTIVE


def test_no_mapping_is_ever_deleted_by_an_audit(full_session):
    for index, code in enumerate([fx.L_ACTIVE, fx.L_DEP_NONE, fx.L_UNKNOWN]):
        mapping_service.create_mapping(
            full_session,
            source_dataset="MANUAL_TEST",
            local_code=f"K-{index}",
            local_text=f"row {index}",
            target_system="LOINC",
            target_code=code,
        )
    full_session.commit()
    before = len(full_session.scalars(select(LocalMapping)).all())

    audit_service.run_audit(full_session, export_csv=False)
    audit_service.run_audit(full_session, export_csv=False)

    assert len(full_session.scalars(select(LocalMapping)).all()) == before
