"""Pseudonymisation: the properties that make it worth doing at all.

A hash of a small integer is not a pseudonym -- with 100 patients you could
recover every identifier by trying 1 to 1000. What makes this one-way is the
key, so most of these tests are about the key.
"""

from __future__ import annotations

import re

import pytest

from backend.app.utils.privacy import PseudonymError, Pseudonymiser, secret_fingerprint

SECRET = "a-test-secret-that-is-long-enough-to-be-accepted"
OTHER = "an-entirely-different-secret-of-adequate-length"


def test_it_refuses_to_run_without_a_secret():
    """Failing loudly beats writing reversible pseudonyms and calling it privacy."""
    for missing in ("", "   ", None):
        with pytest.raises(PseudonymError, match="PSEUDONYM_SECRET"):
            Pseudonymiser(missing if missing is not None else "")


def test_it_refuses_a_secret_too_short_to_be_a_key():
    with pytest.raises(PseudonymError, match="too short"):
        Pseudonymiser("abc")


def test_the_same_identifier_always_gives_the_same_pseudonym():
    """Rows still have to join after pseudonymisation, or the data is useless."""
    p = Pseudonymiser(SECRET)
    assert p.subject(42) == p.subject(42) == p.subject("42")


def test_different_identifiers_give_different_pseudonyms():
    p = Pseudonymiser(SECRET)
    keys = {p.subject(i) for i in range(500)}
    assert len(keys) == 500, "a collision here would merge two patients"


def test_changing_the_secret_changes_every_pseudonym():
    """So two studies cannot be cross-linked by pseudonym alone."""
    assert Pseudonymiser(SECRET).subject(42) != Pseudonymiser(OTHER).subject(42)


def test_a_subject_and_an_encounter_never_collide():
    """Subject 42 and admission 42 are different things and must not share a key."""
    p = Pseudonymiser(SECRET)
    assert p.subject(42) != p.encounter(42)


def test_datasets_are_namespaced():
    """Subject 42 in one hospital is not subject 42 in another."""
    a = Pseudonymiser(SECRET, namespace="MIMIC_III")
    b = Pseudonymiser(SECRET, namespace="HOSPITAL_A")
    assert a.subject(42) != b.subject(42)


def test_a_pseudonym_does_not_contain_the_identifier():
    p = Pseudonymiser(SECRET)
    for value in ("42", "123456", "99999"):
        assert value not in p.subject(value)


def test_a_pseudonym_looks_like_a_pseudonym():
    p = Pseudonymiser(SECRET)
    assert re.fullmatch(r"[0-9a-f]{32}", p.subject(1))


def test_null_survives_as_null():
    """A missing admission id means an outpatient result -- a real state.

    Turning it into a pseudonym for "nothing" would make every outpatient row
    look like it belonged to the same admission.
    """
    p = Pseudonymiser(SECRET)
    assert p.encounter(None) is None
    assert p.encounter("") is None
    assert p.encounter("   ") is None


def test_the_fingerprint_identifies_the_secret_without_revealing_it():
    fingerprint = secret_fingerprint(SECRET)
    assert fingerprint == secret_fingerprint(SECRET)
    assert fingerprint != secret_fingerprint(OTHER)
    assert SECRET not in fingerprint
    assert len(fingerprint) == 12
    assert secret_fingerprint("") == "unset"
