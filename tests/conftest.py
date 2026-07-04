"""Async DB + HTTP test fixtures.

Round-trip and pipeline tests run against an ephemeral ``culprit_test`` database
(kept separate from the main ``culprit`` db so Alembic autogenerate still sees a
pristine schema). Setup runs once per session over a plain connection — no
``docker exec`` — so the same conftest works locally and against CI's Postgres
service. Isolation between tests is a TRUNCATE of every table on engine teardown.

``db_session`` and ``client`` share one function-scoped engine bound to the test
event loop, so an HTTP handler's writes are visible to a direct query in the same
test (and vice versa).
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from culprit.db import get_session, make_engine
from culprit.models import Base

# Maintenance DB used only to CREATE/DROP the test database.
ADMIN_URL = os.environ.get(
    "CULPRIT_TEST_ADMIN_URL",
    "postgresql+asyncpg://culprit:culprit@localhost:5432/postgres",
)
TEST_URL = os.environ.get(
    "CULPRIT_TEST_DATABASE_URL",
    "postgresql+asyncpg://culprit:culprit@localhost:5432/culprit_test",
)


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """(Re)create culprit_test and its schema once for the whole test session."""

    async def setup() -> None:
        admin = make_engine(ADMIN_URL).execution_options(isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(
                text("DROP DATABASE IF EXISTS culprit_test WITH (FORCE)")
            )
            await conn.execute(text("CREATE DATABASE culprit_test"))
        await admin.dispose()

        engine = make_engine(TEST_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(setup())
    yield


@pytest_asyncio.fixture
async def db_engine():
    """A function-scoped engine on culprit_test; truncates every table on teardown."""
    engine = make_engine(TEST_URL)
    yield engine
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(
                text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE')
            )
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine):
    """httpx client against the ASGI app, with get_session bound to culprit_test."""
    from culprit.app import app

    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)
