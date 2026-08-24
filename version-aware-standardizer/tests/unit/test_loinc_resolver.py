"""LOINC resolution rules -- one assertion per branch (Master Instruction 33)."""

from __future__ import annotations

import pytest

from backend.app.constants import Decision, Reason, TerminologyStatus
from backend.app.services.loinc_resolver import LoincResolver
from tests.fixtures import synthetic as fx


@pytest.fixture()
def resolver(loinc_session) -> LoincResolver:
    return LoincResolver(loinc_session)


def test_active_code_is_kept(resolver):
    result = resolver.resolve(fx.L_ACTIVE)
    assert result.status is TerminologyStatus.CURRENT_VALID
    assert result.decision is Decision.KEEP
    assert result.reason is Reason.STATUS_ACTIVE
    assert result.suggested_targets == []


def test_trial_code_is_kept_with_warning(resolver):
    result = resolver.resolve(fx.L_TRIAL)
    assert result.status is TerminologyStatus.CURRENT_TRIAL
    assert result.decision is Decision.KEEP_WITH_WARNING
    assert result.reason is Reason.STATUS_TRIAL
    # A TRIAL term must never be silently swapped for something else.
    assert result.suggested_targets == []


def test_discouraged_with_one_map_to_suggests_replacement(resolver):
    result = resolver.resolve(fx.L_DISC_ONE)
    assert result.status is TerminologyStatus.DISCOURAGED
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    assert result.reason is Reason.SINGLE_OFFICIAL_REPLACEMENT
    assert [t.code for t in result.suggested_targets] == [fx.L_ACTIVE]
    assert result.suggested_targets[0].usable is True


def test_discouraged_with_multiple_map_to_needs_review(resolver):
    result = resolver.resolve(fx.L_DISC_MANY)
    assert result.status is TerminologyStatus.DISCOURAGED
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.MULTIPLE_REPLACEMENTS
    assert sorted(t.code for t in result.suggested_targets) == sorted(
        [fx.L_ACTIVE, fx.L_TRIAL]
    )


def test_deprecated_with_one_map_to_suggests_replacement(resolver):
    result = resolver.resolve(fx.L_DEP_ONE)
    assert result.status is TerminologyStatus.DEPRECATED
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    assert result.reason is Reason.SINGLE_OFFICIAL_REPLACEMENT
    assert [t.code for t in result.suggested_targets] == [fx.L_ACTIVE]
    # A DEPRECATED code must never be approved for a NEW mapping.
    assert result.details["new_mapping_allowed"] is False


def test_deprecated_without_map_to_needs_review(resolver):
    result = resolver.resolve(fx.L_DEP_NONE)
    assert result.status is TerminologyStatus.DEPRECATED
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_OFFICIAL_REPLACEMENT
    assert result.suggested_targets == []


def test_unknown_code_is_flagged_not_guessed(resolver):
    result = resolver.resolve(fx.L_UNKNOWN)
    assert result.status is TerminologyStatus.UNKNOWN
    assert result.decision is Decision.UNKNOWN_CODE
    assert result.reason is Reason.CODE_NOT_IN_CURRENT_RELEASE
    assert result.suggested_targets == []


def test_replacement_chain_is_followed_to_an_active_target(resolver):
    """L_CHAIN_HEAD -> L_CHAIN_MID (also deprecated) -> L_ACTIVE."""
    result = resolver.resolve(fx.L_CHAIN_HEAD)
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    target = result.suggested_targets[0]
    assert target.code == fx.L_ACTIVE
    assert target.usable is True
    assert target.via == [fx.L_CHAIN_HEAD, fx.L_CHAIN_MID, fx.L_ACTIVE]


def test_cyclic_replacement_chain_is_detected(resolver):
    result = resolver.resolve(fx.L_CYCLE_A)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.REPLACEMENT_CHAIN_CYCLE
    assert result.suggested_targets[0].usable is False


def test_replacement_target_that_is_only_trial_is_flagged(resolver):
    result = resolver.resolve(fx.L_DEP_TO_TRIAL)
    assert result.decision is Decision.SUGGEST_REPLACEMENT
    target = result.suggested_targets[0]
    assert target.code == fx.L_TRIAL
    assert target.status == "TRIAL"
    assert "TRIAL" in result.details["warning"]


def test_metadata_only_change_keeps_the_code(full_session):
    """A display/component change must not move the code (Section 22)."""
    resolver = LoincResolver(full_session)
    result = resolver.resolve(
        fx.L_META, mapped_against_version=fx.LOINC_OLD_VERSION
    )
    assert result.decision is Decision.KEEP
    assert result.status is TerminologyStatus.CURRENT_VALID
    assert result.metadata_changed is True
    assert set(result.metadata_diff) == {"component", "long_common_name"}
    assert result.metadata_diff["component"]["prior"] == "Haemoglobin"
    assert result.metadata_diff["component"]["current"] == "Hemoglobin"


def test_no_metadata_drift_reported_for_an_unchanged_code(full_session):
    resolver = LoincResolver(full_session)
    result = resolver.resolve(
        fx.L_ACTIVE, mapped_against_version=fx.LOINC_OLD_VERSION
    )
    assert result.metadata_changed is False
    assert result.metadata_diff == {}


def test_resolution_reports_the_release_it_used(resolver):
    result = resolver.resolve(fx.L_ACTIVE)
    assert result.version == fx.LOINC_NEW_VERSION


def test_without_any_release_the_resolver_abstains(session):
    resolver = LoincResolver(session)
    result = resolver.resolve(fx.L_ACTIVE)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_CURRENT_RELEASE


def test_lookup_returns_official_fields_and_map_to(resolver):
    record = resolver.lookup(fx.L_DISC_ONE)
    assert record["code"] == fx.L_DISC_ONE
    assert record["status"] == "DISCOURAGED"
    assert record["version"] == fx.LOINC_NEW_VERSION
    assert record["map_to"] == [{"target": fx.L_ACTIVE, "comment": "single official replacement"}]
    assert resolver.lookup(fx.L_UNKNOWN) is None


def test_preload_does_not_change_results(loinc_session):
    """Batch preloading is an optimisation, never a behaviour change."""
    codes = [fx.L_ACTIVE, fx.L_DISC_ONE, fx.L_DEP_NONE, fx.L_UNKNOWN]

    lazy = LoincResolver(loinc_session)
    lazy_results = {c: lazy.resolve(c).decision for c in codes}

    eager = LoincResolver(loinc_session)
    eager.preload(codes)
    eager_results = {c: eager.resolve(c).decision for c in codes}

    assert lazy_results == eager_results
