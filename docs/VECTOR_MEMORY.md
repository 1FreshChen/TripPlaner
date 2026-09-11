# pgvector 长期记忆

## 目标

长期记忆同时保留两类信息：

- `user_preferences` 与 `saved_items` 保存可直接计算和展示的结构化数据；
- `memory_entries` 保存行程历史和收藏项的文本、元数据及 1536 维向量，用于跨会话语义召回。

向量查询始终带 `user_id` 条件，不会跨用户召回。用户身份仍由持久化的会话 ID 映射；如果客户端丢失会话 ID，会被视为新用户。

## 数据流

1. 新行程开始时，将“目的地 + 当前偏好”转换为查询向量。
2. PostgreSQL 使用 pgvector 余弦距离和 HNSW 索引，从当前用户的记忆中返回最相关的若干条。
3. 召回结果与结构化偏好、同城收藏、短期会话一起进入 Planner prompt。
4. 行程成功发布后，以 `trip:{plan_id}` 为幂等来源键写入一条行程记忆。
5. 新增收藏时写入 `saved_item:{item_id}`；删除收藏时同步删除对应向量记忆。

每个用户默认最多保留 1000 条向量记忆，写入时清理超限的最旧记录。重复完成同一计划会更新同一来源键，不会制造重复记录。

## 配置

```dotenv
VECTOR_MEMORY_ENABLED=true
EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_TIMEOUT_SECONDS=20
VECTOR_MEMORY_TOP_K=5
VECTOR_MEMORY_MIN_SIMILARITY=0.3
VECTOR_MEMORY_MAX_ENTRIES_PER_USER=1000
```

`EMBEDDING_API_KEY` 或 `EMBEDDING_BASE_URL` 留空时分别复用 `LLM_API_KEY`、`LLM_BASE_URL`。嵌入端点必须兼容 OpenAI Embeddings API，并支持返回 1536 维向量；当前实现会对 `text-embedding-3-*` 请求固定的 `dimensions=1536`。

没有可用 API Key 时向量记忆自动停用，结构化长期记忆仍正常工作。若自建 LLM 端点不支持 embeddings，应配置独立的 `EMBEDDING_*`，或显式设置 `VECTOR_MEMORY_ENABLED=false`，避免每次请求等待失败超时。

## 数据库升级

Compose 已从标准 PostgreSQL 镜像切换到固定版本的 `pgvector/pgvector:0.8.6-pg16-bookworm`，现有 PostgreSQL 16 数据卷可继续使用。部署应用前执行：

```bash
cd backend
alembic upgrade head
```

迁移 `005_pgvector_long_term_memory` 会启用 `vector` 扩展、创建 `memory_entries`，并建立：

- `(user_id, source_key)` 唯一约束；
- 用户/类型与用户/时间普通索引；
- 使用 `vector_cosine_ops` 的 HNSW 向量索引。

如果使用外部托管 PostgreSQL，数据库账号必须有权执行 `CREATE EXTENSION vector`；否则需由管理员预先启用扩展。

## 故障边界

向量写入、查询和删除都运行在嵌套事务（savepoint）中。嵌入服务超时、扩展未安装、迁移未执行或单次向量 SQL 失败时，会记录 warning 并退回结构化记忆，不会回滚已经生成的行程或收藏操作。

生产监控应关注日志中的：

- `vector memory write skipped`
- `vector memory search skipped`
- `vector memory delete skipped`

长期出现这些日志通常意味着嵌入 Key/端点错误、模型维度不兼容或 migration `005` 尚未执行。

## 数据与隐私

`memory_entries` 包含用户的行程偏好、收藏名称、标签和备注。备份、访问控制和删除策略应与 `user_preferences`、`saved_items` 使用同一安全等级。删除用户时，外键 `ON DELETE CASCADE` 会一并删除其向量记忆。
