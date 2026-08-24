"""Download the open-access MIMIC-III demo lab dictionary.

    python scripts/fetch_mimic_demo.py

This is the one real-world dataset in the project that needs no licence
negotiation: the MIMIC-III Clinical Database Demo v1.4 is published under the
Open Data Commons Open Database License (ODbL) v1.0 with the access policy
"Anyone can access the files, as long as they conform to the terms of the
specified license."  No credentialing, no data use agreement.

Only ``D_LABITEMS.csv`` is fetched -- the lab *dictionary*, which contains no
patient data at all: 753 rows of test name, fluid, category and a LOINC code
that real people assigned years ago.  That is exactly the historical mapping
set this project exists to audit.

Two integrity checks run on every download:

1. against a SHA-256 pinned in this file, so a silently changed upstream file
   is noticed;
2. against PhysioNet's own published ``SHA256SUMS.txt``, so a corrupted
   download is noticed even if the pin is stale.

The mappings this file carries are **not** ground truth, and the code never
treats them as such -- see ``scripts/import_mimic_labitems.py``.
"""

from __future__ import annotations

import argparse
import http.client
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.utils.checksum import sha256_file  # noqa: E402
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.fetch_mimic")

BASE_URL = "https://physionet.org/files/mimiciii-demo/1.4"
PROJECT_URL = "https://physionet.org/content/mimiciii-demo/1.4/"
DOI = "https://doi.org/10.13026/C2HM2Q"

# The demo ships its dictionary tables complete and UPPERCASE-named, while the
# *columns inside* are lowercase. (MIMIC-IV's demo is the other way round:
# lowercase, gzipped, under hosp/.) The importer resolves columns
# case-insensitively so both work, but the file name must be exact.
DATA_FILE = "D_LABITEMS.csv"
LICENSE_FILE = "LICENSE.txt"
CHECKSUM_FILE = "SHA256SUMS.txt"

# Pinned as verified on 2026-08-23 (upstream Last-Modified: 2019-10-16).
EXPECTED_SHA256 = "c573653bd06915e48a5fb5f3db01d75554350ec1a628aa91d01ef36daa4eae7f"

LICENCE_NOTICE = f"""
MIMIC-III Clinical Database Demo v1.4
  Access policy : open -- anyone may access these files
  Licence       : Open Data Commons Open Database License (ODbL) v1.0
  Project page  : {PROJECT_URL}
  DOI           : {DOI}

ODbL is share-alike: if you publish a derived database (for example a corrected
local-term to LOINC mapping table built from this file), you must attribute the
source and license the derived database under ODbL as well. Cite MIMIC-III in
any resulting publication.
""".strip()


def _download(url: str, destination: Path, timeout: float) -> None:
    request = urllib.request.Request(
        url,
        headers={
            # Identify the client honestly rather than impersonating a browser.
            "User-Agent": "version-aware-standardizer/0.1 (research; terminology audit)"
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        payload = response.read()
    destination.write_bytes(payload)


def _published_checksum(timeout: float) -> str | None:
    """The SHA-256 PhysioNet publishes for D_LABITEMS.csv, or None."""
    try:
        request = urllib.request.Request(
            f"{BASE_URL}/{CHECKSUM_FILE}",
            headers={"User-Agent": "version-aware-standardizer/0.1"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # A truncated response raises http.client.IncompleteRead, which is not
        # an OSError -- without it the whole degrade-to-warning design is void.
        log.warning("could not fetch %s: %s", CHECKSUM_FILE, exc)
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == DATA_FILE:
            return parts[0].lower()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        default=str(ROOT / "data" / "raw" / "validation"),
        help="where to write the file (default: data/raw/validation/)",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download even if the file exists"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--accept-upstream-change",
        action="store_true",
        help=(
            "proceed when the file no longer matches the pinned checksum but "
            "does match PhysioNet's published one (i.e. upstream republished it)"
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()
    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / DATA_FILE

    print(LICENCE_NOTICE)
    print()

    if target.exists() and not args.force:
        digest = sha256_file(target)
        if digest == EXPECTED_SHA256:
            print(f"Already present and verified: {target}")
            print()
            _print_next_steps(target)
            return 0
        print(
            f"{target} exists but its checksum does not match the pinned value; "
            f"re-downloading."
        )

    url = f"{BASE_URL}/{DATA_FILE}"
    print(f"Downloading {url} ...")
    try:
        _download(url, target, args.timeout)
    except (
        urllib.error.URLError,
        OSError,
        RuntimeError,
        http.client.HTTPException,
    ) as exc:
        target.unlink(missing_ok=True)
        print(f"ERROR: download failed: {exc}", file=sys.stderr)
        print(
            f"       The file can also be fetched by hand from {PROJECT_URL} "
            f"and placed at {target}.",
            file=sys.stderr,
        )
        return 1

    digest = sha256_file(target)
    size = target.stat().st_size
    print(f"  wrote {size:,} bytes")
    print(f"  sha256 {digest}")

    published = _published_checksum(args.timeout)
    if published:
        if digest != published:
            print(
                f"ERROR: the download does not match PhysioNet's published "
                f"checksum ({published}). The file is corrupt; not using it.",
                file=sys.stderr,
            )
            target.unlink(missing_ok=True)
            return 1
        print(f"  matches PhysioNet's published {CHECKSUM_FILE}")
    else:
        print(f"  WARNING: could not fetch {CHECKSUM_FILE} to cross-check")

    if digest != EXPECTED_SHA256:
        message = (
            f"the file no longer matches the checksum pinned in this script "
            f"({EXPECTED_SHA256})"
        )
        if published and digest == published and args.accept_upstream_change:
            print(f"  NOTE: {message}, but it matches upstream; accepted as requested.")
            print(f"        Update EXPECTED_SHA256 in {Path(__file__).name} to {digest}")
        else:
            # Do not leave a file we just refused sitting at the path the
            # importer is documented to read.
            target.unlink(missing_ok=True)
            print(f"ERROR: {message}.", file=sys.stderr)
            print(
                "       If PhysioNet has republished the demo, re-run with "
                "--accept-upstream-change and update the pin. The refused file "
                "has been removed.",
                file=sys.stderr,
            )
            return 1

    # The licence text travels with the data, so its terms are never separated
    # from the file they govern.
    try:
        _download(f"{BASE_URL}/{LICENSE_FILE}", dest_dir / LICENSE_FILE, args.timeout)
        print(f"  wrote {dest_dir / LICENSE_FILE}")
    except (
        urllib.error.URLError,
        OSError,
        RuntimeError,
        http.client.HTTPException,
    ) as exc:
        print(f"  WARNING: could not fetch {LICENSE_FILE}: {exc}")

    print()
    _print_next_steps(target)
    return 0


def _print_next_steps(target: Path) -> None:
    print("Next steps:")
    print(f"  python scripts/import_mimic_labitems.py --file \"{target}\"")
    print(
        "  python scripts/audit_mappings.py --source-dataset MIMIC_III "
        "--report-name mimic_loinc_audit.csv"
    )
    print()
    print(
        "Reminder: these LOINC codes are historical claims to be audited, not "
        "gold labels."
    )


if __name__ == "__main__":
    raise SystemExit(main())
