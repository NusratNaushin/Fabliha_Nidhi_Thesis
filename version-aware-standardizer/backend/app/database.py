"""SQLAlchemy 2.x engine / session wiring."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the project."""


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """Foreign keys are off by default in SQLite; the ORM relies on them."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _make_engine(url: str) -> Engine:
    """Build an engine, with SQLite's quirks handled for SQLite only.

    The PRAGMA hook is attached to *this engine instance* rather than to the
    Engine class: a class-level listener would also fire on any other engine the
    process creates -- for example the one scripts/check_database.py builds for
    a PostgreSQL URL -- and send it a statement it cannot parse.
    """
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # check_same_thread=False lets FastAPI's threadpool share the session
        # factory during tests; SQLite is a test/bootstrap convenience only.
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs.pop("pool_pre_ping")
    built = create_engine(url, **kwargs)
    if built.dialect.name == "sqlite":
        event.listen(built, "connect", _enable_sqlite_foreign_keys)
    return built


engine: Engine = _make_engine(settings.database_url)


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_all() -> None:
    """Create every table. Alembic owns migrations; this is for tests/bootstrap."""
    from backend.app import models  # noqa: F401  (registers the mappers)

    Base.metadata.create_all(bind=engine)
