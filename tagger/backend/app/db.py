from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=10,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def configure_engine(url: str) -> Engine:
    """Point the app at a specific database (used by tests)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, pool_pre_ping=True, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    s = session_factory()()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = session_factory()()
    try:
        yield s
    finally:
        s.close()
