"""SNOMED CT resolution rules -- one assertion per branch (Master Instruction 34)."""

from __future__ import annotations

import pytest

from backend.app.constants import Decision, Reason, TerminologyStatus
from backend.app.services.snomed_resolver import SnomedResolver
from tests.fixtures import synthetic as fx


@pytest.fixture()
def resolver(snomed_session) -> SnomedResolver:
    return SnomedResolver(snomed_session)


def test_active_concept_is_kept(resolver):
    result = resolver.resolve(fx.S_ACTIVE)
    assert result.active is True
    assert result.status is TerminologyStatus.CURRENT_VALID
    assert result.decision is Decision.KEEP
    assert result.suggested_targets == []


def test_inactive_with_single_replaced_by_suggests_replacement(resolver):
    result = resolver.resolve(fx.S_REPLACED)
    assert result.active is False
    assert result.status is TerminologyStatus.INACTIVE
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    assert result.reason is Reason.SINGLE_OFFICIAL_REPLACEMENT
    assert [t.concept_id for t in result.suggested_targets] == [fx.S_ACTIVE]
    assert result.suggested_targets[0].association_type == "REPLACED_BY"


def test_inactive_with_single_same_as_suggests_replacement(resolver):
    result = resolver.resolve(fx.S_SAME_AS)
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    assert result.suggested_targets[0].concept_id == fx.S_ACTIVE
    assert result.suggested_targets[0].association_type == "SAME_AS"


def test_possibly_equivalent_to_always_needs_review(resolver):
    """Even a single row: POSSIBLY EQUIVALENT TO asserts no equivalence."""
    result = resolver.resolve(fx.S_POSSIBLY)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.AMBIGUOUS_ASSOCIATION_TYPE
    assert len(result.associations) == 1
    assert result.suggested_targets[0].usable is False


def test_was_a_always_needs_review(resolver):
    result = resolver.resolve(fx.S_WAS_A)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.AMBIGUOUS_ASSOCIATION_TYPE


def test_multiple_associations_need_review(resolver):
    result = resolver.resolve(fx.S_MULTI)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.MULTIPLE_REPLACEMENTS
    assert len(result.associations) == 2
    assert all(t.usable is False for t in result.suggested_targets)


def test_no_association_needs_review(resolver):
    """The only association member for this concept is inactive, so it is
    correctly ignored (Master Instruction 10)."""
    result = resolver.resolve(fx.S_NO_ASSOC)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_HISTORICAL_ASSOCIATION
    assert result.associations == []


def test_moved_to_is_not_a_clinical_replacement(resolver):
    result = resolver.resolve(fx.S_MOVED)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.MOVED_TO_OTHER_NAMESPACE
    assert result.suggested_targets[0].usable is False


def test_chain_through_an_inactive_target_reaches_an_active_concept(resolver):
    result = resolver.resolve(fx.S_CHAIN_HEAD)
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    target = result.suggested_targets[0]
    assert target.concept_id == fx.S_ACTIVE
    assert target.active is True
    assert target.via == [fx.S_CHAIN_HEAD, fx.S_CHAIN_MID, fx.S_ACTIVE]


def test_cyclic_association_chain_is_detected(resolver):
    result = resolver.resolve(fx.S_CYCLE_A)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.REPLACEMENT_CHAIN_CYCLE
    assert result.suggested_targets[0].usable is False


def test_target_absent_from_the_release_is_not_suggested(resolver):
    result = resolver.resolve(fx.S_DANGLING)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.REPLACEMENT_TARGET_NOT_CURRENT
    assert result.suggested_targets[0].usable is False


def test_unknown_concept_is_flagged(resolver):
    result = resolver.resolve(fx.S_UNKNOWN)
    assert result.status is TerminologyStatus.UNKNOWN
    assert result.decision is Decision.UNKNOWN_CODE
    assert result.reason is Reason.CODE_NOT_IN_CURRENT_RELEASE


def test_inactivation_reason_is_decoded(resolver):
    assert resolver.resolve(fx.S_REPLACED).inactivation_reason == "OUTDATED"
    assert resolver.resolve(fx.S_POSSIBLY).inactivation_reason == "AMBIGUOUS"
    assert resolver.resolve(fx.S_NO_ASSOC).inactivation_reason == "ERRONEOUS"
    assert resolver.resolve(fx.S_MOVED).inactivation_reason == "MOVED_ELSEWHERE"


def test_lookup_exposes_official_metadata(resolver):
    record = resolver.lookup(fx.S_REPLACED)
    assert record["concept_id"] == fx.S_REPLACED
    assert record["active"] is False
    assert record["version"] == fx.SNOMED_NEW_VERSION
    assert record["inactivation_reason"] == "OUTDATED"
    assert record["historical_associations"][0]["association_type"] == "REPLACED_BY"
    assert resolver.lookup(fx.S_UNKNOWN) is None


def test_without_any_release_the_resolver_abstains(session):
    resolver = SnomedResolver(session)
    result = resolver.resolve(fx.S_ACTIVE)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_CURRENT_RELEASE


def test_preload_does_not_change_results(snomed_session):
    ids = [fx.S_ACTIVE, fx.S_REPLACED, fx.S_MULTI, fx.S_UNKNOWN]

    lazy = SnomedResolver(snomed_session)
    lazy_results = {c: lazy.resolve(c).decision for c in ids}

    eager = SnomedResolver(snomed_session)
    eager.preload(ids)
    eager_results = {c: eager.resolve(c).decision for c in ids}

    assert lazy_results == eager_results


def test_release_without_association_file_never_auto_suggests(
    session, tmp_root
):
    """A release that ships no association refset must degrade safely."""
    from backend.app.services.snomed_rf2_parser import ingest_snomed_release

    directory = tmp_root / "no-assoc"
    path = fx.write_snomed_release(
        directory,
        version="29990301",
        inactive=fx.INACTIVE_IN_NEW,
        with_associations=False,
    )
    report = ingest_snomed_release(
        session, file_path=path, version="29990301", make_current=True
    )
    assert report.associations == 0
    assert any("association" in w for w in report.warnings)

    resolver = SnomedResolver(session)
    result = resolver.resolve(fx.S_REPLACED)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_HISTORICAL_ASSOCIATION
