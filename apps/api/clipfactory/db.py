"""SQLite engine (WAL) and session helpers."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from clipfactory.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def make_engine(db_path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        _engine = make_engine(get_settings().db_path)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests point the app at a fresh data dir)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def migrate() -> None:
    """Bring the schema to head. Safe to call on every process start, including by several processes at once."""
    from alembic import command
    from alembic.config import Config

    api_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{get_settings().db_path}")
    # The API, the worker and the live watcher start together. Without this lock they would all run a
    # pending migration at the same time and all but one would crash on "table already exists".
    with (get_settings().data_dir / ".migrate.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file is closed
        command.upgrade(cfg, "head")
