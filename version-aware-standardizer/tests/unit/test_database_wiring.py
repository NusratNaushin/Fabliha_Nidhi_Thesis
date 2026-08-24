"""Engine construction.

Regression cover for a bug that only appears once a process holds two engines:
scripts/check_database.py builds its own engine for a ``--database-url``, and a
SQLite PRAGMA hook registered on the *Engine class* would fire on that engine
too and send ``PRAGMA foreign_keys=ON`` to PostgreSQL.
"""

from __future__ import annotations

from sqlalchemy import event

from backend.app.database import _enable_sqlite_foreign_keys, _make_engine


def _has_sqlite_hook(engine) -> bool:
    return event.contains(engine, "connect", _enable_sqlite_foreign_keys)


def test_sqlite_engine_gets_the_foreign_key_pragma(tmp_path):
    engine = _make_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    try:
        assert _has_sqlite_hook(engine) is True
    finally:
        engine.dispose()


def test_a_postgres_engine_never_gets_the_sqlite_pragma():
    # No connection is made: create_engine is lazy, so this is safe without a
    # server and still proves the listener was not attached.
    engine = _make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    try:
        assert _has_sqlite_hook(engine) is False
        assert engine.dialect.name == "postgresql"
    finally:
        engine.dispose()


def test_two_engines_do_not_share_hooks(tmp_path):
    sqlite_engine = _make_engine(f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    postgres_engine = _make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    try:
        assert _has_sqlite_hook(sqlite_engine) is True
        assert _has_sqlite_hook(postgres_engine) is False
    finally:
        sqlite_engine.dispose()
        postgres_engine.dispose()


def test_sqlite_foreign_keys_are_actually_enforced(tmp_path):
    """The PRAGMA is not cosmetic: the ORM relies on cascade behaviour."""
    from sqlalchemy import text

    engine = _make_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_pool_pre_ping_is_off_for_sqlite_only(tmp_path):
    sqlite_engine = _make_engine(f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    postgres_engine = _make_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    try:
        assert sqlite_engine.pool._pre_ping is False
        assert postgres_engine.pool._pre_ping is True
    finally:
        sqlite_engine.dispose()
        postgres_engine.dispose()
