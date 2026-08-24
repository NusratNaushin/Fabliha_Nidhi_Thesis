"""Application configuration.

Every value that could differ between machines, or between terminology
releases, is read from the environment.  In particular there is *no* LOINC
version and *no* SNOMED release date anywhere in this file: those always come
from the imported release metadata (Hard Rules 1-3).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: backend/app/config.py -> backend/app -> backend -> <root>
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Version-Aware Clinical Terminology Standardizer"
    log_level: str = "INFO"

    # Defaults to a local file database so that a fresh clone can run the test
    # suite before Docker/PostgreSQL is up.  Production/dev use PostgreSQL via
    # DATABASE_URL in .env (see .env.example).
    database_url: str = f"sqlite:///{(ROOT_DIR / 'terminology.db').as_posix()}"

    snowstorm_base_url: str = "http://localhost:8080"
    snowstorm_branch: str = "MAIN"
    snowstorm_timeout_seconds: float = 30.0

    data_raw_dir: str = "data/raw"
    reports_dir: str = "data/reports"

    # Safety limit when following a LOINC MapTo chain or a SNOMED historical
    # association chain (Master Instruction sections 21 and 24).
    max_replacement_chain_depth: int = 10

    # Keys the HMAC that turns a patient identifier into a pseudonym. It has to
    # come from the environment: committing it would make every pseudonym in
    # every exported file reversible by anyone holding the repository. An empty
    # value is not silently tolerated -- the pseudonymiser refuses to start.
    pseudonym_secret: str = ""

    # Rows are streamed and written in batches of this size.
    ingest_batch_size: int = 5_000

    @property
    def raw_path(self) -> Path:
        p = Path(self.data_raw_dir)
        return p if p.is_absolute() else ROOT_DIR / p

    @property
    def reports_path(self) -> Path:
        p = Path(self.reports_dir)
        return p if p.is_absolute() else ROOT_DIR / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
