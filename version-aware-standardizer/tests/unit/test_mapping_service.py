"""Mapping service branches not exercised by the history and safety suites."""

from __future__ import annotations

import pytest

from backend.app.constants import TerminologySystem
from backend.app.services import audit_service, mapping_service
from tests.fixtures import synthetic as fx


def _make(session, **overrides):
    payload = {
        "source_dataset": "MANUAL_TEST",
        "local_code": "L-1",
        "local_text": "a local test name",
        "target_system": "LOINC",
        "target_code": fx.L_ACTIVE,
    }
    payload.update(overrides)
    return mapping_service.create_mapping(session, **payload)


def test_missing_local_code_is_refused(full_session):
    with pytest.raises(mapping_service.MappingError, match="local_code is required"):
        _make(full_session, local_code="  ")


def test_missing_target_code_is_refused(full_session):
    with pytest.raises(mapping_service.MappingError, match="target_code is required"):
        _make(full_session, target_code="")


def test_target_system_aliases_are_normalised(full_session):
    mapping = _make(
        full_session,
        local_code="L-sct",
        target_system="snomed",
        target_code=fx.S_ACTIVE,
    )
    assert mapping.target_system == TerminologySystem.SNOMED_CT.value


def test_unknown_target_system_is_a_value_error(full_session):
    with pytest.raises(ValueError, match="Unsupported target system"):
        _make(full_session, target_system="ICD10")


def test_get_mapping_raises_for_a_missing_id(full_session):
    with pytest.raises(mapping_service.MappingNotFoundError):
        mapping_service.get_mapping(full_session, 424242)


def test_count_mappings_respects_filters(full_session):
    _make(full_session, local_code="L-a")
    _make(full_session, local_code="L-b", source_dataset="OTHER_DATASET")
    _make(
        full_session,
        local_code="L-c",
        target_system="SNOMED_CT",
        target_code=fx.S_ACTIVE,
    )
    full_session.commit()

    assert mapping_service.count_mappings(full_session) == 3
    assert (
        mapping_service.count_mappings(full_session, source_dataset="OTHER_DATASET") == 1
    )
    assert mapping_service.count_mappings(full_session, target_system="SNOMED") == 1


def test_list_mappings_paginates(full_session):
    for i in range(5):
        _make(full_session, local_code=f"page-{i}")
    full_session.commit()

    first = mapping_service.list_mappings(full_session, limit=2, offset=0)
    second = mapping_service.list_mappings(full_session, limit=2, offset=2)
    assert len(first) == len(second) == 2
    assert {m.id for m in first}.isdisjoint({m.id for m in second})


def test_current_release_versions_reflects_the_registry(full_session):
    versions = mapping_service.current_release_versions(full_session)
    assert versions[TerminologySystem.LOINC.value] == fx.LOINC_NEW_VERSION
    assert versions[TerminologySystem.SNOMED_CT.value] == fx.SNOMED_NEW_VERSION


def test_snomed_replacement_can_be_approved_after_an_audit(full_session):
    mapping = _make(
        full_session,
        local_code="sct-dep",
        target_system="SNOMED_CT",
        target_code=fx.S_REPLACED,
        mapped_against_version=fx.SNOMED_OLD_VERSION,
    )
    full_session.commit()

    run = audit_service.run_audit(full_session, export_csv=False)
    result = audit_service.list_results(full_session, run.id)[0]
    assert result.suggested_targets_json[0]["concept_id"] == fx.S_ACTIVE

    revision = mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.S_ACTIVE,
        reviewer="dr-reviewer",
        audit_result_id=result.id,
    )
    full_session.commit()

    assert revision.old_target_code == fx.S_REPLACED
    assert revision.new_target_version == fx.SNOMED_NEW_VERSION
    assert mapping.target_code == fx.S_ACTIVE


def test_approving_an_inactive_snomed_target_is_refused(full_session):
    mapping = _make(
        full_session,
        local_code="sct-amb",
        target_system="SNOMED_CT",
        target_code=fx.S_POSSIBLY,
    )
    full_session.commit()

    with pytest.raises(mapping_service.ReplacementRejected, match="INACTIVE"):
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.S_NO_ASSOC,  # itself inactive
            reviewer="dr-reviewer",
            allow_unsuggested=True,
        )


def test_approval_without_any_audit_explains_what_to_do(full_session):
    mapping = _make(full_session, local_code="never-audited", target_code=fx.L_DEP_ONE)
    full_session.commit()
    with pytest.raises(mapping_service.ReplacementRejected) as excinfo:
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.L_ACTIVE,
            reviewer="dr-reviewer",
        )
    assert "Run an audit first" in str(excinfo.value)


def test_missing_target_code_on_approval_is_refused(full_session):
    mapping = _make(full_session, local_code="blank-approval")
    full_session.commit()
    with pytest.raises(mapping_service.ReplacementRejected, match="target_code is required"):
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code="  ",
            reviewer="dr-reviewer",
        )
