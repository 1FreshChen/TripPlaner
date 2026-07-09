# Phase 3: 创建 LLM 驱动的 PlannerAgent

## 改进的目的

当前 `PlannerAgent`（`backend/app/agents/trip_planner.py` 第 228-320 行）的工作方式是：

1. Stage 0 的三个 Agent（景点搜索、天气查询、酒店推荐）预先收集数据
2. 将所有数据打包成一个大文本 prompt
3. 调用 `LLMService.generate_json()` —— **单次同步调用**
4. LLM 无法主动搜索更多数据、无法用不同关键词重新搜索、无法验证已有数据

这导致：
- 同一个城市+偏好的查询总是得到几乎相同的结果（因为输入的 POI 数据一样）
- LLM 无法通过多次搜索来增加多样性
- 5 个已实现的 Tool（AmapPOISearch、AmapWeather、HotelSearch、BudgetCalculator、UnsplashImage）在规划阶段形同虚设

## 改进的内容

### 核心架构变化

```
旧流程:
  Stage 0: [AttractionSearchAgent, WeatherQueryAgent, HotelAgent] (并行)
       ↓
  Stage 1: PlannerAgent.generate_json(大文本prompt) → TripPlan

新流程:
  Stage 0: [AttractionSearchAgent, WeatherQueryAgent, HotelAgent] (并行, 保留)
       ↓
  Stage 1: LLMPlannerAgent.chat_with_tools()
       ├─ 接收 baseline 数据作为上下文
       ├─ LLM 自主决定搜索策略
       │   ├─ "amap_poi_search(keywords='博物馆')"  → 获取结果
       │   ├─ "amap_poi_search(keywords='文创园区')" → 换个角度搜索
       │   ├─ "amap_weather(city='北京')"           → 验证天气
       │   ├─ "hotel_search(city='北京', hotel_type='精品酒店')"
       │   ├─ "budget_calculator(...)"              → 计算预算
       │   └─ "unsplash_image_search(query='故宫')"  → 搜索图片
       ├─ 多轮工具调用后，LLM 综合所有数据
       └─ 输出最终 TripPlan JSON
```

### 关键设计决策：保留 Stage 0 的三个 Agent

Stage 0 的数据收集 Agent **继续保留**，原因是：

1. **提供 baseline 数据**：LLM 从上下文可以快速了解城市有哪些主要景点，不需要从零搜索
2. **LLM 不可用时的 fallback**：如果 LLM 挂了，Stage 0 的数据 + 确定性算法仍然能产出可用的行程
3. **减少 LLM 工具调用轮次**：baseline 数据减少了 LLM "冷启动"的搜索开销

LLM 被告知：

> 以下是你可参考的 baseline 数据。你可以用工具搜索更多选择。为了增加多样性，建议替换其中 40%-60% 的景点。

### 新建文件

`backend/app/agents/llm_planner.py` —— `LLMPlannerAgent` 类，约 200-300 行

```python
class LLMPlannerAgent(BaseAgent):
    """使用 LLM + Tools 的智能行程规划 Agent"""

    def __init__(
        self,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        enable_critique: bool = True,   # Phase 4
        max_refinement_rounds: int = 3,  # Phase 4
    ):
        ...

    async def execute(self, context: dict) -> TripPlan:
        request = context["request"]
        memory_context = context.get("memory_context", {})
        conversation_context = context.get("conversation_context", [])

        # 构建 prompt
        system_prompt = PLANNER_AGENT_PROMPT  # 使用 Phase 1 的新 prompt
        user_prompt = self._build_user_prompt(
            request, memory_context, conversation_context
        )

        # 构建 tools（OpenAI function-calling 格式）
        tools = self.tool_registry.get_openai_functions()

        # 多轮工具调用 + 规划
        final_text, tool_calls, usage = await self.llm_service.chat_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            tool_executor=self.tool_executor,
            max_tool_rounds=5,
        )

        # 解析 + 验证
        trip_plan = self._parse_and_validate(final_text)
        return trip_plan
```

### 修改的文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/agents/trip_planner.py` | 修改 `build_planner_query()`：加入去重上下文、多样性指令。修改 `TripPlannerAgent.__init__()`：支持注入 ToolRegistry 和 ToolExecutor。保留旧 `PlannerAgent` 不动 |
| `backend/app/orchestration/bootstrap.py` | 注册 `LLMPlannerAgent`（根据配置开关选择新旧 Agent）。更新 fallback chain：旧 PlannerAgent 作为 LLMPlannerAgent 的 fallback |
| `backend/app/services/state_service.py` | 在 create_trip_plan 中传递 tool_registry 和 tool_executor 给 TripPlannerAgent |
| `backend/app/config.py` | 新增配置：`ENABLE_LLM_TOOL_PLANNING: bool = True` |

### 输出验证 + 修正

```python
def _parse_and_validate(self, text: str) -> TripPlan:
    try:
        payload = json.loads(text)
        return TripPlan.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as e:
        # 将错误信息反馈给 LLM，请求修正
        correction_prompt = f"你的输出有格式错误：{e}。请修正后重新输出完整的 JSON。"
        corrected_text, _, _ = await self.llm_service.chat_with_tools(
            system_prompt=PLANNER_AGENT_PROMPT,
            user_prompt=correction_prompt,
            tools=[],  # 修正时不需要工具
            tool_executor=self.tool_executor,
            max_tool_rounds=1,
        )
        payload = json.loads(corrected_text)
        return TripPlan.model_validate(payload)
```

## 改进的方法

### 实施步骤

1. **创建 `llm_planner.py`**：实现 `LLMPlannerAgent` 类
2. **修改 `trip_planner.py`**：
   - 修改 `build_planner_query()`，加入去重上下文和多样性指令
   - 修改 `TripPlannerAgent.__init__()`，接受 tool_registry 和 tool_executor
3. **修改 `bootstrap.py`**：
   - 注册 `LLMPlannerAgent`，设置 `depends_on` 同原 PlannerAgent
   - 将原 `PlannerAgent(use_llm=False)` 注册为 DETERMINISTIC 级 fallback
4. **修改 `state_service.py`**：传递 tool 相关依赖
5. **添加配置开关**：`ENABLE_LLM_TOOL_PLANNING`

### 验证方式

1. 发送"北京 3 天 历史文化"请求
2. 检查 token_usage 审计表：确认有工具调用记录（tool_calls 轮次 > 0）
3. 检查 LLM 是否使用了不同的搜索关键词（如 "博物馆" → "艺术区" → "胡同"）
4. 对比开关前后的输出质量
5. 确认 Pydantic 验证通过
6. 关闭开关后，确认旧的 PlannerAgent 仍然正常工作

### 影响范围

- 新建 1 个文件
- 修改 4 个文件
- 核心架构变更，但通过开关和 fallback 机制保证安全
