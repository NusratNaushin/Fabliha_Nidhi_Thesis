"""Offline preferred-term resolution.

Reports that say "112283007" are unreadable; reports that say
"Escherichia coli" are reviewable. Getting the display term from the release
files rather than from a running Snowstorm is what makes the whole system
usable on a laptop with nothing else installed -- and it is exactly the kind of
detail where a wrong constant silently produces plausible nonsense, so every
branch of the resolution is pinned here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from backend.app.constants import (
    LANGUAGE_REFSET_GB_ENGLISH,
    LANGUAGE_REFSET_US_ENGLISH,
)
from backend.app.models import SnomedConceptTerm
from backend.app.services.snomed_rf2_parser import ingest_snomed_release
from backend.app.services.snomed_resolver import SnomedResolver
from tests.fixtures import synthetic as fx


@pytest.fixture()
def resolver(snomed_session) -> SnomedResolver:
    return SnomedResolver(snomed_session)


def _term(session, concept_id: str) -> SnomedConceptTerm:
    return session.scalar(
        select(SnomedConceptTerm).where(
            SnomedConceptTerm.release_version == fx.SNOMED_NEW_VERSION,
            SnomedConceptTerm.concept_id == concept_id,
        )
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_one_row_per_concept_not_per_description(snomed_session):
    """The 1.4M-row description file collapses to one row per concept."""
    rows = snomed_session.scalar(select(func.count()).select_from(SnomedConceptTerm))
    assert rows == len(fx.ALL_CONCEPTS)
    assert len(fx.snomed_description_rows(fx.SNOMED_NEW_VERSION)) > rows


def test_fully_specified_name_is_captured(snomed_session):
    term = _term(snomed_session, fx.S_ACTIVE)
    assert term.fsn == f"Synthetic concept {fx.S_ACTIVE} (finding)"


def test_us_english_wins_over_gb_english(snomed_session):
    """Both dialects prefer a different synonym; US is listed first."""
    term = _term(snomed_session, fx.S_ACTIVE)
    assert term.preferred_term == "Synthetic organism alpha"
    assert term.language_refset_id == LANGUAGE_REFSET_US_ENGLISH


def test_gb_english_is_used_when_us_has_no_preference(snomed_session):
    term = _term(snomed_session, fx.S_ACTIVE_2)
    assert term.preferred_term == "Synthetic organism beta (GB)"
    assert term.language_refset_id == LANGUAGE_REFSET_GB_ENGLISH


def test_an_acceptable_synonym_never_becomes_the_preferred_term(snomed_session):
    for concept_id in (fx.S_ACTIVE, fx.S_REPLACED, fx.S_SAME_AS):
        term = _term(snomed_session, concept_id)
        assert "Acceptable alias" not in (term.preferred_term or "")


def test_an_inactive_refset_member_is_ignored(snomed_session):
    """S_WAS_A's only preferred row is inactive, so it has no PT."""
    term = _term(snomed_session, fx.S_WAS_A)
    assert term.preferred_term is None
    assert term.fsn is not None
    assert term.display == term.fsn


def test_an_inactive_description_is_ignored(snomed_session):
    term = _term(snomed_session, fx.S_ACTIVE)
    assert "Retired spelling" not in (term.preferred_term or "")
    assert "Retired spelling" not in (term.fsn or "")


def test_a_dialect_we_do_not_read_is_ignored(snomed_session):
    """A preferred member in an unconfigured refset must not leak through."""
    for concept_id in fx.ALL_CONCEPTS:
        term = _term(snomed_session, concept_id)
        assert "Acceptable alias" not in (term.preferred_term or "")


def test_display_falls_back_to_the_fsn(snomed_session):
    term = _term(snomed_session, fx.S_NO_ASSOC)
    assert term.preferred_term is None
    assert term.display == f"Synthetic concept {fx.S_NO_ASSOC} (finding)"


def test_descriptions_can_be_skipped(session, snomed_new_zip):
    report = ingest_snomed_release(
        session,
        file_path=snomed_new_zip,
        version=fx.SNOMED_NEW_VERSION,
        with_descriptions=False,
    )
    assert report.concept_terms == 0
    assert session.scalar(select(func.count()).select_from(SnomedConceptTerm)) == 0
    # Everything else still works; only the display term is missing.
    resolver = SnomedResolver(session)
    result = resolver.resolve(fx.S_REPLACED)
    assert result.display is None
    assert result.decision.value == "SUGGEST_REPLACEMENT"


