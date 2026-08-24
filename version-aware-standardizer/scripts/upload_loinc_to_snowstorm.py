"""Load a LOINC release into Snowstorm so its FHIR ``$lookup`` works.

    python scripts/upload_loinc_to_snowstorm.py --file data/raw/loinc/Loinc_<version>.zip
    python scripts/upload_loinc_to_snowstorm.py --file <...> --dry-run
    python scripts/upload_loinc_to_snowstorm.py --file <...> --download-cli

Snowstorm serves LOINC through the HAPI FHIR terminology loader, and its own
documentation says to feed it with the HAPI FHIR CLI rather than through
Snowstorm's native import API. This script does exactly that documented step,
with the checks that make the difference between a clean load and a confusing
one:

* Java 17 or newer is present (the CLI needs it);
* Snowstorm is actually up, before a multi-hundred-megabyte upload starts;
* the ``-u`` value is a bare system URL. Appending ``|version`` to it looks
  reasonable and is wrong: it routes the upload to the *custom* terminology
  loader, which then fails with "Did not find file matching concepts.csv";
* afterwards, a real ``$lookup`` is performed -- using a code taken from the
  release **we** imported, so nothing about the check is hard-coded.

This is the one part of the pipeline that shells out to a third-party tool, so
it reports the exact command it runs and never hides its output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.constants import LoincStatus, TerminologySystem  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models import LoincConceptVersion  # noqa: E402
from backend.app.services import release_service  # noqa: E402
from backend.app.services.snowstorm_client import (  # noqa: E402
    LOINC_SYSTEM_URI,
    SnowstormClient,
    SnowstormError,
)
from backend.app.utils.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("script.upload_loinc")

# Pinned as the current stable HAPI FHIR release; override with --cli-version.
# HAPI 6.x is deliberately not the default: it predates the LOINC package
# rename of the Multiaxial Hierarchy to "Component Hierarchy by System", which
# is what makes older CLIs choke on modern LOINC archives.
DEFAULT_CLI_VERSION = "8.10.1"
CLI_DOWNLOAD_TEMPLATE = (
    "https://github.com/hapifhir/hapi-fhir/releases/download/"
    "v{version}/hapi-fhir-{version}-cli.zip"
)
MIN_JAVA_MAJOR = 17

TOOLS_DIR = ROOT / "tools"


class UploadError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
def java_major_version() -> int | None:
    """Major version of the ``java`` on PATH, or None if there is none."""
    java = shutil.which("java")
    if not java:
        return None
    try:
        result = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # `java -version` writes to stderr, in the form: openjdk version "17.0.17"
    text = (result.stderr or "") + (result.stdout or "")
    for token in text.replace('"', " ").split():
        head = token.split(".")[0]
        if head.isdigit():
            major = int(head)
            # Java 8 reports 1.8.0_xxx; treat the leading 1 as version 8.
            if major == 1:
                parts = token.split(".")
                if len(parts) > 1 and parts[1].isdigit():
                    return int(parts[1])
                continue
            return major
    return None


def find_cli(explicit: str | None = None) -> Path | None:
    """Locate the HAPI FHIR CLI: an explicit path, PATH, or ./tools."""
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None

    for name in ("hapi-fhir-cli", "hapi-fhir-cli.cmd", "hapi-fhir-cli.bat"):
        found = shutil.which(name)
        if found:
            return Path(found)

    if TOOLS_DIR.is_dir():
        # On Windows only the .cmd launcher is executable; the extensionless
        # POSIX shell script sorts first alphabetically and would be picked
        # instead, failing with WinError 193 on every run after the download.
        wanted = {".cmd", ".bat"} if os.name == "nt" else {""}
        candidates = [c for c in sorted(TOOLS_DIR.rglob("hapi-fhir-cli*")) if c.is_file()]
        preferred = [c for c in candidates if c.suffix.lower() in wanted]
        for candidate in preferred:
            return candidate
        for candidate in candidates:
            if candidate.suffix.lower() != ".jar":
                return candidate
    return None


def download_cli(version: str, timeout: float = 600.0) -> Path:
    """Fetch and unpack the HAPI FHIR CLI distribution into ./tools."""
    import urllib.request

    url = CLI_DOWNLOAD_TEMPLATE.format(version=version)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    archive = TOOLS_DIR / f"hapi-fhir-{version}-cli.zip"

    print(f"Downloading {url} ...")
    request = urllib.request.Request(
        url, headers={"User-Agent": "version-aware-standardizer/0.1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        archive.write_bytes(response.read())
    print(f"  wrote {archive.stat().st_size:,} bytes")

    target = TOOLS_DIR / f"hapi-fhir-cli-{version}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    archive.unlink(missing_ok=True)

    for name in ("hapi-fhir-cli.cmd", "hapi-fhir-cli"):
        candidate = target / name
        if candidate.is_file():
            if os.name != "nt" and name == "hapi-fhir-cli":
                candidate.chmod(0o755)
            print(f"  CLI at {candidate}")
            return candidate
    raise UploadError(f"{archive.name} did not contain a hapi-fhir-cli launcher.")


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------
def build_command(
    cli: Path,
    loinc_zip: Path,
    fhir_base: str,
    *,
    fhir_version: str = "r4",
    system_url: str = LOINC_SYSTEM_URI,
) -> list[str]:
    """The exact invocation Snowstorm's own documentation prescribes."""
    if "|" in system_url:
        raise UploadError(
            f"-u must be a bare system URL; {system_url!r} contains '|'. "
            f"Appending a version routes the upload to the custom-terminology "
            f"loader, which fails with 'Did not find file matching concepts.csv'."
        )
    return [
        str(cli),
        "upload-terminology",
        "-d",
        str(loinc_zip),
        "-v",
        fhir_version,
        "-t",
        fhir_base.rstrip("/"),
        "-u",
        system_url,
    ]


