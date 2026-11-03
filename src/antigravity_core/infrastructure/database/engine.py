from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from antigravity_core.config.static_config import DatabaseStaticConfig
from antigravity_core.domain.exceptions import StartupValidationError


class DatabaseManager:
    """Manages SQLAlchemy 2.0 AsyncEngine and transactional session lifecycle."""

    def __init__(self, db_config: DatabaseStaticConfig) -> None:
        self._engine: AsyncEngine = create_async_engine(
            db_config.dsn,
            pool_size=db_config.pool_size,
            max_overflow=db_config.max_overflow,
            pool_timeout=db_config.pool_timeout_sec,
            pool_pre_ping=True,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def verify_connectivity(self) -> None:
        from antigravity_core.infrastructure.database.tables import Base

        try:
            async with self._engine.connect() as conn:
                result = await conn.scalar(text("SELECT 1"))
                if result != 1:
                    raise StartupValidationError(
                        f"Unexpected PostgreSQL connectivity check result: {result}"
                    )
            async with self._engine.begin() as ddl_conn:
                await ddl_conn.run_sync(Base.metadata.create_all)
                await ddl_conn.execute(
                    text("ALTER TABLE event_sensors ADD COLUMN IF NOT EXISTS action_command TEXT")
                )
                await ddl_conn.execute(
                    text(
                        "ALTER TABLE event_sensors ADD COLUMN IF NOT EXISTS wake_mode VARCHAR(32) NOT NULL DEFAULT 'ALWAYS'"
                    )
                )
        except SQLAlchemyError as exc:
            raise StartupValidationError(
                f"Failed to connect to PostgreSQL 18 database: {exc}"
            ) from exc

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        await self._engine.dispose()
