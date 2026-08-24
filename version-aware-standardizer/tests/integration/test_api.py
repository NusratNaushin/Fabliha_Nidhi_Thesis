"""FastAPI endpoint tests (Master Instruction 46).

These run against the seeded synthetic database and a TestClient.  Snowstorm is
deliberately *not* required: every endpoint either works without it or degrades
to a clearly-reported 503, which is itself part of the contract.
"""

from __future__ import annotations

from backend.app.constants import Decision
from backend.app.services import mapping_service
from tests.fixtures import synthetic as fx


def _create_mapping(client, **overrides):
    payload = {
        "source_dataset": "MANUAL_TEST",
        "local_code": "local-1",
        "local_text": "HBsAg",
        "target_system": "LOINC",
        "target_code": fx.L_DEP_ONE,
        "mapped_against_version": fx.LOINC_OLD_VERSION,
    }
    payload.update(overrides)
    return client.post("/api/v1/mappings", json=payload)


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------
def test_health_reports_every_dependency(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is True
    assert "snowstorm" in body
    assert body["snowstorm"]["available"] in (True, False)
    assert body["releases"]["LOINC"]["version"] == fx.LOINC_NEW_VERSION


def test_root_lands_on_the_console(client):
    """A person who types the bare URL wants the app, not a JSON blob."""
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/ui/"


def test_api_root_still_points_at_the_docs(client):
    body = client.get("/api").json()
    assert body["docs"] == "/docs"
    assert body["console"] == "/ui/"


def test_the_console_is_served_from_the_app_itself(client):
    """No CDN: the page and its assets must come from this process.

    A console that needs the internet would be useless on the air-gapped
    machines this kind of terminology work often runs on.
    """
    page = client.get("/ui/")
    assert page.status_code == 200
    assert "Terminology Console" in page.text
    # Nothing may be pulled from a third-party origin.
    assert "http://" not in page.text.replace("http://www.w3.org", "")
    assert "https://" not in page.text

    for asset in ("/ui/app.css", "/ui/app.js"):
        res = client.get(asset)
        assert res.status_code == 200, asset
        assert len(res.content) > 1000, asset


def test_the_console_javascript_explains_every_decision(client):
    """The UI must not invent a vocabulary the engine does not use."""
    js = client.get("/ui/app.js").text
    for decision in (
        "KEEP",
        "KEEP_WITH_WARNING",
        "SUGGEST_REPLACEMENT",
        "MANUAL_REVIEW",
        "UNKNOWN_CODE",
    ):
        assert decision in js, decision


def test_openapi_schema_builds(client):
    spec = client.get("/openapi.json").json()
    assert "/api/v1/audits" in spec["paths"]
    assert "/api/v1/mappings/{mapping_id}/approve-replacement" in spec["paths"]


# ---------------------------------------------------------------------------
# releases
# ---------------------------------------------------------------------------
def test_list_releases_includes_superseded_ones(client):
    releases = client.get("/api/v1/releases").json()
    versions = {(r["system"], r["version"]): r["is_current"] for r in releases}
    assert versions[("LOINC", fx.LOINC_OLD_VERSION)] is False
    assert versions[("LOINC", fx.LOINC_NEW_VERSION)] is True
    assert versions[("SNOMED_CT", fx.SNOMED_NEW_VERSION)] is True


def test_list_releases_filter_by_system(client):
    releases = client.get("/api/v1/releases", params={"system": "SNOMED"}).json()
    assert releases
    assert {r["system"] for r in releases} == {"SNOMED_CT"}


def test_current_releases(client):
    body = client.get("/api/v1/releases/current").json()
    assert body["LOINC"]["version"] == fx.LOINC_NEW_VERSION
    assert body["SNOMED_CT"]["version"] == fx.SNOMED_NEW_VERSION
    assert len(body["LOINC"]["sha256"]) == 64


# ---------------------------------------------------------------------------
# LOINC
# ---------------------------------------------------------------------------
def test_get_loinc_returns_official_fields(client):
    body = client.get(f"/api/v1/loinc/{fx.L_DISC_ONE}").json()
    assert body["code"] == fx.L_DISC_ONE
    assert body["status"] == "DISCOURAGED"
    assert body["map_to"][0]["target"] == fx.L_ACTIVE


def test_get_unknown_loinc_is_404(client):
    assert client.get(f"/api/v1/loinc/{fx.L_UNKNOWN}").status_code == 404


def test_resolve_loinc_active(client):
    body = client.get(f"/api/v1/loinc/{fx.L_ACTIVE}/resolve").json()
    assert body["decision"] == Decision.KEEP.value
    assert body["version"] == fx.LOINC_NEW_VERSION


def test_resolve_loinc_deprecated_suggests_one_target(client):
    body = client.get(f"/api/v1/loinc/{fx.L_DEP_ONE}/resolve").json()
    assert body["status"] == "DEPRECATED"
    assert body["decision"] == Decision.SUGGEST_REPLACEMENT.value
    assert [t["code"] for t in body["suggested_targets"]] == [fx.L_ACTIVE]


def test_resolve_loinc_ambiguous_abstains(client):
    body = client.get(f"/api/v1/loinc/{fx.L_DISC_MANY}/resolve").json()
    assert body["decision"] == Decision.MANUAL_REVIEW.value
    assert body["reason"] == "MULTIPLE_REPLACEMENTS"


def test_resolve_loinc_reports_metadata_drift(client):
    body = client.get(
        f"/api/v1/loinc/{fx.L_META}/resolve",
        params={"mapped_against_version": fx.LOINC_OLD_VERSION},
    ).json()
    assert body["decision"] == Decision.KEEP.value
    assert body["metadata_changed"] is True
    assert "component" in body["metadata_diff"]


def test_resolve_unknown_loinc_is_a_verdict_not_a_404(client):
    body = client.get(f"/api/v1/loinc/{fx.L_UNKNOWN}/resolve").json()
    assert body["decision"] == Decision.UNKNOWN_CODE.value


# ---------------------------------------------------------------------------
# SNOMED
# ---------------------------------------------------------------------------
def test_get_snomed_concept(client):
    body = client.get(f"/api/v1/snomed/{fx.S_REPLACED}").json()
    assert body["active"] is False
    assert body["inactivation_reason"] == "OUTDATED"
    assert body["historical_associations"][0]["association_type"] == "REPLACED_BY"


def test_get_unknown_snomed_is_404(client):
    assert client.get(f"/api/v1/snomed/{fx.S_UNKNOWN}").status_code == 404


def test_resolve_snomed_replaced_by(client):
    body = client.get(f"/api/v1/snomed/{fx.S_REPLACED}/resolve").json()
    assert body["decision"] == Decision.SUGGEST_REPLACEMENT.value
    assert body["suggested_targets"][0]["concept_id"] == fx.S_ACTIVE


def test_resolve_snomed_possibly_equivalent_abstains(client):
    body = client.get(f"/api/v1/snomed/{fx.S_POSSIBLY}/resolve").json()
    assert body["decision"] == Decision.MANUAL_REVIEW.value
    assert body["reason"] == "AMBIGUOUS_ASSOCIATION_TYPE"


def test_snomed_search_without_snowstorm_returns_503(client):
    """Master Instruction 7: the backend must fail clearly, not silently."""
    response = client.get("/api/v1/snomed/search", params={"term": "staph"})
    assert response.status_code in (200, 503)
    if response.status_code == 503:
        assert "Snowstorm" in response.json()["detail"]
    else:
        assert response.json()["active_only"] is True


# ---------------------------------------------------------------------------
# mappings
# ---------------------------------------------------------------------------
def test_create_and_read_a_mapping(client):
    created = _create_mapping(client)
    assert created.status_code == 201
    mapping_id = created.json()["id"]

    body = client.get(f"/api/v1/mappings/{mapping_id}").json()
    assert body["target_code"] == fx.L_DEP_ONE
    assert body["mapped_against_version"] == fx.LOINC_OLD_VERSION
    assert body["revisions"] == []


def test_duplicate_mapping_is_409(client):
    _create_mapping(client)
    assert _create_mapping(client).status_code == 409


def test_unsupported_target_system_is_422(client):
    assert _create_mapping(client, target_system="ICD10").status_code == 422


def test_list_mappings_supports_filters(client):
    _create_mapping(client)
    _create_mapping(
        client, local_code="local-2", target_system="SNOMED_CT", target_code=fx.S_ACTIVE
    )
    all_rows = client.get("/api/v1/mappings").json()
    assert len(all_rows) == 2
    loinc_only = client.get(
        "/api/v1/mappings", params={"target_system": "LOINC"}
    ).json()
    assert len(loinc_only) == 1


def test_missing_mapping_is_404(client):
    assert client.get("/api/v1/mappings/9999").status_code == 404


# ---------------------------------------------------------------------------
# audits and approval
# ---------------------------------------------------------------------------
def test_audit_run_and_results(client):
    _create_mapping(client)
    run = client.post("/api/v1/audits", json={"export_csv": False}).json()
    assert run["status"] == "COMPLETED"
    assert run["loinc_version"] == fx.LOINC_NEW_VERSION
    assert run["summary_json"]["total_mappings"] == 1

    results = client.get(f"/api/v1/audits/{run['id']}/results").json()
    assert results[0]["decision"] == Decision.SUGGEST_REPLACEMENT.value

    report = client.get(f"/api/v1/audits/{run['id']}/report")
    assert "Terminology Audit Report" in report.text


def test_audit_results_filter_by_decision(client):
    _create_mapping(client)
    _create_mapping(client, local_code="local-amb", target_code=fx.L_DISC_MANY)
    run = client.post("/api/v1/audits", json={"export_csv": False}).json()
    manual = client.get(
        f"/api/v1/audits/{run['id']}/results",
        params={"decision": Decision.MANUAL_REVIEW.value},
    ).json()
    assert len(manual) == 1
    assert manual[0]["old_code"] == fx.L_DISC_MANY


def test_missing_audit_run_is_404(client):
    assert client.get("/api/v1/audits/9999").status_code == 404
    assert client.get("/api/v1/audits/9999/results").status_code == 404
    assert client.get("/api/v1/audits/9999/report").status_code == 404


def test_approve_replacement_end_to_end(client):
    mapping_id = _create_mapping(client).json()["id"]
    run = client.post("/api/v1/audits", json={"export_csv": False}).json()
    result = client.get(f"/api/v1/audits/{run['id']}/results").json()[0]

    response = client.post(
        f"/api/v1/mappings/{mapping_id}/approve-replacement",
        json={
            "target_code": fx.L_ACTIVE,
            "reviewer": "dr-reviewer",
            "reason": "official MapTo, reviewed",
            "audit_result_id": result["id"],
        },
    )
    assert response.status_code == 200
    revision = response.json()
    assert revision["old_target_code"] == fx.L_DEP_ONE
    assert revision["new_target_code"] == fx.L_ACTIVE
    assert revision["old_target_version"] == fx.LOINC_OLD_VERSION
    assert revision["new_target_version"] == fx.LOINC_NEW_VERSION
    assert revision["approved_by"] == "dr-reviewer"

    detail = client.get(f"/api/v1/mappings/{mapping_id}").json()
    assert detail["target_code"] == fx.L_ACTIVE
    assert len(detail["revisions"]) == 1

    history = client.get(f"/api/v1/mappings/{mapping_id}/history").json()
    assert history[0]["old_target_code"] == fx.L_DEP_ONE


def test_approving_an_unsuggested_code_is_409(client):
    mapping_id = _create_mapping(client).json()["id"]
    client.post("/api/v1/audits", json={"export_csv": False})
    response = client.post(
        f"/api/v1/mappings/{mapping_id}/approve-replacement",
        json={"target_code": fx.L_TRIAL, "reviewer": "dr-reviewer"},
    )
    assert response.status_code == 409
    assert "never suggested" in response.json()["detail"]


def test_approving_on_a_missing_mapping_is_404(client):
    response = client.post(
        "/api/v1/mappings/9999/approve-replacement",
        json={"target_code": fx.L_ACTIVE, "reviewer": "dr-reviewer"},
    )
    assert response.status_code == 404


def test_history_of_a_missing_mapping_is_404(client):
    assert client.get("/api/v1/mappings/9999/history").status_code == 404


def test_audit_does_not_change_the_mapping(client, full_session):
    mapping_id = _create_mapping(client).json()["id"]
    client.post("/api/v1/audits", json={"export_csv": False})
    mapping = mapping_service.get_mapping(full_session, mapping_id)
    full_session.refresh(mapping)
    assert mapping.target_code == fx.L_DEP_ONE


# ---------------------------------------------------------------------------
# GET /api/v1/releases/diff
# ---------------------------------------------------------------------------
def test_diff_reproduces_the_official_change_snapshot(client):
    """The endpoint's whole point is the validation block, not the counts."""
    res = client.get(
        f"/api/v1/releases/diff?system=LOINC"
        f"&old={fx.LOINC_OLD_VERSION}&new={fx.LOINC_NEW_VERSION}"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["system"] == "LOINC"
    assert body["old_version"] == fx.LOINC_OLD_VERSION
    assert body["new_version"] == fx.LOINC_NEW_VERSION
    assert body["removed_codes"] == 0, "LOINC never deletes a code"
    assert body["new_codes"] >= 1

    v = body["validation"]
    assert v["official_changes"] > 0, "a vacuous comparison proves nothing"
    assert v["missed_changes"] == 0
    assert v["unexpected_changes"] == 0


def test_diff_accepts_the_spellings_people_type(client):
    res = client.get(
        f"/api/v1/releases/diff?system=snomed"
        f"&old={fx.SNOMED_OLD_VERSION}&new={fx.SNOMED_NEW_VERSION}"
    )
    assert res.status_code == 200
    assert res.json()["system"] == "SNOMED_CT"


def test_diff_refuses_an_unknown_terminology(client):
    res = client.get("/api/v1/releases/diff?system=ICD10&old=1&new=2")
    assert res.status_code == 422
    assert "ICD10" in res.json()["detail"]


def test_diff_refuses_comparing_a_release_with_itself(client):
    v = fx.LOINC_NEW_VERSION
    res = client.get(f"/api/v1/releases/diff?system=LOINC&old={v}&new={v}")
    assert res.status_code == 422
    assert "different" in res.json()["detail"]


def test_diff_names_what_is_available_when_a_release_is_missing(client):
    res = client.get(
        f"/api/v1/releases/diff?system=LOINC&old=0.01&new={fx.LOINC_NEW_VERSION}"
    )
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert "0.01" in detail
    assert fx.LOINC_NEW_VERSION in detail, "tell the caller what they could have asked for"


def test_diff_is_cached_but_stays_correct(client):
    """Releases are immutable, so the same pair may be served from cache."""
    url = (
        f"/api/v1/releases/diff?system=LOINC"
        f"&old={fx.LOINC_OLD_VERSION}&new={fx.LOINC_NEW_VERSION}"
    )
    first = client.get(url).json()
    second = client.get(url).json()
    assert first == second
