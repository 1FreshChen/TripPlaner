# Phase 4: 多轮自我审视循环

## 改进的目的

当前行程生成是"一次成型"的——PlannerAgent 调用一次 LLM，拿到 JSON，验证 schema 通过就返回。这个过程没有任何质量检查机制。常见的质量问题无法被自动发现：

- **景点重复**：故宫在 Day 1 和 Day 3 都出现了
- **天气不匹配**：暴雨天安排了颐和园户外游览
- **行程不可行**：3 个景点之间的交通时间加起来要 4 小时，但总游览时间只有 8 小时
- **描述偷懒**：LLM 在某些景点上偷懒写了泛泛的描述
- **预算不合理**：每天餐饮费波动在 30-500 元之间

人类旅行规划师会反复检查和调整——Agent 也应该有类似的能力。

## 改进的内容

### 新增 PlanCritic 审视 Agent

创建一个专门评估行程质量的 Agent，从多个维度打分：

```json
{
  "scores": {
    "attraction_diversity": 7,      // 0-10，景点类型是否多样
    "description_quality": 6,       // 0-10，描述是否具体有参考价值
    "weather_compatibility": 9,     // 0-10，天气与景点类型是否匹配
    "schedule_feasibility": 5,      // 0-10，每日行程时间是否合理
    "budget_realism": 7             // 0-10，预算是否合理
  },
  "issues": [
    {
      "severity": "high",
      "day": 2,
      "problem": "故宫在 Day 1 和 Day 3 重复出现",
      "suggestion": "Day 3 替换为景山公园或北海公园"
    },
    {
      "severity": "medium",
      "day": 1,
      "problem": "3个景点间交通预估超过3小时，日程过满",
      "suggestion": "减少1个景点，或将距离较近的景点分组"
    },
    {
      "severity": "low",
      "day": 2,
      "problem": "午餐描述为'享用当地美食'，过于泛泛",
      "suggestion": "推荐1-2个该区域的具体餐厅或菜品"
    }
  ],
  "suggestions": [
    "Day 3 可以增加一个文创园区的行程以增加多样性",
    "建议将 Day 1 的上午行程调整为 8:30 开始，避开故宫排队高峰"
  ],
  "needs_revision": true,
  "revision_summary": "存在2个高优先级问题和1个中优先级问题，建议修正后重新生成"
}
```

### 审视 → 修正循环

```
LLMPlannerAgent.execute()
  │
  ├─ 第 1 轮：生成初始行程
  │
  ├─ PlanCritic.evaluate(plan, request) → 评分 + 问题列表
  │
  ├─ 如果 needs_revision == true 且未超过 max_rounds:
  │   │
  │   ├─ 将审视结果注入 LLM 上下文：
  │   │   "上一版行程存在以下问题，请修正:
  │   │    1. 故宫重复出现
  │   │    2. Day 1 行程过满
  │   │    3. ..."
  │   │
  │   ├─ LLM 重新生成（可以再次使用工具搜索替代景点）
  │   │
  │   └─ PlanCritic.evaluate(new_plan, request)
  │
  └─ 返回最佳版本（取评分最高的那版）
```

### 配置

- `max_refinement_rounds: int = 3` —— 最多修正 3 轮
- `min_pass_score: float = 7.0` —— 平均分达到 7 分以上可提前结束
- `ENABLE_PLAN_CRITIQUE: bool = True` —— 环境变量开关

### 审视维度详解

| 维度 | 检查内容 | 扣分条件 |
|------|---------|---------|
| `attraction_diversity` | 同类景点占比、跨天重复 | 同类 > 50% 或 有重复景点 |
| `description_quality` | 描述长度、具体性、实用 tips | 描述 < 80 字或含模板文本 |
| `weather_compatibility` | 天气与实际活动类型的匹配 | 雨天安排户外或高温天中午户外 |
| `schedule_feasibility` | 游览时间 + 交通时间是否合理 | 总时间 > 10h 或包含不合理的路线 |
| `budget_realism` | 预算计算是否正确、分配是否合理 | 总预算明显偏离请求或每天波动过大 |

## 改进的方法

### 新建文件

`backend/app/agents/critic.py` —— `PlanCritic` 类

```python
class PlanCritic:
    """行程质量审视 Agent"""

    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    async def evaluate(
        self,
        plan: TripPlan,
        request: TripPlanRequest
    ) -> CritiqueResult:
        """对行程进行多维度评估"""
        prompt = self._build_critique_prompt(plan, request)
        result = await self.llm_service.generate_json(
            system_prompt=CRITIC_AGENT_PROMPT,
            user_prompt=prompt
        )
        return CritiqueResult.model_validate(result)

    def _build_critique_prompt(self, plan, request) -> str:
        """构建评估 prompt：将行程 JSON + 用户需求传给 Critic"""
        ...
```

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/agents/prompts.py` | 新增 `CRITIC_AGENT_PROMPT` |
| `backend/app/agents/llm_planner.py` | 集成审视循环到 `execute()` 方法 |
| `backend/app/models/schemas.py` | 新增 `CritiqueResult` Pydantic 模型 |
| `backend/app/config.py` | 新增 `ENABLE_PLAN_CRITIQUE` 配置项 |

### 实施步骤

1. 在 `prompts.py` 中编写 `CRITIC_AGENT_PROMPT`
2. 在 `schemas.py` 中定义 `CritiqueScores`、`CritiqueIssue`、`CritiqueResult` 模型
3. 创建 `critic.py`，实现 `PlanCritic` 类
4. 在 `llm_planner.py` 的 `execute()` 中实现审视循环
5. 添加审计事件记录：每次审视的分数保存到 `event_log`

### 验证方式

1. 生成行程后检查审计日志中的审视分数
2. 人为制造质量问题（如重复景点），确认 Critic 能检测到
3. 确认 `max_refinement_rounds` 限制生效
4. 对比开启/关闭审视时的输出质量
5. 检查审视对 token 消耗的影响是否在可接受范围内

### 成本考量

审视循环每轮调用一次 LLM（无工具），3 轮最多增加 3 次额外调用。对 token 消耗的影响约为基准的 50-80%。通过配置开关，用户可以在成本和质量的权衡中选择。
