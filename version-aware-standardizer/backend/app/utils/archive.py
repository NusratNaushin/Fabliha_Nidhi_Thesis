"""Uniform read access to a terminology release that is either a ZIP or a
already-extracted directory.

Both LOINC and SNOMED ship deeply nested archives whose internal folder names
change from release to release (``Loinc_2.xx/LoincTable/Loinc.csv``,
``SnomedCT_InternationalRF2_PRODUCTION_<date>T.../Snapshot/Terminology/...``).

Master Instruction sections 9 and 13 are explicit about this: locate members by
*basename / filename pattern*, never by assuming a fixed folder name.
"""

from __future__ import annotations

import fnmatch
import io
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterator, Sequence


class ArchiveError(RuntimeError):
    """Raised when a release archive is missing, unreadable or malformed."""


@dataclass(frozen=True)
class ArchiveMember:
    """One file inside a release, identified by its full internal path."""

    name: str  # full path inside the archive (posix separators)

    @property
    def basename(self) -> str:
        return self.name.rsplit("/", 1)[-1]


class ReleaseArchive:
    """Read-only view over a release ZIP file or an extracted release folder."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise ArchiveError(f"Release path does not exist: {self.path}")

        self._zip: zipfile.ZipFile | None = None
        if self.path.is_file():
            if not zipfile.is_zipfile(self.path):
                raise ArchiveError(
                    f"Release file is not a valid ZIP archive: {self.path}"
                )
            self._zip = zipfile.ZipFile(self.path)
            self._members = [
                ArchiveMember(info.filename.replace("\\", "/"))
                for info in self._zip.infolist()
                if not info.is_dir()
            ]
        elif self.path.is_dir():
            self._members = [
                ArchiveMember(p.relative_to(self.path).as_posix())
                for p in sorted(self.path.rglob("*"))
                if p.is_file()
            ]
        else:
            raise ArchiveError(f"Release path is neither file nor directory: {self.path}")

        if not self._members:
            raise ArchiveError(f"Release archive is empty: {self.path}")

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> "ReleaseArchive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- discovery ---------------------------------------------------------
    @property
    def members(self) -> Sequence[ArchiveMember]:
        return tuple(self._members)

    def find_by_basename(self, basename: str) -> list[ArchiveMember]:
        """All members whose file name equals ``basename`` (case-insensitive)."""
        wanted = basename.lower()
        return [m for m in self._members if m.basename.lower() == wanted]

    def find_by_pattern(self, pattern: str) -> list[ArchiveMember]:
        """All members whose *basename* matches a glob such as
        ``sct2_Concept_Snapshot*.txt`` (case-insensitive)."""
        pat = pattern.lower()
        return [m for m in self._members if fnmatch.fnmatch(m.basename.lower(), pat)]

    def require_basename(self, basename: str) -> ArchiveMember:
        matches = self.find_by_basename(basename)
        if not matches:
            raise ArchiveError(
                f"Required file '{basename}' not found in release {self.path.name}. "
                f"Archive contains {len(self._members)} files; "
                f"first few: {[m.basename for m in self._members[:8]]}"
            )
        # Prefer the shallowest path when a release ships several copies
        # (e.g. an 'AccessoryFiles' duplicate next to the canonical table).
        matches.sort(key=lambda m: (m.name.count("/"), len(m.name)))
        return matches[0]

    def require_pattern(self, pattern: str) -> ArchiveMember:
        matches = self.find_by_pattern(pattern)
        if not matches:
            raise ArchiveError(
                f"No file matching '{pattern}' found in release {self.path.name}."
            )
        matches.sort(key=lambda m: (m.name.count("/"), len(m.name)))
        return matches[0]

    # -- reading -----------------------------------------------------------
    @contextmanager
    def open_binary(self, member: ArchiveMember) -> Iterator[IO[bytes]]:
        if self._zip is not None:
            with self._zip.open(member.name) as fh:
                yield fh
        else:
            with (self.path / member.name).open("rb") as fh:
                yield fh

    @contextmanager
    def open_text(
        self, member: ArchiveMember, encoding: str = "utf-8-sig", newline: str = ""
    ) -> Iterator[io.TextIOWrapper]:
        """Open a member as text.

        ``utf-8-sig`` is deliberate: official LOINC CSVs are shipped with a BOM
        and the first column would otherwise be read as ``\ufeffLOINC_NUM``.
        """
        with self.open_binary(member) as raw:
            wrapper = io.TextIOWrapper(raw, encoding=encoding, newline=newline)
            try:
                yield wrapper
            finally:
                wrapper.detach()
