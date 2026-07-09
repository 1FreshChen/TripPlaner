# Phase 5: 去重与记忆驱动的个性化

## 改进的目的

当前系统的用户记忆（`LongTermMemory` + `MemoryRecall`）收集了丰富的用户偏好数据：

- `preferred_categories`：用户偏好的景点类别
- `budget_profile`：历史预算水平分布
- `travel_style`：旅行风格（深度游 / 轻松游 / 打卡游）
- `favorite_cities`：最常去的城市
- `avg_trip_days`：平均旅行天数
- `saved_attractions_in_city`：收藏的景点

但实际使用时，仅有 `preferred_categories` 被用于一个简单的关键词匹配（`_keyword_from_preferences()` 方法，将"历史文化"映射为"博物馆 历史 景点"）。其余数据全部**浪费**。

更严重的是，系统完全没有去重机制——同一用户查询同一城市 3 次，每次得到的都是几乎相同的 Top-N 景点列表。这对用户来说毫无价值。

## 改进的内容

### 1. 去重服务（DeduplicationService）

**新建文件**：`backend/app/services/deduplication.py`

核心功能：

```python
class DeduplicationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_previously_suggested(
        self, user_id: str, city: str
    ) -> PreviouslySuggested:
        """
        查询用户在同一城市的所有历史行程中已出现的景点/酒店/餐厅
        
        从 trip_plans 表的 plan_json JSONB 字段中提取：
        - attractions: List[str]  # 所有景点名称
        - hotels: List[str]       # 所有酒店名称
        - restaurants: List[str]  # 所有餐厅名称
        - categories: Dict[str, int]  # 各类别出现次数
        """
        ...

    async def get_diversity_boost(
        self, user_id: str, city: str
    ) -> DiversityGuidance:
        """
        分析历史数据的类别分布，生成多样性引导：
        
        - underexplored_categories: List[str]
          如果"博物馆"被推荐了 8 次但"艺术区"仅 1 次
          → 返回 ["艺术区", "文创园区", "现代建筑"]
        
        - search_keywords: List[str]
          具体的搜索关键词建议
          → ["美术馆", "798艺术区", "创意园区", "当代建筑"]
        """
        ...

    async def record_suggestions(
        self, plan_id: str, plan: TripPlan
    ) -> None:
        """行程创建成功后，记录已推荐的景点/酒店/餐厅"""
        ...
```

### 2. 增强 MemoryRecall

**修改文件**：`backend/app/memory/recall.py`

在 `recall()` 返回的 `memory_context` 中新增字段：

```python
{
    # 原有字段
    "preferred_categories": [...],
    "travel_style": "深度游",
    "budget_profile": {...},
    "favorite_cities": [...],
    "avg_trip_days": 3.5,
    "saved_attractions_in_city": [...],

    # 新增字段
    "previously_suggested": {
        "attractions": ["故宫", "天坛", "颐和园", ...],  # 已推荐过的景点
        "hotels": ["如家酒店(天安门店)", ...],
        "restaurants": ["全聚德(前门店)", ...],
    },
    "diversity_guidance": {
        "underexplored_categories": ["艺术区", "自然风光"],
        "suggested_search_keywords": ["美术馆", "文创园", "公园", "登山"],
    },
    "personalization": {
        "max_attractions_per_day": 2,         # 轻松游：2个；深度游：4个
        "preferred_start_time": "09:30",      # 轻松游推迟出发
        "crowd_avoidance_tips": True,          # 是否提供错峰建议
        "max_walking_minutes": 120,            # 步行容忍度转换
    }
}
```

### 3. 个性化规则

| 用户偏好 | 对规划的影响 |
|---------|------------|
| `travel_style: "轻松型"` | 每天 ≤ 2 个主要景点；出发时间不早于 9:30；安排更多休息/咖啡点 |
| `travel_style: "深度游"` | 每天 ≤ 3 个景点但每个景点游览时间 ≥ 3h；更详细的文化背景描述 |
| `travel_style: "打卡型"` | 每天可安排 4-5 个景点；优先安排地标性景点 |
| `crowd_avoidance: True` | 每个景点附带错峰时间建议；优先推荐小众景点 |
| `walking_tolerance: "较少"` | 相邻景点步行距离 ≤ 1km；安排更多公共交通/打车 |
| `budget_profile: {"经济": 8, "舒适": 2}` | 默认选经济型酒店和性价比餐厅 |

### 4. 融入规划 Prompt

在 `build_planner_query()` 中生成专门的"个性化提醒"段落：

```
## 个性化提醒

- 您的旅行风格是「深度游」，每天安排不超过 3 个景点，请为每个景点留出充足的探索时间
- 您偏好避开人群，请为热门景点推荐错峰游览时间
- 您此前在北京已游览过以下景点，请避免重复推荐：
  - 故宫博物院
  - 天坛公园
  - 颐和园
  - 八达岭长城
- 您较少探索以下类型，建议优先考虑：
  - 艺术区（如 798、草场地）
  - 自然风光（如 香山、植物园）
  - 城市公园（如 奥林匹克公园）
```

## 改进的方法

### 新建文件

`backend/app/services/deduplication.py` —— `DeduplicationService` 类

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/memory/recall.py` | `recall()` 方法新增去重和多样性数据 |
| `backend/app/memory/long_term.py` | 新增方法支持 `recall()` 的数据需求 |
| `backend/app/agents/trip_planner.py` | `build_planner_query()` 新增"个性化提醒"段落 |
| `backend/app/services/state_service.py` | 在 `create_trip_plan()` 中集成 `DeduplicationService` |
| `backend/app/models/schemas.py` | 新增 `PreviouslySuggested`、`DiversityGuidance` 模型 |

### 实施步骤

1. **创建模型**：在 `schemas.py` 中定义新数据结构
2. **实现去重查询**：通过 SQLAlchemy 查询 `trip_plans` 表的 JSONB 字段
3. **增强 MemoryRecall**：集成去重和多样性数据
4. **增强 prompt 构建**：在 `build_planner_query()` 中生成个性化提醒
5. **记录已推荐**：行程创建成功后写入记录

### JSONB 查询性能说明

查询"同一用户+同一城市的所有历史景点"通过以下方式高效实现：

```sql
-- 利用已有索引 idx_trip_plans_user_id 和 idx_trip_plans_city
SELECT plan_json
FROM trip_plans
WHERE user_id = $1
  AND city = $2
  AND status != 'archived'
```

数据量按每个用户每个城市 10 个以内的行程估算，JSONB 提取在应用层完成，无需额外的 GIN 索引。

### 验证方式

1. 创建用户 A 的"北京 3 天 历史文化"行程 → 记录景点列表
2. 再次创建用户 A 的"北京 3 天 历史文化"行程 → 验证去重率 ≥ 40%
3. 第三次创建 → 验证去重率 ≥ 60%
4. 切换到上海 → 验证不产生无关联的去重
5. 创建用户 B 的"北京 3 天 历史文化"行程 → 验证不与用户 A 的数据去重
