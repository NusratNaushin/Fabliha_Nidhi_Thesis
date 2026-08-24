"""Snowstorm client behaviour, exercised against a mock transport.

No server is needed: httpx's MockTransport lets us assert the exact requests the
client makes.  The point worth pinning down is that search *always* carries
``activeFilter=true`` and ``termActive=true`` -- an inactive concept must never
be offered as a new mapping candidate (Master Instruction 12 and 18).
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.services.snowstorm_client import (
    LOINC_SYSTEM_URI,
    SNOMED_SYSTEM_URI,
    SnowstormClient,
    SnowstormError,
    SnowstormUnavailable,
)


def make_client(handler, **kwargs) -> SnowstormClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        transport=transport,
        base_url="http://snowstorm.test",
        headers={"Accept": "application/json"},
    )
    return SnowstormClient(base_url="http://snowstorm.test", client=http, **kwargs)


def test_health_reports_the_server_version():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/version"
        return httpx.Response(200, json={"version": "10.4.2"})

    with make_client(handler) as client:
        health = client.health()
    assert health.available is True
    assert health.version == "10.4.2"
    assert health.as_dict()["base_url"] == "http://snowstorm.test"


def test_health_never_raises_when_the_server_is_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler) as client:
        health = client.health()
    assert health.available is False
    assert "could not be reached" in health.detail
    # The branch we would have queried is still reported, for diagnosis.
    assert health.branch == "MAIN"


def test_require_available_raises_with_the_start_command():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler) as client:
        with pytest.raises(SnowstormUnavailable) as excinfo:
            client.require_available()
    assert "docker compose up -d" in str(excinfo.value)


def test_search_always_filters_to_active_content():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"items": [{"conceptId": "1", "active": True}]})

    with make_client(handler) as client:
        items = client.search_concepts("staphylococcus", limit=5)

    assert seen["activeFilter"] == "true"
    assert seen["termActive"] == "true"
    assert seen["term"] == "staphylococcus"
    assert seen["limit"] == "5"
    assert items[0]["conceptId"] == "1"


def test_search_can_be_widened_deliberately():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"items": []})

    with make_client(handler) as client:
        assert client.search_concepts("x", active_only=False, ecl="<<64572001") == []
    assert "activeFilter" not in seen
    assert seen["ecl"] == "<<64572001"


def test_concept_lookup_uses_the_branch_and_maps_404_to_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/MAIN/concepts/12345":
            return httpx.Response(
                200,
                json={
                    "conceptId": "12345",
                    "active": True,
                    "pt": {"term": "Preferred term"},
                    "fsn": {"term": "Fully specified name (finding)"},
                },
            )
        return httpx.Response(404)

    with make_client(handler) as client:
        concept = client.get_concept("12345")
        assert concept["conceptId"] == "12345"
        assert client.get_concept("99999") is None
        assert client.preferred_term("12345") == "Preferred term"
        assert client.preferred_term("99999") is None


def test_preferred_term_falls_back_to_the_fsn():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"conceptId": "1", "fsn": {"term": "Only an FSN (finding)"}}
        )

    with make_client(handler) as client:
        assert client.preferred_term("1") == "Only an FSN (finding)"


def test_server_errors_are_surfaced_with_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="branch MAIN is locked")

    with make_client(handler) as client:
        with pytest.raises(SnowstormError) as excinfo:
            client.get_concept("12345")
    assert "HTTP 500" in str(excinfo.value)
    assert "branch MAIN is locked" in str(excinfo.value)


def test_fhir_lookup_passes_system_and_code():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"parameter": [{"name": "display"}]})

    with make_client(handler) as client:
        payload = client.lookup_loinc("55797-5")
    assert seen["system"] == LOINC_SYSTEM_URI
    assert seen["code"] == "55797-5"
    assert payload["parameter"][0]["name"] == "display"


def test_historical_translation_targets_the_requested_refset():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"parameter": []})

    with make_client(handler) as client:
        client.translate_historical("212002", refset_id="900000000000527005")
    assert seen["url"] == f"{SNOMED_SYSTEM_URI}?fhir_cm=900000000000527005"
    assert seen["system"] == SNOMED_SYSTEM_URI
    assert seen["code"] == "212002"


def test_an_unknown_refset_is_rejected_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    with make_client(handler) as client:
        with pytest.raises(ValueError, match="not a known historical association"):
            client.translate_historical("212002", refset_id="123")


def test_create_import_reads_the_id_from_the_location_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/imports"
        return httpx.Response(
            201,
            headers={"Location": "http://snowstorm.test/imports/abc-123"},
        )

    with make_client(handler) as client:
        assert client.create_import() == "abc-123"


def test_create_import_falls_back_to_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "body-456"})

    with make_client(handler) as client:
        assert client.create_import() == "body-456"


def test_create_import_without_an_id_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    with make_client(handler) as client:
        with pytest.raises(SnowstormError, match="no import id"):
            client.create_import()


def test_rejected_import_creation_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad branch")

    with make_client(handler) as client:
        with pytest.raises(SnowstormError, match="HTTP 400"):
            client.create_import()


def test_archive_upload_and_status_polling(tmp_path):
    archive = tmp_path / "rf2.zip"
    archive.write_bytes(b"PK\x03\x04 not really a zip")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(200)
        return httpx.Response(200, json={"status": "COMPLETED"})

    with make_client(handler) as client:
        client.upload_import_archive("abc-123", str(archive))
        status = client.get_import_status("abc-123")

    assert calls == ["POST /imports/abc-123/archive", "GET /imports/abc-123"]
    assert status["status"] == "COMPLETED"


def test_a_rejected_upload_is_surfaced(tmp_path):
    archive = tmp_path / "rf2.zip"
    archive.write_bytes(b"nope")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="not an RF2 archive")

    with make_client(handler) as client:
        with pytest.raises(SnowstormError, match="rejected the RF2 archive"):
            client.upload_import_archive("abc-123", str(archive))


def test_a_lazily_created_client_is_closed_cleanly():
    client = SnowstormClient(base_url="http://snowstorm.test")
    assert client.client is not None
    client.close()
    assert client._client is None
