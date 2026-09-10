import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


def test_settings_include_phase1_infrastructure_defaults():
    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.encryption_key == ""
    assert settings.max_conversation_messages == 20
    assert settings.rate_limit_per_minute == 5


def test_database_engine_and_session_factory_are_lazy_singletons(monkeypatch):
    from app import database

    created_engines: list[tuple[str, dict]] = []
    created_factories: list[tuple[object, dict]] = []

    class FakeEngine:
        pass

    class FakeSessionFactory:
        pass

    def fake_create_async_engine(url: str, **kwargs) -> FakeEngine:
        created_engines.append((url, kwargs))
        return FakeEngine()

    def fake_async_sessionmaker(engine: object, **kwargs) -> FakeSessionFactory:
        created_factories.append((engine, kwargs))
        return FakeSessionFactory()

    monkeypatch.setattr(database, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(database, "async_sessionmaker", fake_async_sessionmaker)

    database.reset_database_state()

    engine = database.get_engine()
    assert database.get_engine() is engine
    assert created_engines == [
        (
            "postgresql+asyncpg://postgres:postgres@localhost:5432/trip_planner",
            {
                "echo": False,
                "pool_size": 10,
                "max_overflow": 20,
                "pool_recycle": 3600,
            },
        )
    ]

    session_factory = database.get_session_factory()
    assert database.get_session_factory() is session_factory
    assert created_factories == [
        (
            engine,
            {
                "class_": AsyncSession,
                "expire_on_commit": False,
            },
        )
    ]

    database.reset_database_state()


def test_get_db_yields_async_session_and_commits(monkeypatch):
    from app import database

    async def consume_dependency() -> list[str]:
        events: list[str] = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                events.append("exit")

            async def commit(self):
                events.append("commit")

            async def rollback(self):
                events.append("rollback")

        def fake_factory() -> FakeSession:
            return FakeSession()

        monkeypatch.setattr(database, "get_session_factory", lambda: fake_factory)

        dependency: AsyncIterator[AsyncSession] = database.get_db()
        session = await anext(dependency)
        assert isinstance(session, FakeSession)

        with pytest.raises(StopAsyncIteration):
            await anext(dependency)

        return events

    assert asyncio.run(consume_dependency()) == ["commit", "exit"]


def test_db_models_metadata_matches_phase1_tables_and_schemas():
    from app.models import db_models

    tables = db_models.Base.metadata.tables

    assert {
        "users",
        "trip_plans",
        "trip_plan_versions",
        "conversation_messages",
        "user_preferences",
        "saved_items",
        "memory_entries",
        "audit.event_log",
        "audit.token_usage",
    }.issubset(tables.keys())

    trip_plans = tables["trip_plans"]
    status_constraints = " ".join(
        str(constraint.sqltext)
        for constraint in trip_plans.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    )
    for status in ("draft", "generating", "completed", "editing", "archived", "failed"):
        assert status in status_constraints
    assert "idx_trip_plans_session_id" in {index.name for index in trip_plans.indexes}

    versions = tables["trip_plan_versions"]
    assert any(
        {column.name for column in constraint.columns} == {"trip_plan_id", "version"}
        for constraint in versions.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )

    event_log = tables["audit.event_log"]
    assert event_log.schema == "audit"
    assert "idx_audit_event_type" in {index.name for index in event_log.indexes}

    tasks = tables["trip_plan_tasks"]
    for column in (
        "heartbeat_at",
        "lease_owner",
        "workflow_version",
        "state_schema_version",
        "recovery_state",
        "recovery_enqueued_at",
        "checkpoint_deleted_at",
    ):
        assert column in tasks.columns
    assert "idx_trip_plan_tasks_recovery" in {index.name for index in tasks.indexes}

    memory_entries = tables["memory_entries"]
    assert str(memory_entries.columns.embedding.type) == "VECTOR(1536)"
    assert "idx_memory_entries_embedding_hnsw" in {
        index.name for index in memory_entries.indexes
    }


def test_initial_alembic_migration_contains_required_schema_objects():
    migration_path = "alembic/versions/001_initial.py"

    with open(migration_path, encoding="utf-8") as migration_file:
        migration = migration_file.read()

    for expected in (
        "CREATE EXTENSION IF NOT EXISTS pgcrypto",
        "CREATE SCHEMA IF NOT EXISTS audit",
        '"users"',
        '"trip_plans"',
        '"event_log"',
        "schema=\"audit\"",
        "idx_token_usage_model",
    ):
        assert expected in migration


def test_pgvector_memory_migration_enables_extension_and_hnsw_index():
    migration_path = "alembic/versions/005_pgvector_long_term_memory.py"

    with open(migration_path, encoding="utf-8") as migration_file:
        migration = migration_file.read()

    for expected in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        '"memory_entries"',
        "VECTOR(1536)",
        "uq_memory_entries_user_source",
        "idx_memory_entries_embedding_hnsw",
        'postgresql_using="hnsw"',
        'postgresql_ops={"embedding": "vector_cosine_ops"}',
    ):
        assert expected in migration
