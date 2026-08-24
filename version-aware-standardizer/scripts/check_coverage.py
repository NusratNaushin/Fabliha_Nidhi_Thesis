"""Enforce the two coverage floors this project committed to.

    python -m pytest --cov=backend/app --cov-report=xml
    python scripts/check_coverage.py --min-overall 85 --min-core 95

The Master Instruction sets >= 85% overall and >= 95% for the core resolver
modules.  A single overall figure hides a thin resolver behind fat, easily
covered schema files, so the two are checked separately.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The modules that actually decide whether a clinical mapping is still valid.
CORE_MODULES = (
    "backend/app/services/loinc_resolver.py",
    "backend/app/services/snomed_resolver.py",
)


def _normalise(filename: str) -> str:
    return filename.replace("\\", "/").lstrip("./")


def read_coverage(path: Path) -> tuple[float, dict[str, float]]:
    """Return (overall percent, {normalised filename: percent})."""
    tree = ET.parse(path)
    root = tree.getroot()

    overall_attr = root.get("line-rate")
    per_file: dict[str, float] = {}
    covered_total = 0
    valid_total = 0

    for class_element in root.iter("class"):
        filename = _normalise(class_element.get("filename", ""))
        lines = list(class_element.iter("line"))
        valid = len(lines)
        covered = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        if valid:
            per_file[filename] = covered / valid * 100.0
        covered_total += covered
        valid_total += valid

    if overall_attr is not None:
        overall = float(overall_attr) * 100.0
    elif valid_total:
        overall = covered_total / valid_total * 100.0
    else:
        overall = 0.0

    return overall, per_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        default=str(ROOT / "coverage.xml"),
        help="path to a Cobertura coverage.xml (default: ./coverage.xml)",
    )
    parser.add_argument("--min-overall", type=float, default=85.0)
    parser.add_argument("--min-core", type=float, default=95.0)
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        print(
            "Generate it first:\n"
            "  python -m pytest --cov=backend/app --cov-report=xml",
            file=sys.stderr,
        )
        return 2

    overall, per_file = read_coverage(path)

    failures: list[str] = []

    status = "ok  " if overall >= args.min_overall else "FAIL"
    print(f"[{status}] overall {overall:6.2f}%   (floor {args.min_overall:.0f}%)")
    if overall < args.min_overall:
        failures.append(f"overall {overall:.2f}% < {args.min_overall:.0f}%")

    print()
    print("Core resolver modules:")
    for module in CORE_MODULES:
        # Cobertura filenames are relative to the coverage source root, which
        # may or may not include the leading package directories.
        match = next(
            (name for name in per_file if name.endswith(module) or module.endswith(name)),
            None,
        )
        if match is None:
            failures.append(f"{module} absent from the coverage report")
            print(f"  [FAIL] {module}: not present in {path.name}")
            continue
        percent = per_file[match]
        status = "ok  " if percent >= args.min_core else "FAIL"
        print(f"  [{status}] {module}  {percent:6.2f}%   (floor {args.min_core:.0f}%)")
        if percent < args.min_core:
            failures.append(f"{module} {percent:.2f}% < {args.min_core:.0f}%")

    print()
    if failures:
        print("Coverage gate FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Coverage gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
