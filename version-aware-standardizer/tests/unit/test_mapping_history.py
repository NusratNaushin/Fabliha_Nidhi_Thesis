"""History preservation (Master Instruction 43).

A mapping that moved A -> B -> C must keep both hops readable forever, together
with the terminology version, date, reason and reviewer of each.  Without that,
a published audit number cannot be reproduced a year later.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.constants import MapCorrelation, ReviewStatus
from backend.app.models import MappingRevision
from backend.app.services import mapping_service
from tests.fixtures import synthetic as fx


@pytest.fixture()
def mapping(full_session):
    m = mapping_service.create_mapping(
        full_session,
        source_dataset="MANUAL_TEST",
        local_code="HBsAg-local",
        local_text="HBsAg",
        target_system="LOINC",
        target_code=fx.L_CHAIN_HEAD,
        mapped_against_version=fx.LOINC_OLD_VERSION,
        map_correlation=MapCorrelation.EXACT_MATCH.value,
    )
    full_session.commit()
    return m


def test_two_successive_approvals_leave_two_history_rows(full_session, mapping):
    first = mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_TRIAL,
        reviewer="reviewer-one",
        reason="first hop",
        allow_unsuggested=True,
    )
    full_session.commit()

    second = mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_ACTIVE,
        reviewer="reviewer-two",
        reason="second hop",
        allow_unsuggested=True,
    )
    full_session.commit()

    revisions = mapping_service.get_revisions(full_session, mapping.id)
    assert [(r.old_target_code, r.new_target_code) for r in revisions] == [
        (fx.L_CHAIN_HEAD, fx.L_TRIAL),
        (fx.L_TRIAL, fx.L_ACTIVE),
    ]
    assert mapping.target_code == fx.L_ACTIVE

    # Nothing was overwritten: the first revision is byte-for-byte intact.
    assert first.reason == "first hop"
    assert first.approved_by == "reviewer-one"
    assert second.approved_by == "reviewer-two"


def test_history_records_the_version_each_code_was_valid_in(full_session, mapping):
    mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_ACTIVE,
        reviewer="reviewer-one",
        allow_unsuggested=True,
    )
    full_session.commit()

    revision = mapping_service.get_revisions(full_session, mapping.id)[0]
    assert revision.old_target_version == fx.LOINC_OLD_VERSION
    assert revision.new_target_version == fx.LOINC_NEW_VERSION
    assert revision.approved_at is not None
    assert revision.created_at is not None
    # And the mapping now declares which release it was validated against.
    assert mapping.mapped_against_version == fx.LOINC_NEW_VERSION
    assert mapping.review_status == ReviewStatus.APPROVED.value


def test_approving_the_same_code_twice_is_refused(full_session, mapping):
    mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_ACTIVE,
        reviewer="reviewer-one",
        allow_unsuggested=True,
    )
    full_session.commit()
    with pytest.raises(mapping_service.ReplacementRejected) as excinfo:
        mapping_service.approve_replacement(
            full_session,
            mapping_id=mapping.id,
            target_code=fx.L_ACTIVE,
            reviewer="reviewer-one",
            allow_unsuggested=True,
        )
    assert "already points at" in str(excinfo.value)


def test_map_correlation_is_preserved(full_session, mapping):
    assert mapping.map_correlation == MapCorrelation.EXACT_MATCH.value


def test_duplicate_mapping_creation_is_refused(full_session, mapping):
    with pytest.raises(mapping_service.MappingError) as excinfo:
        mapping_service.create_mapping(
            full_session,
            source_dataset="MANUAL_TEST",
            local_code="HBsAg-local",
            local_text="HBsAg again",
            target_system="LOINC",
            target_code=fx.L_ACTIVE,
        )
    assert "already exists" in str(excinfo.value)


def test_bulk_create_skips_existing_rows(full_session, mapping):
    rows = [
        {
            "source_dataset": "MANUAL_TEST",
            "local_code": "HBsAg-local",
            "local_text": "duplicate",
            "target_system": "LOINC",
            "target_code": fx.L_ACTIVE,
        },
        {
            "source_dataset": "MANUAL_TEST",
            "local_code": "new-local",
            "local_text": "fresh",
            "target_system": "LOINC",
            "target_code": fx.L_ACTIVE,
        },
    ]
    created, skipped = mapping_service.bulk_create_mappings(full_session, rows)
    full_session.commit()
    assert (created, skipped) == (1, 1)


def test_revision_rows_are_never_updated_in_place(full_session, mapping):
    mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_TRIAL,
        reviewer="reviewer-one",
        allow_unsuggested=True,
    )
    full_session.commit()
    first_id = full_session.scalars(select(MappingRevision)).all()[0].id

    mapping_service.approve_replacement(
        full_session,
        mapping_id=mapping.id,
        target_code=fx.L_ACTIVE,
        reviewer="reviewer-one",
        allow_unsuggested=True,
    )
    full_session.commit()

    all_revisions = full_session.scalars(select(MappingRevision)).all()
    assert len(all_revisions) == 2
    assert all_revisions[0].id == first_id
    assert all_revisions[0].new_target_code == fx.L_TRIAL