def test_a_release_without_description_files_warns_but_imports(session, tmp_root):
    path = fx.write_snomed_release(
        tmp_root / "no-descriptions",
        version="29990501",
        inactive=fx.INACTIVE_IN_NEW,
        with_descriptions=False,
    )
    report = ingest_snomed_release(session, file_path=path, version="29990501")
    assert report.concept_terms == 0
    assert any("description Snapshot file missing" in w for w in report.warnings)
    assert report.concepts == len(fx.ALL_CONCEPTS)


def test_language_refset_priority_is_configurable(session, snomed_new_zip):
    """Ask for GB first and the GB spelling wins instead."""
    ingest_snomed_release(
        session,
        file_path=snomed_new_zip,
        version=fx.SNOMED_NEW_VERSION,
        language_refsets=(LANGUAGE_REFSET_GB_ENGLISH, LANGUAGE_REFSET_US_ENGLISH),
    )
    term = _term(session, fx.S_ACTIVE)
    assert term.preferred_term == "Synthetic organism alpha (GB spelling)"
    assert term.language_refset_id == LANGUAGE_REFSET_GB_ENGLISH


# ---------------------------------------------------------------------------
# resolver / API surface
# ---------------------------------------------------------------------------
def test_resolution_carries_the_display_term(resolver):
    result = resolver.resolve(fx.S_REPLACED)
    assert result.display == "Synthetic superseded finding"


def test_active_concept_resolution_has_a_display_term(resolver):
    assert resolver.resolve(fx.S_ACTIVE).display == "Synthetic organism alpha"


def test_suggested_targets_carry_display_terms(resolver):
    result = resolver.resolve(fx.S_REPLACED)
    assert result.suggested_targets[0].display == "Synthetic organism alpha"


def test_review_only_targets_also_carry_display_terms(resolver):
    """A reviewer needs the name most of all when asked to decide."""
    result = resolver.resolve(fx.S_MULTI)
    displays = {t.display for t in result.suggested_targets}
    assert "Synthetic organism alpha" in displays
    assert None not in displays


def test_associations_report_whether_the_target_is_active(resolver):
    result = resolver.resolve(fx.S_MULTI)
    assert all(a.target_active is True for a in result.associations)

    dangling = resolver.resolve(fx.S_DANGLING)
    assert dangling.associations[0].target_active is None


def test_lookup_exposes_fsn_and_preferred_term(resolver):
    record = resolver.lookup(fx.S_ACTIVE)
    assert record["fsn"] == f"Synthetic concept {fx.S_ACTIVE} (finding)"
    assert record["preferred_term"] == "Synthetic organism alpha"
    assert record["language_refset_id"] == LANGUAGE_REFSET_US_ENGLISH
    assert record["display"] == "Synthetic organism alpha"


def test_display_for_is_none_for_an_unknown_concept(resolver):
    assert resolver.display_for(fx.S_UNKNOWN) is None


def test_preload_batches_the_terms_too(snomed_session):
    from sqlalchemy import event

    from backend.app.database import engine

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    resolver = SnomedResolver(snomed_session)
    event.listen(engine, "before_cursor_execute", record)
    try:
        resolver.preload(list(fx.ALL_CONCEPTS))
        before = len(statements)
        for concept_id in fx.ALL_CONCEPTS:
            resolver.display_for(concept_id)
        after = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Every display term came from the preload; not one extra query.
    assert after == before


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def test_audit_csv_carries_the_display_term(full_session, tmp_root):
    import csv

    from backend.app.services import audit_service, mapping_service

    mapping_service.create_mapping(
        full_session,
        source_dataset="TERM_TEST",
        local_code="T-1",
        local_text="local organism",
        target_system="SNOMED_CT",
        target_code=fx.S_REPLACED,
    )
    full_session.commit()

    run = audit_service.run_audit(
        full_session, source_dataset="TERM_TEST", report_name="term_audit.csv"
    )
    with open(run.report_path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert "target_display" in rows[0]
    assert rows[0]["target_display"] == "Synthetic superseded finding"


def test_api_returns_the_offline_display_term(client):
    body = client.get(f"/api/v1/snomed/{fx.S_ACTIVE}").json()
    assert body["fsn"] == f"Synthetic concept {fx.S_ACTIVE} (finding)"
    assert body["preferred_term"] == "Synthetic organism alpha"
    assert body["display"] == "Synthetic organism alpha"
    assert body["language_refset_id"] == LANGUAGE_REFSET_US_ENGLISH


def test_api_resolve_returns_the_offline_display_term(client):
    body = client.get(f"/api/v1/snomed/{fx.S_REPLACED}/resolve").json()
    assert body["display"] == "Synthetic superseded finding"
    assert body["suggested_targets"][0]["display"] == "Synthetic organism alpha"
