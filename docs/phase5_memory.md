# Phase 5: 记忆系统

> **所属项目**: 智能旅行助手 Harness 架构升级
> **依赖**: Phase 1（数据库表）、Phase 4（Session 和 API 上下文）
> **被依赖**: 无（最终消费方）

---

## 目标

建立分层记忆系统：短期记忆（会话上下文滑动窗口）、长期记忆（跨会话用户偏好增量学习）、记忆召回（新计划生成时自动注入相关历史信息）。

---

## 分层设计

```
┌──────────────────────────────────────────────┐
│              记忆系统                          │
│                                               │
│  短期记忆 (ShortTermMemory)                    │
│  ├── 存储: 内存 + conversation_messages 表     │
│  ├── 范围: 当前会话                           │
│  ├── 策略: 滑动窗口 (最近 20 条消息)            │
│  └── 用途: 多轮对话上下文                      │
│                                               │
│  长期记忆 (LongTermMemory)                     │
│  ├── 存储: user_preferences + saved_items 表   │
│  ├── 范围: 跨会话                             │
│  ├── 策略: 每次完成计划后增量更新               │
│  └── 用途: 偏好学习、收藏管理                   │
│                                               │
│  记忆召回 (MemoryRecall)                       │
│  ├── 新计划生成时召回相关历史偏好和收藏           │
│  └── 注入到编排器 context                      │
└──────────────────────────────────────────────┘
```

---

## 5.1 ShortTermMemory

```python
# backend/app/memory/short_term.py

from typing import Dict, List, Optional
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import ConversationMessage


class ShortTermMemory:
    """
    短期记忆：当前会话的对话上下文。
    - 内存中维护滑动窗口（最近 N 条消息）
    - 同时持久化到 conversation_messages 表
    """

    def __init__(self, session_id: str, max_messages: int = 20):
        self.session_id = session_id
        self.max_messages = max_messages
        self._buffer: List[Dict] = []  # [{role, content, metadata, ...}, ...]

    async def add(
        self,
        role: str,
        content: str,
        db: AsyncSession,
        tool_calls: Optional[List[Dict]] = None,
        tool_name: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        """添加一条消息到缓冲区并持久化"""
        entry = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_name": tool_name,
            "metadata": metadata or {},
            "timestamp": datetime.utcnow(),
        }
        self._buffer.append(entry)

        # 滑动窗口裁剪
        if len(self._buffer) > self.max_messages:
            self._buffer = self._buffer[-self.max_messages:]

        # 持久化
        db_msg = ConversationMessage(
            session_id=self.session_id,
            role=role,
            content=content,
            tool_calls_json=tool_calls,
            tool_name=tool_name,
            metadata_json=metadata or {},
        )
        db.add(db_msg)

    def get_context(self) -> List[Dict[str, str]]:
        """获取 LLM 格式的上下文消息列表"""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self._buffer
        ]

    def get_last_n(self, n: int) -> List[Dict]:
        """获取最近 n 条消息"""
        return self._buffer[-n:] if n < len(self._buffer) else self._buffer.copy()

    async def restore_from_db(self, db: AsyncSession) -> None:
        """从数据库恢复消息历史（服务重启后调用）"""
        result = await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == self.session_id)
            .order_by(ConversationMessage.created_at.asc())
            .limit(self.max_messages)
        )
        rows = result.scalars().all()
        self._buffer = [
            {
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls_json,
                "tool_name": r.tool_name,
                "metadata": r.metadata_json or {},
                "timestamp": r.created_at,
            }
            for r in rows
        ]

    def clear(self) -> None:
        """清空缓冲区（不删除 DB 记录）"""
        self._buffer.clear()
```

---

## 5.2 LongTermMemory

```python
# backend/app/memory/long_term.py

from typing import Dict, List, Optional
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import UserPreference, SavedItem


class LongTermMemory:
    """
    长期记忆：跨会话的用户偏好和收藏。
    每次完成一个旅行计划后，增量更新用户偏好画像。
    """

    def __init__(self, db_session_factory):
        self._db_factory = db_session_factory

    async def get_preferences(self, user_id: UUID, db: AsyncSession) -> UserPreference:
        """获取用户偏好，不存在则返回默认偏好"""
        result = await db.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        prefs = result.scalar_one_or_none()
        if prefs is None:
            prefs = UserPreference(user_id=user_id)
            db.add(prefs)
            await db.flush()
        return prefs

    async def update_from_trip(
        self,
        user_id: UUID,
        db: AsyncSession,
        city: str,
        preferences: List[str],
        budget_level: str,
        days: int,
        liked_attractions: Optional[List[str]] = None,
    ) -> None:
        """
        根据一个已完成行程更新用户偏好。
        调用时机：用户确认/保存行程计划后。
        """
        prefs = await self.get_preferences(user_id, db)

        # 更新偏好类别
        for cat in preferences:
            if cat and cat.strip():
                prefs.preferred_categories = list(set(prefs.preferred_categories or []) | {cat.strip()})

        # 更新预算画像
        profile = dict(prefs.budget_profile or {})
        profile[budget_level] = profile.get(budget_level, 0) + 1
        prefs.budget_profile = profile

        # 更新平均旅行天数
        if prefs.avg_trip_days:
            prefs.avg_trip_days = round((prefs.avg_trip_days + days) / 2, 1)
        else:
            prefs.avg_trip_days = float(days)

        # 更新常去城市
        cities = list(prefs.favorite_cities or [])
        if city not in cities:
            cities.append(city)
        prefs.favorite_cities = cities[:10]  # 最多保留10个城市

        await db.flush()

    async def get_saved_items(
        self, user_id: UUID, db: AsyncSession, item_type: Optional[str] = None
    ) -> List[SavedItem]:
        """获取用户收藏"""
        query = select(SavedItem).where(SavedItem.user_id == user_id)
        if item_type:
            query = query.where(SavedItem.item_type == item_type)
        query = query.order_by(SavedItem.created_at.desc()).limit(50)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def add_saved_item(
        self, user_id: UUID, db: AsyncSession,
        item_type: str, item_data: Dict, tags: Optional[List[str]] = None, note: Optional[str] = None,
    ) -> SavedItem:
        """添加收藏"""
        item = SavedItem(
            user_id=user_id,
            item_type=item_type,
            item_data=item_data,
            tags=tags or [],
            note=note,
        )
        db.add(item)
        await db.flush()
        return item
```

