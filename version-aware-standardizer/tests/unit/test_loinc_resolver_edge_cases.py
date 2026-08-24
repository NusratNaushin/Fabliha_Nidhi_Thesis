"""Edge cases of the LOINC resolver.

Each test builds its own tiny release so the shared fixture stays readable.
These are the paths where a careless implementation would either crash or --
much worse -- present an obsolete or invented code as a safe replacement.
"""

from __future__ import annotations

import pytest

from backend.app.constants import Decision, Reason, TerminologyStatus
from backend.app.services.loinc_ingest import ingest_loinc_release
from backend.app.services.loinc_resolver import LoincResolver
from tests.fixtures import synthetic as fx

HEAD = "20001-1"
MID = "20002-2"
TAIL = "20003-3"
ACTIVE = "20004-4"
ABSENT = "29999-9"


def _row(code: str, status: str, **kwargs) -> list[str]:
    return fx._loinc_row(code, status=status, **kwargs)


def _install(session, tmp_root, name: str, rows, map_to) -> LoincResolver:
    """Build, import and make current a one-off synthetic release."""
    path = fx.write_loinc_release(
        tmp_root / name,
        version=name,
        rows=rows,
        map_to=map_to,
        changes=[],
    )
    ingest_loinc_release(session, file_path=path, version=name, make_current=True)
    return LoincResolver(session)


def test_replacement_target_absent_from_the_release(session, tmp_root):
    resolver = _install(
        session,
        tmp_root,
        "edge-absent",
        rows=[_row(HEAD, "DEPRECATED")],
        map_to=[[HEAD, ABSENT, "points nowhere"]],
    )
    result = resolver.resolve(HEAD)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.REPLACEMENT_TARGET_NOT_CURRENT
    target = result.suggested_targets[0]
    assert target.usable is False
    assert target.status is None
    assert "not present in the current LOINC release" in target.note


def test_chain_stops_when_the_next_hop_has_no_map_to(session, tmp_root):
    resolver = _install(
        session,
        tmp_root,
        "edge-dead-end",
        rows=[_row(HEAD, "DEPRECATED"), _row(MID, "DEPRECATED")],
        map_to=[[HEAD, MID, "superseded once, then nothing"]],
    )
    result = resolver.resolve(HEAD)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.NO_OFFICIAL_REPLACEMENT
    assert "no further MapTo entry" in result.suggested_targets[0].note


def test_chain_stops_when_the_next_hop_forks(session, tmp_root):
    resolver = _install(
        session,
        tmp_root,
        "edge-fork",
        rows=[
            _row(HEAD, "DEPRECATED"),
            _row(MID, "DEPRECATED"),
            _row(TAIL, "ACTIVE"),
            _row(ACTIVE, "ACTIVE"),
        ],
        map_to=[
            [HEAD, MID, "single hop"],
            [MID, TAIL, "fork A"],
            [MID, ACTIVE, "fork B"],
        ],
    )
    result = resolver.resolve(HEAD)
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.MULTIPLE_REPLACEMENTS
    assert "forks into 2" in result.suggested_targets[0].note


def test_chain_longer_than_the_safety_depth_is_abandoned(session, tmp_root):
    codes = [f"3000{i}-{i}" for i in range(6)]
    rows = [_row(code, "DEPRECATED") for code in codes[:-1]]
    rows.append(_row(codes[-1], "ACTIVE"))
    map_to = [
        [codes[i], codes[i + 1], "one long chain"] for i in range(len(codes) - 1)
    ]
    path = fx.write_loinc_release(
        tmp_root / "edge-deep", version="edge-deep", rows=rows, map_to=map_to, changes=[]
    )
    ingest_loinc_release(
        session, file_path=path, version="edge-deep", make_current=True
    )

    shallow = LoincResolver(session, max_depth=3)
    result = shallow.resolve(codes[0])
    assert result.decision is Decision.MANUAL_REVIEW
    assert result.reason is Reason.REPLACEMENT_CHAIN_TOO_DEEP
    assert "safety depth of 3" in result.suggested_targets[0].note

    # With enough depth the same chain resolves cleanly.
    deep = LoincResolver(session, max_depth=10)
    resolved = deep.resolve(codes[0])
    assert resolved.decision is Decision.SUGGEST_REPLACEMENT
    assert resolved.suggested_targets[0].code == codes[-1]


def test_unrecognised_status_is_never_interpreted(session, tmp_root):
    """Hard Rule 17: guessing at an unknown STATUS would be inventing meaning."""
    resolver = _install(
        session,
        tmp_root,
        "edge-status",
        rows=[_row(HEAD, "RETIRED_BY_VENDOR")],
        map_to=[],
    )
    result = resolver.resolve(HEAD)
    assert result.status is TerminologyStatus.UNKNOWN
    assert result.decision is Decision.MANUAL_REVIEW
    assert "unrecognised STATUS" in result.details["message"]
    assert result.raw_status == "RETIRED_BY_VENDOR"


def test_status_comparison_is_case_and_space_insensitive(session, tmp_root):
    resolver = _install(
        session,
        tmp_root,
        "edge-case-status",
        rows=[_row(HEAD, " active ")],
        map_to=[],
    )
    assert resolver.resolve(HEAD).decision is Decision.KEEP


def test_baseline_release_not_imported_is_reported_not_guessed(loinc_session):
    resolver = LoincResolver(loinc_session)
    result = resolver.resolve(fx.L_ACTIVE, mapped_against_version="0.99")
    assert result.metadata_changed is None
    assert "not imported" in result.details["metadata_baseline_missing"]


def test_accessors_are_safe_with_no_release_imported(session):
    resolver = LoincResolver(session)
    assert resolver.version is None
    assert resolver.get_concept(fx.L_ACTIVE) is None
    assert resolver.get_map_to(fx.L_ACTIVE) == []
    assert resolver.lookup(fx.L_ACTIVE) is None
    resolver.preload([fx.L_ACTIVE])
    resolver.preload_baseline("", [fx.L_ACTIVE])


def test_whitespace_around_a_code_is_tolerated(loinc_session):
    resolver = LoincResolver(loinc_session)
    assert resolver.resolve(f"  {fx.L_ACTIVE} ").decision is Decision.KEEP


def test_empty_code_is_an_unknown_code_not_a_crash(loinc_session):
    result = LoincResolver(loinc_session).resolve("")
    assert result.decision is Decision.UNKNOWN_CODE


@pytest.mark.parametrize("chunk_size", [1, 2, 900])
def test_preload_handles_chunk_boundaries(loinc_session, monkeypatch, chunk_size):
    """The 900-item chunking must not drop codes at a boundary."""
    resolver = LoincResolver(loinc_session)
    codes = [fx.L_ACTIVE, fx.L_DISC_ONE, fx.L_DEP_ONE, fx.L_UNKNOWN]
    original = range

    def patched(*args):
        if len(args) == 3 and args[2] == 900:
            return original(args[0], args[1], chunk_size)
        return original(*args)

    monkeypatch.setattr(
        "backend.app.services.loinc_resolver.range", patched, raising=False
    )
    resolver.preload(codes)
    assert resolver.resolve(fx.L_DISC_ONE).decision is Decision.SUGGEST_REPLACEMENT
    assert resolver.resolve(fx.L_UNKNOWN).decision is Decision.UNKNOWN_CODE
