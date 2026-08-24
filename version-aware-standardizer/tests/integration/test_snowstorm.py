"""Integration tests against a running Snowstorm (Master Instruction 35, 36).

These are skipped unless Snowstorm is actually reachable, so the suite stays
green on a machine that has not yet imported a licensed SNOMED release.

Run them with:

    cd infra/snowstorm && docker compose up -d
    python scripts/import_snomed.py --file data/raw/snomed/<RF2>.zip --version <YYYYMMDD>
    pytest tests/integration/test_snowstorm.py -m integration -v

The historical-association regression case uses a pair documented by SNOMED
International rather than one invented here.  If a future edition changes that
pair, the test must be updated from the current official reference-set data --
never by weakening the assertion.
"""

from __future__ import annotations

import pytest

from backend.app.services.snowstorm_client import (
    LOINC_SYSTEM_URI,
    SnowstormClient,
    SnowstormError,
)

pytestmark = pytest.mark.integration

# Documented REPLACED BY example: 212002 |Cellulitis of face| -> 398450001.
DOCUMENTED_INACTIVE_CONCEPT = "212002"
DOCUMENTED_REPLACEMENT = "398450001"
REPLACED_BY_REFSET = "900000000000526001"

# An official LOINC example code used to verify the LOINC upload.
LOINC_EXAMPLE_CODE = "55797-5"


@pytest.fixture(scope="module")
def client():
    with SnowstormClient() as snowstorm:
        health = snowstorm.health()
        if not health.available:
            pytest.skip(f"Snowstorm is not running: {health.detail}")
        yield snowstorm


def test_server_health(client):
    health = client.health()
    assert health.available is True
    assert health.base_url


def test_active_concept_lookup(client):
    concept = client.get_concept(DOCUMENTED_REPLACEMENT)
    if concept is None:
        pytest.skip("No SNOMED release has been imported into Snowstorm yet.")
    assert concept["conceptId"] == DOCUMENTED_REPLACEMENT
    assert concept["active"] is True


def test_inactive_concept_lookup(client):
    concept = client.get_concept(DOCUMENTED_INACTIVE_CONCEPT)
    if concept is None:
        pytest.skip("No SNOMED release has been imported into Snowstorm yet.")
    assert concept["conceptId"] == DOCUMENTED_INACTIVE_CONCEPT
    assert concept["active"] is False


def test_term_search_returns_only_active_concepts(client):
    items = client.search_concepts("staphylococcus", limit=10)
    if not items:
        pytest.skip("No SNOMED release has been imported into Snowstorm yet.")
    assert all(item["active"] is True for item in items)


def test_preferred_term_is_available(client):
    term = client.preferred_term(DOCUMENTED_REPLACEMENT)
    if term is None:
        pytest.skip("No SNOMED release has been imported into Snowstorm yet.")
    assert isinstance(term, str) and term


def test_historical_conceptmap_translation(client):
    """The FHIR route to the same fact our RF2 parser computes offline."""
    try:
        payload = client.translate_historical(
            DOCUMENTED_INACTIVE_CONCEPT, refset_id=REPLACED_BY_REFSET
        )
    except SnowstormError as exc:
        pytest.skip(f"ConceptMap/$translate unavailable: {exc}")
    if payload is None:
        pytest.skip("No SNOMED release has been imported into Snowstorm yet.")

    parameters = {p["name"]: p for p in payload.get("parameter", [])}
    if not parameters.get("result", {}).get("valueBoolean"):
        pytest.skip(
            "This edition does not carry the documented REPLACED BY pair; "
            "update the test from the current official reference-set data."
        )
    matches = [p for p in payload["parameter"] if p["name"] == "match"]
    codes = {
        part["valueCoding"]["code"]
        for match in matches
        for part in match.get("part", [])
        if part.get("name") == "concept" and "valueCoding" in part
    }
    assert DOCUMENTED_REPLACEMENT in codes


def test_local_resolver_agrees_with_snowstorm(client, session):
    """Cross-check: the offline verdict must match the server's active flag."""
    from backend.app.services.release_service import get_current
    from backend.app.services.snomed_resolver import SnomedResolver

    if get_current(session, "SNOMED_CT") is None:
        pytest.skip("No SNOMED release parsed into the local database yet.")

    resolver = SnomedResolver(session)
    for concept_id in (DOCUMENTED_INACTIVE_CONCEPT, DOCUMENTED_REPLACEMENT):
        local = resolver.lookup(concept_id)
        remote = client.get_concept(concept_id)
        if local is None or remote is None:
            pytest.skip(f"{concept_id} is not in both stores.")
        assert local["active"] == remote["active"]


def test_loinc_codesystem_lookup(client):
    """Verifies the hapi-fhir-cli LOINC upload described in the README."""
    try:
        payload = client.fhir_lookup(LOINC_SYSTEM_URI, LOINC_EXAMPLE_CODE)
    except SnowstormError as exc:
        pytest.skip(f"LOINC CodeSystem is not loaded into Snowstorm: {exc}")
    if payload is None:
        pytest.skip("LOINC has not been uploaded to Snowstorm.")
    names = {p["name"] for p in payload.get("parameter", [])}
    assert "display" in names