def verification_code(session) -> tuple[str | None, str | None]:
    """An ACTIVE code from the LOINC release we imported, and its version.

    Using our own release rather than a famous example keeps the check honest:
    it proves the *loaded* content is queryable, and it hard-codes nothing.
    """
    release = release_service.get_current(session, TerminologySystem.LOINC.value)
    if release is None:
        return None, None
    code = session.scalar(
        select(LoincConceptVersion.loinc_num)
        .where(
            LoincConceptVersion.release_version == release.version,
            LoincConceptVersion.status == LoincStatus.ACTIVE.value,
        )
        .order_by(LoincConceptVersion.loinc_num)
        .limit(1)
    )
    return code, release.version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", required=True, help="the official LOINC release ZIP")
    parser.add_argument(
        "--fhir-base",
        default=None,
        help="Snowstorm FHIR base (default: <SNOWSTORM_BASE_URL>/fhir)",
    )
    parser.add_argument("--fhir-version", default="r4")
    parser.add_argument(
        "--cli", default=None, help="path to hapi-fhir-cli, if not on PATH"
    )
    parser.add_argument("--cli-version", default=DEFAULT_CLI_VERSION)
    parser.add_argument(
        "--download-cli",
        action="store_true",
        help="download the HAPI FHIR CLI into ./tools if it is not already available",
    )
    parser.add_argument(
        "--verify-code",
        default=None,
        help="LOINC code for the post-upload check (default: one from the imported release)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and print the command, but do not upload",
    )
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)

    configure_logging()

    loinc_zip = Path(args.file)
    if not loinc_zip.is_file():
        print(f"ERROR: {loinc_zip} does not exist.", file=sys.stderr)
        print(
            "Download 'LOINC Complete' from the official LOINC downloads page "
            "(a free account) and place it in data/raw/loinc/.",
            file=sys.stderr,
        )
        return 2
    if not zipfile.is_zipfile(loinc_zip):
        print(f"ERROR: {loinc_zip} is not a ZIP archive.", file=sys.stderr)
        return 2

    fhir_base = args.fhir_base or f"{settings.snowstorm_base_url.rstrip('/')}/fhir"

    # -- Java ------------------------------------------------------------
    major = java_major_version()
    if major is None:
        print("ERROR: no `java` on PATH. The HAPI FHIR CLI needs Java 17+.", file=sys.stderr)
        return 2
    if major < MIN_JAVA_MAJOR:
        print(
            f"ERROR: Java {major} found; the HAPI FHIR CLI needs "
            f"{MIN_JAVA_MAJOR} or newer.",
            file=sys.stderr,
        )
        return 2
    print(f"Java:       {major}")

    # -- CLI -------------------------------------------------------------
    cli = find_cli(args.cli)
    if cli is None and args.download_cli:
        try:
            cli = download_cli(args.cli_version)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            print(f"ERROR: could not obtain the HAPI FHIR CLI: {exc}", file=sys.stderr)
            return 1
    if cli is None:
        print("ERROR: hapi-fhir-cli not found.", file=sys.stderr)
        print(
            f"       Re-run with --download-cli, or download "
            f"{CLI_DOWNLOAD_TEMPLATE.format(version=args.cli_version)} "
            f"and pass --cli <path>.",
            file=sys.stderr,
        )
        return 2
    print(f"CLI:        {cli}")

    # A .cmd/.bat launcher hands the command line back to cmd.exe, which
    # re-parses it: a path containing & | ^ < > ( ) " %% ! would be split or
    # swallowed, dropping later flags -- and cmd can still exit 0.
    if cli.suffix.lower() in {".cmd", ".bat"}:
        hostile = sorted(set(str(loinc_zip)) & set('&|^<>()"%!'))
        if hostile:
            print(
                f"ERROR: the LOINC path contains {hostile}, which cmd.exe re-parses "
                f"when invoking {cli.name}.",
                file=sys.stderr,
            )
            print(
                "       Move the archive somewhere without those characters, or "
                "pass the .jar launcher via --cli.",
                file=sys.stderr,
            )
            return 2

    print(f"LOINC ZIP:  {loinc_zip}")
    print(f"FHIR base:  {fhir_base}")

    # -- Snowstorm -------------------------------------------------------
    client = SnowstormClient()
    try:
        health = client.health()
        if not health.available:
            print(
                f"ERROR: Snowstorm is not reachable at {client.base_url}: "
                f"{health.detail}",
                file=sys.stderr,
            )
            print(
                "       Start it first: cd infra/snowstorm && docker compose up -d",
                file=sys.stderr,
            )
            return 1
        print(f"Snowstorm:  up ({health.version or 'version unknown'})")

        try:
            command = build_command(
                cli,
                loinc_zip,
                fhir_base,
                fhir_version=args.fhir_version,
            )
        except UploadError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        print()
        print("Command:")
        print("  " + " ".join(f'"{p}"' if " " in p else p for p in command))
        print()

        if args.dry_run:
            print("Dry run: nothing uploaded.")
            return 0

        print("Uploading (LOINC is large; this takes a while) ...")
        try:
            result = subprocess.run(command, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(
                f"ERROR: the upload did not finish within {args.timeout}s.",
                file=sys.stderr,
            )
            return 1
        except OSError as exc:
            print(f"ERROR: could not run the CLI: {exc}", file=sys.stderr)
            return 1
        if result.returncode != 0:
            print(
                f"ERROR: hapi-fhir-cli exited {result.returncode}. Its output is "
                f"above.",
                file=sys.stderr,
            )
            return 1

        # -- verify ------------------------------------------------------
        code = args.verify_code
        version = None
        if not code:
            with SessionLocal() as session:
                code, version = verification_code(session)
        if not code:
            print()
            print(
                "Upload finished, but no LOINC release is imported locally, so "
                "there is no code to verify with. Pass --verify-code to check."
            )
            return 0

        print()
        print(f"Verifying with {code} (ACTIVE in LOINC {version or 'unknown'}) ...")
        try:
            payload = client.lookup_loinc(code)
        except SnowstormError as exc:
            print(f"ERROR: the verification lookup failed: {exc}", file=sys.stderr)
            return 1
        if not payload:
            print(
                f"ERROR: $lookup returned nothing for {code}. The upload reported "
                f"success but the code system is not queryable -- check "
                f"{fhir_base}/CodeSystem?url={LOINC_SYSTEM_URI}",
                file=sys.stderr,
            )
            return 1
        display = next(
            (
                p.get("valueString")
                for p in payload.get("parameter", [])
                if p.get("name") == "display"
            ),
            None,
        )
        print(f"  display: {display!r}")
        print()
        print("LOINC is loaded and queryable through Snowstorm's FHIR API.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
