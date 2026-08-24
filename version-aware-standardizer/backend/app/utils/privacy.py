"""Turning patient identifiers into pseudonyms, once, at the door.

Standardizing results means touching ``SUBJECT_ID``, ``HADM_ID`` and
``CHARTTIME`` -- so this project now handles patient-level data, and the
identifiers must stop being identifiers before anything else happens.

The method is a keyed HMAC. Not a plain hash: a bare SHA-256 of a small integer
identifier is trivially reversible by trying every integer, which for a
100-patient demo takes microseconds. The key is what makes the mapping one-way
for anyone who does not hold it.

Consequences that follow from that choice, and are relied on elsewhere:

* the same identifier always yields the same pseudonym, so rows still join;
* a different secret yields entirely different pseudonyms, so two studies
  cannot be cross-linked by pseudonym alone;
* nobody can go from pseudonym back to patient without the secret, which lives
  in the environment and never in the repository.
"""

from __future__ import annotations

import hashlib
import hmac

from backend.app.config import settings

# Long enough that collisions are not a practical concern, short enough to read
# in a CSV. 128 bits of a SHA-256 digest.
_PSEUDONYM_LENGTH = 32


class PseudonymError(RuntimeError):
    """Raised when pseudonymisation cannot be done safely."""


class Pseudonymiser:
    """Maps identifiers to stable, non-reversible keys.

    Namespacing by dataset is deliberate: subject 42 in MIMIC-III and subject 42
    in another hospital's export are different people, and must not collide into
    one pseudonym.
    """

    def __init__(self, secret: str | None = None, *, namespace: str = "MIMIC") -> None:
        secret = secret if secret is not None else settings.pseudonym_secret
        if not secret or not secret.strip():
            raise PseudonymError(
                "PSEUDONYM_SECRET is not set. Patient identifiers must not be "
                "written in the clear, and an unkeyed hash of a small integer id "
                "is reversible by brute force in moments.\n"
                "Set it in .env, e.g.\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if len(secret.strip()) < 16:
            raise PseudonymError(
                "PSEUDONYM_SECRET is too short to be a useful key; use at least "
                "16 characters, ideally 48 random ones."
            )
        self._key = secret.strip().encode("utf-8")
        self.namespace = namespace

    def key_for(self, identifier: object, *, kind: str = "subject") -> str | None:
        """Pseudonym for one identifier. ``None`` in, ``None`` out.

        A null admission id is a real state -- an outpatient result -- not
        missing data, so it must survive as null rather than becoming a
        pseudonym for "nothing".
        """
        if identifier is None:
            return None
        text = str(identifier).strip()
        if not text:
            return None
        message = f"{self.namespace}|{kind}|{text}".encode("utf-8")
        digest = hmac.new(self._key, message, hashlib.sha256).hexdigest()
        return digest[:_PSEUDONYM_LENGTH]

    def subject(self, subject_id: object) -> str | None:
        return self.key_for(subject_id, kind="subject")

    def encounter(self, hadm_id: object) -> str | None:
        return self.key_for(hadm_id, kind="encounter")


def secret_fingerprint(secret: str | None = None) -> str:
    """A short, safe identifier for *which* secret was used.

    Recorded in run manifests so two runs can be told apart, without the
    manifest carrying anything that helps recover the secret itself.
    """
    secret = secret if secret is not None else settings.pseudonym_secret
    if not secret:
        return "unset"
    return hashlib.sha256(b"fingerprint|" + secret.encode("utf-8")).hexdigest()[:12]
