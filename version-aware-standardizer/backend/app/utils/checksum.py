"""SHA-256 helpers.

Hard Rule 11: every terminology release must carry a SHA-256 checksum.
Hard Rule 12: the same release/checksum must never be imported twice -- which
is what makes the checksum a *content* identity rather than a filename
identity (see the checksum test in section 45 of the Master Instruction).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024  # 1 MiB


def sha256_file(path: str | Path) -> str:
    """Return the lowercase hex SHA-256 of a file, streamed in 1 MiB chunks."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Cannot checksum: not a file: {p}")
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