---

## 5.3 MemoryRecall

```python
# backend/app/memory/recall.py

from typing import Dict, List
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.memory.long_term import LongTermMemory


class MemoryRecall:
    """
    记忆召回：在生成新计划时，从长期记忆中召回相关信息注入上下文。
    """

    def __init__(self, long_term_memory: LongTermMemory):
        self._ltm = long_term_memory

    async def recall(
        self,
        user_id: UUID,
        db: AsyncSession,
        city: str,
        preferences: str,
    ) -> Dict:
        """
        召回与当前请求相关的历史信息。
        返回:
          {
            "user_preferences": {...},
            "saved_attractions_in_city": [...],
            "budget_profile": {...},
            "favorite_cities": [...],
            "avg_trip_days": 3.5,
          }
        """
        prefs = await self._ltm.get_preferences(user_id, db)

        # 召回同城历史收藏景点
        saved_items = await self._ltm.get_saved_items(user_id, db, item_type="attraction")
        saved_attractions = [
            item.item_data for item in saved_items
            if item.item_data.get("city", "") == city
        ]

        recall_context = {
            "preferred_categories": prefs.preferred_categories or [],
            "travel_style": prefs.travel_style or "",
            "budget_profile": prefs.budget_profile or {},
            "favorite_cities": prefs.favorite_cities or [],
            "saved_attractions_in_city": saved_attractions,
            "avg_trip_days": prefs.avg_trip_days,
        }

        return recall_context
```

---

## 5.4 记忆与编排器的集成

```python
# 在 TripPlannerAgent 或 API 路由中集成记忆召回

async def plan_trip_with_memory(
    request: TripPlanRequest,
    session_id: UUID,
    db: AsyncSession,
    orchestrator: AgentOrchestrator,
    memory_recall: MemoryRecall,
    short_term: ShortTermMemory,
) -> TripPlan:
    user_id = await resolve_user_id(session_id, db)

    # 1. 召回长期偏好
    recalled = await memory_recall.recall(user_id, db, request.city, request.preferences)

    # 2. 获取短期对话上下文
    conversation_context = short_term.get_context()

    # 3. 构建增强 context
    context = {
        "request": request,
        "user_preferences": recalled,
        "conversation_context": conversation_context,
    }

    # 4. 执行编排
    trace = await orchestrator.run(str(uuid.uuid4()), context)

    trip_plan = context.get("trip_planner")

    # 5. 更新长期记忆
    await memory_recall._ltm.update_from_trip(
        user_id=user_id,
        db=db,
        city=request.city,
        preferences=request.preferences.split(","),
        budget_level=request.budget,
        days=request.days,
    )

    return trip_plan
```

---

## 关键文件清单

| 文件 | 操作 |
|---|---|
| `backend/app/memory/__init__.py` | 新建 |
| `backend/app/memory/models.py` | 新建：Memory 数据模型 |
| `backend/app/memory/short_term.py` | 新建：ShortTermMemory |
| `backend/app/memory/long_term.py` | 新建：LongTermMemory |
| `backend/app/memory/recall.py` | 新建：MemoryRecall |
| `backend/app/api/routes/trip.py` | 修改：集成记忆召回到 plan 端点 |
| `backend/tests/test_memory.py` | 新建 |

---

## 验证方式

1. 短期记忆：创建会话 → 发送 3 条消息 → 验证 `get_context()` 返回 3 条 → 发送第 21 条 → 验证窗口保留最近 20 条
2. 长期记忆：用户 A 创建 3 个"历史文化"类北京计划 → 验证 `user_preferences.preferred_categories` 包含"历史文化"，`favorite_cities` 包含"北京"
3. 记忆召回：用户 A 再次创建北京计划 → 验证 `recall()` 返回的 `budget_profile` 反映了历史偏好（如"中等"计数最高）
4. 服务重启恢复：重启后端 → 调用 `restore_from_db()` → 验证对话历史正确加载
5. `pytest tests/test_memory.py -v`
