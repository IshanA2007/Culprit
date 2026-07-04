"""Async DB test fixtures.

Round-trip and pipeline tests run against an ephemeral ``culprit_test`` database
(kept separate from the main ``culprit`` db so Alembic autogenerate still sees a
pristine schema). Setup runs once per session over a plain connection — no
``docker exec`` — so the same conftest works locally and against CI's Postgres
service. Isolation between tests is a TRUNCATE of every table.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from culprit.db import make_engine
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
async def db_session():
    """A committed-writes session against culprit_test; truncates on teardown."""
    engine = make_engine(TEST_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(
                text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE')
            )
    await engine.dispose()
