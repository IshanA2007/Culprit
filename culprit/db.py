"""Async SQLAlchemy engine + session plumbing.

The engine is constructed lazily (nothing connects at import time), so importing
the app or settings never requires a live Postgres. ``get_session`` is the
FastAPI dependency; ``make_engine`` lets tests point at an ephemeral database.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from culprit.config import get_settings


def make_engine(url: str) -> AsyncEngine:
    """Build an async engine for an explicit URL (tests use this directly)."""
    return create_async_engine(url, pool_pre_ping=True)


@lru_cache
def get_engine() -> AsyncEngine:
    """Process-wide async engine bound to the configured DATABASE_URL."""
    return make_engine(get_settings().database_url)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, closes it on request completion."""
    async with get_sessionmaker()() as session:
        yield session
