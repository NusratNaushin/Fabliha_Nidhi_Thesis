#!/usr/bin/env bash
# One-shot setup for the Version-Aware Clinical Terminology Standardizer.
#
# The POSIX counterpart of scripts/bootstrap.ps1, for macOS and Linux.
# It never downloads LOINC or SNOMED CT content: those are licence-controlled
# and must be placed in data/raw/ by hand.
#
#   ./scripts/bootstrap.sh
#   ./scripts/bootstrap.sh --skip-snowstorm --skip-migrations

set -euo pipefail

SKIP_SNOWSTORM=0
SKIP_MIGRATIONS=0
for arg in "$@"; do
    case "$arg" in
        --skip-snowstorm)  SKIP_SNOWSTORM=1 ;;
        --skip-migrations) SKIP_MIGRATIONS=1 ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cyan()   { printf '\033[36m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
grey()   { printf '\033[90m%s\033[0m\n' "$*"; }

cyan "Project root: $ROOT"

# --- 1. Python -------------------------------------------------------------
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON not found. Install Python 3.11 or newer." >&2
    exit 1
fi
VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
cyan "Python $VERSION"
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        f"Python 3.11+ is required; found {sys.version_info.major}.{sys.version_info.minor}."
    )
PY

# --- 2. Virtual environment ------------------------------------------------
if [ ! -d .venv ]; then
    cyan "Creating virtual environment ..."
    "$PYTHON" -m venv .venv
else
    grey "Virtual environment already exists."
fi
VENV_PY="$ROOT/.venv/bin/python"

cyan "Installing dependencies ..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r requirements.txt --quiet

# --- 3. .env ---------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    yellow "Created .env from .env.example -- edit the password before use."
else
    grey ".env already exists; leaving it alone."
fi

# --- 4. Snowstorm ----------------------------------------------------------
if [ "$SKIP_SNOWSTORM" -eq 0 ]; then
    if [ ! -d infra/snowstorm ]; then
        cyan "Cloning Snowstorm (infrastructure only, never modified) ..."
        if command -v gh >/dev/null 2>&1; then
            gh repo clone IHTSDO/snowstorm infra/snowstorm
        else
            git clone --depth 1 https://github.com/IHTSDO/snowstorm.git infra/snowstorm
        fi
    else
        grey "infra/snowstorm already present."
    fi

    # Elasticsearch needs a raised mmap count. On Linux this is a host setting;
    # on macOS the Docker VM handles it.
    if [ "$(uname -s)" = "Linux" ]; then
        CURRENT="$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)"
        if [ "$CURRENT" -lt 262144 ]; then
            yellow "Elasticsearch needs vm.max_map_count >= 262144 (currently $CURRENT)."
            yellow "  sudo sysctl -w vm.max_map_count=262144"
        fi
    fi
fi

# --- 5. Database -----------------------------------------------------------
if [ "$SKIP_MIGRATIONS" -eq 0 ]; then
    cyan "Applying database migrations ..."
    if ! "$VENV_PY" -m alembic upgrade head; then
        yellow "Migrations failed. Is PostgreSQL up?  docker compose up -d"
    fi
fi

# --- 6. Next steps ---------------------------------------------------------
echo
green "Bootstrap complete."
echo
cyan "Next steps:"
cat <<'STEPS'
  1. docker compose up -d                              # PostgreSQL
  2. (cd infra/snowstorm && docker compose up -d)      # Snowstorm + Elasticsearch (~8 GB RAM)
  3. Place the official releases (they are NOT downloaded for you):
       data/raw/loinc/Loinc_<version>.zip               # free LOINC account
       data/raw/snomed/SnomedCT_*RF2*.zip               # licensed affiliate access
  4. .venv/bin/python scripts/import_loinc.py  --file data/raw/loinc/<file>.zip  --version <version>
  5. .venv/bin/python scripts/import_snomed.py --file data/raw/snomed/<file>.zip --version <YYYYMMDD>
  6. .venv/bin/python -m uvicorn backend.app.main:app --reload   # http://localhost:8000/docs
STEPS
echo
cyan "No terminology files yet? Run the full pipeline on synthetic data:"
echo "  .venv/bin/python scripts/demo_end_to_end.py"
echo
cyan "The open-access MIMIC-III demo needs no licence:"
echo "  .venv/bin/python scripts/fetch_mimic_demo.py"
