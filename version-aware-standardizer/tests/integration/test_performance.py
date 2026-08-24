"""Performance characteristics of the audit engine (Master Instruction 47).

Two things are asserted, both structural rather than wall-clock:

* the audit of 10,000 mappings issues a *bounded, small* number of SQL
  statements -- i.e. the batch preload really does eliminate the N+1 pattern;
* throughput stays in a sane range on an ordinary developer machine.

The timing bound is deliberately generous: a CI box is not a benchmark rig, and
a flaky performance test is worse than none.  The query-count bound is the
assertion that actually protects the design.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import event

from backend.app.database import engine
from backend.app.models import LocalMapping
from backend.app.services import audit_service
from tests.fixtures import synthetic as fx

MAPPING_COUNT = 10_000

# Cycle through every LOINC decision branch so the run is not artificially easy.
ROTATION = [
    fx.L_ACTIVE,
    fx.L_TRIAL,
    fx.L_DISC_ONE,
    fx.L_DISC_MANY,
    fx.L_DEP_ONE,
    fx.L_DEP_NONE,
    fx.L_CHAIN_HEAD,
    fx.L_UNKNOWN,
    fx.L_META,
]


@pytest.fixture()
def bulk_session(full_session):
    rows = [
        {
            "source_dataset": "PERF_TEST",
            "source_system": "synthetic",
            "local_code": f"perf-{i}",
            "local_text": f"synthetic local test {i}",
            "local_context_json": None,
            "target_system": "LOINC",
            "target_code": ROTATION[i % len(ROTATION)],
            "target_display": None,
            "mapped_against_version": fx.LOINC_OLD_VERSION,
            "map_correlation": "NOT_SPECIFIED",
            "review_status": "UNREVIEWED",
        }
        for i in range(MAPPING_COUNT)
    ]
    full_session.execute(LocalMapping.__table__.insert(), rows)
    full_session.commit()
    return full_session


@pytest.mark.slow
def test_audit_of_10k_mappings_avoids_n_plus_1(bulk_session):
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    started = time.perf_counter()
    try:
        run = audit_service.run_audit(
            bulk_session,
            source_dataset="PERF_TEST",
            export_csv=False,
            mark_review_status=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    elapsed = time.perf_counter() - started

    assert run.mapping_count == MAPPING_COUNT
    assert run.summary_json["total_mappings"] == MAPPING_COUNT

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # With a naive implementation this would be >= 2 * MAPPING_COUNT.
    assert len(selects) < 100, (
        f"{len(selects)} SELECT statements for {MAPPING_COUNT} mappings looks "
        f"like an N+1 regression"
    )

    per_mapping_ms = (elapsed / MAPPING_COUNT) * 1000
    print(
        f"\naudited {MAPPING_COUNT} mappings in {elapsed:.2f}s "
        f"({per_mapping_ms:.3f} ms/mapping, {len(selects)} SELECTs)"
    )
    assert per_mapping_ms < 5.0


@pytest.mark.slow
def test_bulk_audit_summary_is_internally_consistent(bulk_session):
    run = audit_service.run_audit(
        bulk_session,
        source_dataset="PERF_TEST",
        export_csv=False,
        mark_review_status=False,
    )
    summary = run.summary_json
    status_total = (
        summary["valid"]
        + summary["trial_warning"]
        + summary["discouraged"]
        + summary["deprecated"]
        + summary["inactive_snomed"]
        + summary["unknown"]
    )
    assert status_total == summary["total_mappings"]
    assert sum(summary["decisions"].values()) == summary["total_mappings"]
