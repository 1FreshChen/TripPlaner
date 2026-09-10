import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.config import VECTOR_MEMORY_DIMENSIONS, Settings
from app.memory.embeddings import OpenAIEmbeddingService, build_embedding_service
from app.memory.vector_store import VectorMemoryStore, build_vector_memory_store
from app.models.db_models import MemoryEntry


def run(coro):
    return asyncio.run(coro)


class NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ResultRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeEmbeddingService:
    model = "test-embedding"

    def __init__(self):
        self.inputs = []

    async def embed(self, text):
        self.inputs.append(text)
        return [0.01] * VECTOR_MEMORY_DIMENSIONS


def test_openai_embedding_service_requests_fixed_dimensions():
    captured = {}

    class FakeEmbeddings:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.25] * VECTOR_MEMORY_DIMENSIONS)]
            )

    service = OpenAIEmbeddingService(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="text-embedding-3-small",
        timeout_seconds=10,
        client=SimpleNamespace(embeddings=FakeEmbeddings()),
    )

    vector = run(service.embed("  北京   历史文化  "))

    assert len(vector) == VECTOR_MEMORY_DIMENSIONS
    assert captured == {
        "model": "text-embedding-3-small",
        "input": "北京 历史文化",
        "dimensions": VECTOR_MEMORY_DIMENSIONS,
    }


def test_embedding_service_is_disabled_without_a_key():
    settings = Settings(_env_file=None, LLM_API_KEY="", EMBEDDING_API_KEY="")

    assert build_embedding_service(settings) is None
    assert build_vector_memory_store(settings) is None


def test_vector_store_upserts_trip_memory_and_prunes_old_entries():
    embeddings = FakeEmbeddingService()

    class FakeDB:
        def __init__(self):
            self.statements = []

        def begin_nested(self):
            return NestedTransaction()

        async def execute(self, statement):
            self.statements.append(statement)
            return ResultRows([])

    db = FakeDB()
    store = VectorMemoryStore(embeddings, max_entries_per_user=10)
    source_id = uuid.uuid4()

    stored = run(
        store.remember_trip(
            user_id=uuid.uuid4(),
            db=db,
            city="北京",
            preferences=["历史文化", "博物馆"],
            budget_level="中等",
            days=3,
            source_id=source_id,
        )
    )

    assert stored is True
    assert embeddings.inputs == ["用户规划了北京3日行程；偏好：历史文化、博物馆；预算：中等。"]
    assert len(db.statements) == 2
    insert_sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_memory_entries_user_source DO UPDATE" in insert_sql
    assert "memory_entries" in insert_sql
    delete_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
    assert "DELETE FROM memory_entries" in delete_sql
    assert "OFFSET" in delete_sql


def test_vector_store_search_is_user_scoped_and_returns_similarity():
    embeddings = FakeEmbeddingService()
    user_id = uuid.uuid4()
    entry = MemoryEntry(
        id=uuid.uuid4(),
        user_id=user_id,
        source_key="trip:source",
        memory_type="trip",
        content="用户喜欢历史文化和慢节奏旅行。",
        metadata_json={"city": "西安"},
        embedding=[0.01] * VECTOR_MEMORY_DIMENSIONS,
        embedding_model=embeddings.model,
        access_count=0,
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    class FakeDB:
        def __init__(self):
            self.statement = None

        def begin_nested(self):
            return NestedTransaction()

        async def execute(self, statement):
            self.statement = statement
            return ResultRows([(entry, 0.2)])

    db = FakeDB()
    store = VectorMemoryStore(embeddings, top_k=3, min_similarity=0.3)

    results = run(store.search(user_id=user_id, db=db, query="北京 历史文化"))

    assert results[0]["content"] == entry.content
    assert results[0]["similarity"] == 0.8
    assert results[0]["metadata"] == {"city": "西安"}
    assert entry.access_count == 1
    assert entry.last_accessed_at is not None
    statement_sql = str(db.statement.compile(dialect=postgresql.dialect()))
    assert "memory_entries.user_id =" in statement_sql
    assert "ORDER BY memory_entries.embedding <=>" in statement_sql


def test_vector_store_failure_does_not_escape_to_trip_workflow():
    class FailingEmbeddings:
        model = "broken"

        async def embed(self, text):
            raise RuntimeError("embedding endpoint unavailable")

    store = VectorMemoryStore(FailingEmbeddings())
    stored = run(
        store.remember_trip(
            user_id=uuid.uuid4(),
            db=SimpleNamespace(),
            city="上海",
            preferences=["城市漫步"],
            budget_level="舒适",
            days=2,
        )
    )

    assert stored is False
