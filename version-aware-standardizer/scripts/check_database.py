"""Verify that a database is reachable and its schema is current.

    python scripts/check_database.py
    python scripts/check_database.py --database-url postgresql+psycopg://user:pw@host/db

Answers four questions, and says which one failed rather than just "error":

1. can we connect at all?
2. is every expected table present?
3. is the Alembic revision the head revision?
4. do the release-registry invariants hold (at most one current release per
   terminology system)?

Exit code 0 only when all four pass.

The engine is built here from the URL under test rather than reused from the
application, so ``--database-url`` really does check the database you named --
mutating the environment after ``backend.app.database`` has been imported would
be silently ignored, which is the worst possible behaviour for a check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.constants import TerminologySystem  # noqa: E402
from backend.app.database import Base  # noqa: E402
from backend.app import models  # noqa: E402,F401  (registers the mappers)
from backend.app.models import TerminologyRelease  # noqa: E402


def alembic_head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="the database to check (default: the application's DATABASE_URL)",
    )
    parser.add_argument(
        "--skip-revision-check",
        action="store_true",
        help="do not compare the Alembic revision against head",
    )
    args = parser.parse_args(argv)

    url = args.database_url or settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)

    print(f"Database: {engine.url.render_as_string(hide_password=True)}")
    print(f"Dialect:  {engine.dialect.name}")
    print()

    failures: list[str] = []

    try:
        # -- 1. connectivity -----------------------------------------------
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            print("[ok]   connection")
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            print(f"[FAIL] connection: {type(exc).__name__}: {exc}")
            print()
            print("  Is the database up?    docker compose up -d")
            print("  Is DATABASE_URL right? See .env / .env.example")
            print("  Port 5432 already taken by a native PostgreSQL? Start the")
            print("  container on another port:  POSTGRES_PORT=55432 docker compose up -d")
            return 1

        # -- 2. tables ------------------------------------------------------
        present = set(inspect(engine).get_table_names())
        expected = set(Base.metadata.tables)
        missing = sorted(expected - present)
        if missing:
            failures.append("tables")
            print(f"[FAIL] tables: {len(missing)} missing: {missing}")
            print("       run: python -m alembic upgrade head")
        else:
            print(f"[ok]   tables ({len(expected)} present)")

        # -- 3. migration revision -----------------------------------------
        if args.skip_revision_check:
            print("[skip] alembic revision")
        else:
            try:
                head = alembic_head()
                current = None
                if "alembic_version" in present:
                    with engine.connect() as connection:
                        current = connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar()
                if current is None:
                    failures.append("revision")
                    print("[FAIL] alembic revision: no alembic_version row")
                    print("       run: python -m alembic upgrade head")
                elif current != head:
                    failures.append("revision")
                    print(f"[FAIL] alembic revision: at {current}, head is {head}")
                    print("       run: python -m alembic upgrade head")
                else:
                    print(f"[ok]   alembic revision ({current})")
            except Exception as exc:  # noqa: BLE001
                failures.append("revision")
                print(f"[FAIL] alembic revision: {type(exc).__name__}: {exc}")

        # -- 4. registry invariants ----------------------------------------
        if "terminology_release" in present:
            with Session(bind=engine) as session:
                broken: list[str] = []
                for system in TerminologySystem:
                    current_rows = list(
                        session.scalars(
                            select(TerminologyRelease).where(
                                TerminologyRelease.system == system.value,
                                TerminologyRelease.is_current.is_(True),
                            )
                        )
                    )
                    if len(current_rows) > 1:
                        broken.append(
                            f"{system.value} has {len(current_rows)} current releases: "
                            f"{[r.version for r in current_rows]}"
                        )
                if broken:
                    failures.append("invariants")
                    print("[FAIL] registry invariants:")
                    for line in broken:
                        print(f"       {line}")
                else:
                    print(
                        "[ok]   registry invariants "
                        "(at most one current release per system)"
                    )

                any_release = session.scalar(select(TerminologyRelease.id).limit(1))
                if any_release is None:
                    print()
                    print("Note: no terminology release has been imported yet.")
                    print(
                        "      Audits will abstain with NO_CURRENT_RELEASE until one is."
                    )
        else:
            print("[skip] registry invariants (table missing)")
    finally:
        engine.dispose()

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("All database checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
