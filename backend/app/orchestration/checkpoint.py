from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings


def postgres_checkpoint_dsn(settings: Settings | None = None) -> str:
    """Return a psycopg-compatible DSN without duplicating database configuration."""
    settings = settings or get_settings()
    explicit_dsn = (settings.langgraph_checkpoint_dsn or "").strip()
    if explicit_dsn:
        return explicit_dsn
    url = make_url(settings.database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


@dataclass
class CheckpointRuntime:
    """Own the PostgreSQL checkpoint pool for one worker process."""

    settings: Settings
    pool: object | None = None
    saver: object | None = None

    async def start(self, *, setup: bool = True) -> object:
        if self.saver is not None:
            return self.saver

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(
            conninfo=postgres_checkpoint_dsn(self.settings),
            min_size=1,
            max_size=4,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await self.pool.open()
        self.saver = AsyncPostgresSaver(self.pool)
        if setup:
            await self.saver.setup()
        return self.saver

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
        self.pool = None
        self.saver = None


def create_checkpoint_runtime(settings: Settings | None = None) -> CheckpointRuntime:
    return CheckpointRuntime(settings=settings or get_settings())
