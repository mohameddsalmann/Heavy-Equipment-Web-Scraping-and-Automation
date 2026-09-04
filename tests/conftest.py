from __future__ import annotations

import os
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base

TEST_POSTGRES_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test_user:test_password@localhost:5433/equipment_monitor_test",
)


@pytest_asyncio.fixture
async def pg_engine():
    """Real PostgreSQL 16 engine using NullPool for per-test asyncpg event-loop isolation."""
    engine = create_async_engine(
        TEST_POSTGRES_URL,
        echo=False,
        future=True,
        poolclass=NullPool,
    )

    # 1. Hard verification: Confirm dialect is REAL PostgreSQL
    assert engine.dialect.name == "postgresql", (
        f"Tests MUST run against a real PostgreSQL instance, got '{engine.dialect.name}'"
    )

    # 2. Verify PostgreSQL version >= 16
    async with engine.connect() as conn:
        res = await conn.execute(text("SHOW server_version;"))
        version_str = res.scalar()
        assert version_str is not None, "Failed to retrieve PostgreSQL version"
        major_version = int(version_str.split(".")[0])
        assert major_version >= 16, f"Expected PostgreSQL 16+, got: {version_str}"

        # Initialize schema
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_db_session(pg_engine):
    """Provides an isolated session against real PostgreSQL, truncating tables after each test."""
    session_maker = async_sessionmaker(
        bind=pg_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        yield session
        await session.rollback()

    # Truncate tables with CASCADE to preserve isolation
    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text("TRUNCATE TABLE alert_events, price_history, listings, source_scan_status CASCADE;")
        )


@pytest_asyncio.fixture
async def session_factory(pg_engine):
    """Factory yielding sessions connected to the real PostgreSQL 16 test database."""
    maker = async_sessionmaker(
        bind=pg_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield maker

    async with pg_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text("TRUNCATE TABLE alert_events, price_history, listings, source_scan_status CASCADE;")
        )
