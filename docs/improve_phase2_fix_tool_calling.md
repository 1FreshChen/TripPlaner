# Phase 2: 修复 chat_with_tools 多轮工具调用

## 改进的目的

当前 `LLMService.chat_with_tools()` 方法（`backend/app/services/llm_service.py` 第 81-141 行）存在一个关键缺陷：LLM 返回 `tool_calls` 后，方法**不执行工具、不把结果反馈给 LLM**，而是直接 return。这导致：

- 该方法实际上是单轮的——LLM 说"我要查天气"，但天气结果永远不会回到 LLM
- 对话端点（`state_service.py`）被迫用手动两步流程绕过这个问题
- 规划 Agent 无法使用多轮工具调用模式

这个修复是 Phase 3（LLM 驱动的 PlannerAgent）的**必要前提**。

## 改进的内容

### 当前流程（有问题）

```
1. 构造 messages（system + user）
2. POST /chat/completions（带 tools）
3. LLM 返回 tool_calls
4. 记录日志
5. → return tool_calls    ← 问题：不执行工具，不继续对话
```

### 改进后流程

```
1. 构造 messages（system + user）
2. POST /chat/completions（带 tools）
3. 如果 LLM 返回 tool_calls 且未超过 max_tool_rounds：
   a. 执行每个 tool_call → 获取结果
   b. 将 assistant 消息（含 tool_calls）追加到 messages
   c. 将 tool 结果消息追加到 messages
   d. → 回到步骤 2
4. 如果 LLM 返回纯文本（无 tool_calls）：
   → 返回最终文本 + 累计 TokenUsage
```

### 方法签名变更

```python
# 旧签名
def chat_with_tools(self, system_prompt, user_prompt, tools, max_tool_rounds=5)
    -> Tuple[str, List[Dict], TokenUsage]

# 新签名
async def chat_with_tools(
    self,
    system_prompt: str,
    user_prompt: str,
    tools: List[Dict[str, Any]],
    tool_executor,                    # 新增：用于执行工具调用
    max_tool_rounds: int = 5,
) -> Tuple[str, List[Dict[str, Any]], TokenUsage]:
```

### 工具执行集成

新增参数 `tool_executor` 是 `ToolExecutor` 实例，方法内部通过它执行工具：

```python
# 伪代码
for tool_call in tool_calls:
    tool_name = tool_call["function"]["name"]
    tool_args = json.loads(tool_call["function"]["arguments"])
    result = await tool_executor.execute_by_name(tool_name, **tool_args)
    tool_results.append({
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "content": json.dumps(result)
    })
```

## 改进的方法

### 修改文件

`backend/app/services/llm_service.py` —— 重写 `chat_with_tools()` 方法

### 实施步骤

1. **添加 `ToolExecutor` 参数**：修改方法签名，接受一个 `tool_executor` 对象
2. **将同步方法改为异步**：`def` → `async def`，使用 `httpx.AsyncClient` 替代 `requests`
3. **实现消息累积循环**：
   - 维护 `messages` 列表
   - 每轮将 LLM 响应追加到 messages
   - 如果有 tool_calls，执行工具并将结果追加
   - 循环直到 LLM 返回纯文本或达到最大轮次
4. **累计 TokenUsage**：跨所有轮次累加 token 用量
5. **超时处理**：每轮独立超时，避免单轮卡死影响全局
6. **保留旧的 `chat_with_tools` 为 `chat_with_tools_legacy`**：确保对话端点不受影响

### 同步更新调用方

1. `backend/app/services/state_service.py` 中的 `send_conversation_message()` 方法
2. 后续 Phase 3 中的 `LLMPlannerAgent`

### 验证方式

1. 编写单元测试模拟 LLM 返回 tool_calls
2. 验证工具被实际执行
3. 验证工具结果被返回给 LLM 做下一轮调用
4. 验证纯文本响应时循环正确退出
5. 验证 `max_tool_rounds` 限制生效

### 影响范围

- 核心修改：1 个文件（`llm_service.py`）
- 调用方适配：1 个文件（`state_service.py`）
- 需要添加 `httpx` 异步支持（已在 requirements.txt 中）
