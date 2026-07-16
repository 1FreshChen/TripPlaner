# 对话调整功能：端到端实现计划

## Context

当前"对话调整"功能存在断裂：用户在对话中说"把第二天的故宫换成天坛"，LLM 只会返回文字建议但**不会真正修改行程计划**。`ConversationResponse.updated_plan` 字段在 Schema 中存在但始终为 `None`，前端也完全忽略该字段。

目标：让对话调整形成闭环——用户用自然语言提出修改 → LLM 返回修改后的完整行程 → 自动写入数据库（带版本管理） → 前端无刷新更新展示。

---

## 总体架构：混合意图识别 + 两阶段 LLM 调用

```
用户消息 → [规则三态判断]
              ├─ 明确修改 → 生成可执行修改指令
              ├─ 明确咨询 → 不修改
              └─ 无法确定 → LLM 结合最近对话判断并还原完整修改指令
         → [Phase 1] LLM 自由对话（支持工具调用） → 文字回复
         → 修改意图为真？
              ├─ 否 → 返回纯文字
              └─ 是 → [Phase 2] LLM 修改完整 TripPlan JSON → 校验 → 写入DB → 返回 updated_plan
```

规则只快速处理明确表达；自然偏好、指代和“可以/就按刚才说的办”等确认语由 LLM 结合最近六条消息处理。Phase 1 与 Phase 2 分离，是因为前者需要工具调用，后者需要稳定的结构化 JSON。

---

## 后端改动

### 1. 意图检测 — `state_service.py` 新增方法

在 `StateService` 中实现三层判断：
- `_classify_modification_intent_by_rules()` 返回 `True / False / None`，快速识别明确修改和明确咨询
- `_resolve_modification_intent()` 只在规则返回 `None` 且前端启用 `apply_to_plan` 时调用 LLM
- LLM 输出 `should_modify / instruction / reason`，其中 `instruction` 必须是脱离上下文也能执行的完整指令
- `apply_to_plan` 表示允许智能修改关联行程，不再表示每一条消息都必须修改

### 2. 计划修改方法 — `state_service.py` 新增 `_modify_plan_via_conversation()`

核心流程：
```
a. 序列化当前行程为 JSON 字符串
b. 构建 Modification System Prompt（约束：只改用户要求的部分，保持其他不变）
c. 调用 llm.chat_with_tools(tools=[], max_tool_rounds=1)
   → 不传 response_format（DeepSeek 兼容），靠 prompt 要求纯 JSON 输出
d. 解析：_strip_json_fence() → json.loads() → TripPlan.model_validate()
e. 无效 JSON、`no_change` 或与原计划完全相同都会 retry 一次
f. 成功后：复用 update_trip_plan 模式写入 DB
   - version += 1
   - change_type = "agent_regenerate"
   - change_summary = 解析后的可执行修改指令（截断200字）
   - 记录 AuditEvent + TokenUsage
g. 成功返回 TripPlanResponse；确认需要修改但两次失败时抛出 PlanModificationError
```

### 3. 修改 `send_conversation_message()` — 串联 Phase 1 和 Phase 2

在加载最近对话后先完成意图判断，再执行自由对话和计划修改：

```python
intent = await _resolve_modification_intent(...)
updated_plan = None
if referenced_plan_id and intent.should_modify:
    try:
        updated_plan = await self._modify_plan_via_conversation(
            modification_instruction=intent.instruction,
            ...,
        )
        if updated_plan:
            content = f"✅ 行程已更新(版本{updated_plan.version})\n\n{content}"
    except Exception:
        plan_update_failed = True
        content += "本次没有成功保存修改，当前行程未变化"

return ConversationResponse(..., updated_plan=updated_plan, plan_update_failed=plan_update_failed)
```

### 4. 复用 JSON 清理工具

统一复用 `app.utils.json_utils.strip_json_fence()`，避免在 Planner 和 StateService 中保留重复实现。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `backend/app/services/state_service.py` | 三态规则、LLM 意图兜底、上下文指令还原、修改执行和审计 |
| `backend/app/models/schemas.py` | 请求增加 `apply_to_plan`，响应增加明确的失败标志 |

---

## 前端改动

### 5. `tripPlanStore` 新增 `applyExternalUpdate` 动作

```typescript
function applyExternalUpdate(updatedPlan: TripPlanResponse) {
  currentPlan.value = updatedPlan
  planId.value = updatedPlan.plan_id
  planStatus.value = updatedPlan.status
  versions.value.unshift({...})  // 追加到版本历史
  originalPlan.value = null
}
```

### 6. `sessionStore.send()` 消费 `reply.updated_plan`

在 `send()` 返回前增加：
```typescript
if (reply.updated_plan) {
  useTripPlanStore().applyExternalUpdate(reply.updated_plan)
}
```

同时在 assistant 消息上附加 `_planUpdated: true` 标记，供 UI 渲染。

### 7. `Conversation.vue` 添加视觉反馈

- `handleSend()` 中检查 `reply.updated_plan`，弹出 `message.success("行程已更新")`
- 消息列表中，标记了 `_planUpdated` 的助手消息旁显示绿色 Tag "行程已更新"

### 8. `types/index.ts` 微调

`ConversationMessage` 接口增加可选字段 `_planUpdated?: boolean`。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `frontend/src/stores/tripPlanStore.ts` | 新增 `applyExternalUpdate` |
| `frontend/src/stores/sessionStore.ts` | `send()` 中处理 `updated_plan` |
| `frontend/src/views/Conversation.vue` | 成功通知 + 消息标记 |
| `frontend/src/types/index.ts` | `ConversationMessage._planUpdated` 字段 |

---

## 错误处理策略

| 场景 | 处理 |
|------|------|
| 意图分类无法解析 | 保守地不修改，记录 `llm_error` 来源、日志和审计详情 |
| LLM Phase 2 返回无效 JSON（两次都失败） | 记录警告和 AuditEvent，返回 `plan_update_failed=true` 并明确告知未保存 |
| 已确认修改却返回 `no_change` 或相同 JSON | 作为失败自动重试，不能静默伪装成成功 |
| 用户在已更新后再次说“可以” | 分类器根据“行程已更新”上下文判断，不重复生成版本 |
| 连续快速发两条修改消息 | 第二条基于第一条的结果（DB 已 flush），版本号正确递增 |
| 引用的行程已删除/归档 | `referenced_plan` 为 None，不进入 Phase 2 |
| DeepSeek 不支持 `response_format` | Phase 2 不使用该参数，仅靠 prompt 要求纯 JSON + `_strip_json_fence` 清理 |

---

## 验证方案

1. **单元测试**（`backend/tests/` 新增）：
   - `test_detect_modification_intent`：验证明确修改、明确咨询和不确定三态结果
   - `test_resolve_modification_intent`：验证上下文确认语由 LLM 还原为完整指令
   - `test_modify_success`：Mock LLM 返回有效 JSON，验证版本递增、change_type、audit 事件
   - `test_modify_retry`：验证无效 JSON、`no_change` 和相同 JSON 都会重试
   - `test_modify_both_fail`：Mock 两次无效 JSON，验证失败标志且不写入 DB

2. **端到端测试**：
   - 生成一个行程 → 进入对话 → 输入"把第二天的故宫换成天坛"
   - 验证：助手回复含"行程已更新" + `updated_plan` 非空
   - 回到 Result 页面：行程内容已变为天坛，版本号 +1
   - 查看版本历史：出现 `change_type=agent_regenerate` 的版本记录

3. **回归测试**：确保非修改类对话（如"天坛好玩吗"）不触发 Phase 2，行为不变
