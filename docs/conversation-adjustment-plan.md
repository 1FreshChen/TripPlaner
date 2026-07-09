# 对话调整功能：端到端实现计划

## Context

当前"对话调整"功能存在断裂：用户在对话中说"把第二天的故宫换成天坛"，LLM 只会返回文字建议但**不会真正修改行程计划**。`ConversationResponse.updated_plan` 字段在 Schema 中存在但始终为 `None`，前端也完全忽略该字段。

目标：让对话调整形成闭环——用户用自然语言提出修改 → LLM 返回修改后的完整行程 → 自动写入数据库（带版本管理） → 前端无刷新更新展示。

---

## 总体架构：两阶段 LLM 调用

```
用户消息 → [Phase 1] LLM 自由对话（支持工具调用） → 文字回复
         → [意图检测] 是否在要求修改行程？
              ├─ 否 → 返回纯文字（现有行为，零开销）
              └─ 是 → [Phase 2] LLM 结构化输出（无工具） → 解析验证 → 写入DB → 返回 updated_plan
```

两阶段分离的原因：Phase 1 需要工具调用（如"查一下天坛门票"），Phase 2 需要结构化 JSON 输出（与 tool calling 互斥）。

---

## 后端改动

### 1. 意图检测 — `state_service.py` 新增方法

在 `StateService` 类中添加 `_detect_modification_intent(message: str) -> bool`：
- 使用正则关键词列表匹配（替换/修改/增加/删除 + 天数/行程 等中文模式）
- 纯规则匹配，零延迟，不额外消耗 LLM 调用
- 仅当 `referenced_plan_id` 存在 **且** 意图检测命中时，才进入 Phase 2

### 2. 计划修改方法 — `state_service.py` 新增 `_modify_plan_via_conversation()`

核心流程：
```
a. 序列化当前行程为 JSON 字符串
b. 构建 Modification System Prompt（约束：只改用户要求的部分，保持其他不变）
c. 调用 llm.chat_with_tools(tools=[], max_tool_rounds=1)
   → 不传 response_format（DeepSeek 兼容），靠 prompt 要求纯 JSON 输出
d. 解析：_strip_json_fence() → json.loads() → TripPlan.model_validate()
e. 失败则 retry 一次（带错误信息反馈给 LLM）
f. 成功后：复用 update_trip_plan 模式写入 DB
   - version += 1
   - change_type = "agent_regenerate"
   - change_summary = 用户消息（截断200字）
   - 记录 AuditEvent + TokenUsage
g. 返回 TripPlanResponse 或 None（优雅降级）
```

### 3. 修改 `send_conversation_message()` — 串联 Phase 1 和 Phase 2

在现有 Phase 1 完成后（获取 `content, tool_calls, usage` 之后），插入：

```python
updated_plan = None
if referenced_plan_id and _detect_modification_intent(message):
    try:
        updated_plan = await self._modify_plan_via_conversation(...)
        if updated_plan:
            content = f"✅ 行程已更新(版本{updated_plan.version})\n\n{content}"
    except Exception:
        logger.warning(...)  # 优雅降级，不影响聊天

return ConversationResponse(..., updated_plan=updated_plan)  # 不再是 None
```

### 4. 提取 `_strip_json_fence` 到 `StateService`

将 `LLMPlannerAgent._strip_json_fence()` 的纯函数逻辑复制为 `StateService` 的静态方法（10行，无依赖）。

### 涉及文件

| 文件 | 改动 |
|------|------|
| `backend/app/services/state_service.py` | 主要改动：意图检测 + 修改方法 + 串联 + JSON fence 工具 |
| （无其他后端文件需要改动） | Schema `ConversationResponse.updated_plan` 已存在，无需变更 |

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
| LLM Phase 2 返回无效 JSON（两次都失败） | 记录警告日志 + AuditEvent，`updated_plan=None`，对话正常继续 |
| 意图检测误判（用户说"换"但不是改行程） | Phase 2 prompt 会要求 LLM 识别无修改需求 → 可扩展返回 `{"no_change": true}` |
| 连续快速发两条修改消息 | 第二条基于第一条的结果（DB 已 flush），版本号正确递增 |
| 引用的行程已删除/归档 | `referenced_plan` 为 None，不进入 Phase 2 |
| DeepSeek 不支持 `response_format` | Phase 2 不使用该参数，仅靠 prompt 要求纯 JSON + `_strip_json_fence` 清理 |

---

## 验证方案

1. **单元测试**（`backend/tests/` 新增）：
   - `test_detect_modification_intent`：验证中英文关键词命中/不命中
   - `test_modify_success`：Mock LLM 返回有效 JSON，验证版本递增、change_type、audit 事件
   - `test_modify_retry`：Mock 先无效后有效 JSON，验证 retry 成功
   - `test_modify_both_fail`：Mock 两次无效 JSON，验证返回 None 且不写入 DB

2. **端到端测试**：
   - 生成一个行程 → 进入对话 → 输入"把第二天的故宫换成天坛"
   - 验证：助手回复含"行程已更新" + `updated_plan` 非空
   - 回到 Result 页面：行程内容已变为天坛，版本号 +1
   - 查看版本历史：出现 `change_type=agent_regenerate` 的版本记录

3. **回归测试**：确保非修改类对话（如"天坛好玩吗"）不触发 Phase 2，行为不变
