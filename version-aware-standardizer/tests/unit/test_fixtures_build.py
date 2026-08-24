"""Smoke test: the synthetic releases parse into the expected row counts."""

from __future__ import annotations

from sqlalchemy import func, select

from backend.app.models import (
    LoincConceptVersion,
    LoincMapTo,
    SnomedConceptVersion,
    SnomedHistoricalAssociation,
    SnomedInactivation,
    TerminologyRelease,
)
from tests.fixtures import synthetic


def test_loinc_release_imported(loinc_session):
    releases = loinc_session.scalars(select(TerminologyRelease)).all()
    assert len(releases) == 1
    assert releases[0].system == "LOINC"
    assert releases[0].version == synthetic.LOINC_NEW_VERSION
    assert releases[0].is_current is True
    assert len(releases[0].sha256) == 64

    concepts = loinc_session.scalar(
        select(func.count()).select_from(LoincConceptVersion)
    )
    assert concepts == len(synthetic.loinc_new_rows())

    map_rows = loinc_session.scalar(select(func.count()).select_from(LoincMapTo))
    assert map_rows == len(synthetic.loinc_new_map_to())


def test_snomed_release_imported(snomed_session):
    concepts = snomed_session.scalar(
        select(func.count()).select_from(SnomedConceptVersion)
    )
    assert concepts == len(synthetic.ALL_CONCEPTS)

    associations = snomed_session.scalars(
        select(SnomedHistoricalAssociation)
    ).all()
    assert len(associations) == len(
        synthetic.snomed_association_rows(synthetic.SNOMED_NEW_VERSION)
    )
    assert any(a.active is False for a in associations)

    inactivations = snomed_session.scalar(
        select(func.count()).select_from(SnomedInactivation)
    )
    assert inactivations == len(synthetic.INACTIVE_IN_NEW)
