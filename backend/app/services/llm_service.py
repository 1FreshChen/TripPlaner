from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
import requests

logger = logging.getLogger(__name__)

MODEL_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-v3": (0.14, 0.28),
    "deepseek": (0.14, 0.28),
}


@dataclass
class TokenUsage:
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate an LLM call cost in USD."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        pricing = next((value for key, value in MODEL_PRICING.items() if key in model), None)
    if pricing is None:
        return 0.0

    prompt_price, completion_price = pricing
    cost = (prompt_tokens / 1_000_000) * prompt_price + (completion_tokens / 1_000_000) * completion_price
    return round(cost, 8)


class LLMService:
    """OpenAI-compatible chat completions client."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def generate_json(self, system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                },
                timeout=240,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as exc:
            logger.warning("LLM JSON generation failed, fallback will be used: %s", exc)
            return None

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_executor: Any,
        max_tool_rounds: int = 5,
        response_format: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], TokenUsage]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tool_calls_log: List[Dict[str, Any]] = []
        total_usage = TokenUsage(model=self.model, provider=self._infer_provider())

        if not self.enabled:
            return "", tool_calls_log, total_usage

        tool_rounds = 0
        async with httpx.AsyncClient(timeout=180.0) as client:
            while True:
                request_json: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.4,
                }
                if response_format is not None:
                    request_json["response_format"] = response_format
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=request_json,
                )
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]

                usage = body.get("usage", {})
                total_usage.prompt_tokens += usage.get("prompt_tokens", 0)
                total_usage.completion_tokens += usage.get("completion_tokens", 0)
                total_usage.total_tokens += usage.get("total_tokens", 0)

                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    return message.get("content", ""), tool_calls_log, total_usage

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                parsed_calls: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
                for tool_call in tool_calls:
                    tool_name = tool_call.get("function", {}).get("name", "")
                    arguments = json.loads(tool_call.get("function", {}).get("arguments") or "{}")
                    tool_calls_log.append(
                        {
                            "tool": tool_name,
                            "arguments": arguments,
                            "id": tool_call.get("id"),
                        }
                    )
                    parsed_calls.append((tool_call, tool_name, arguments))

                if tool_rounds >= max_tool_rounds:
                    logger.warning("LLM tool round limit reached: %s", max_tool_rounds)
                    # Finalize: make one last call WITHOUT tools, forcing JSON output
                    # (DeepSeek models otherwise loop forever calling tools)
                    return await self._finalize_with_json(
                        client=client,
                        system_prompt=system_prompt,
                        messages=messages,
                        tool_calls_log=tool_calls_log,
                        total_usage=total_usage,
                        pending_tool_calls=parsed_calls,
                    )

                for tool_call, tool_name, arguments in parsed_calls:
                    try:
                        result = await tool_executor.execute_by_name(tool_name, **arguments)
                    except Exception as exc:
                        logger.warning("Tool execution failed for '%s': %s", tool_name, exc)
                        result = {"success": False, "tool": tool_name, "error": str(exc)}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                tool_rounds += 1

    def _infer_provider(self) -> str:
        if "openai" in self.base_url:
            return "openai"
        if "deepseek" in self.base_url:
            return "deepseek"
        return "custom"

    @staticmethod
    def _messages_without_unanswered_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        clean_messages: List[Dict[str, Any]] = []
        for message in messages:
            if "tool_calls" in message:
                content = message.get("content") or ""
                if content:
                    clean_messages.append({"role": message.get("role", "assistant"), "content": content})
                continue
            if message.get("role") == "tool":
                clean_messages.append(
                    {
                        "role": "user",
                        "content": f"工具 {message.get('tool_call_id', '')} 返回结果：{message.get('content', '')}",
                    }
                )
                continue
            clean_messages.append(dict(message))
        return clean_messages

    @staticmethod
    def _format_pending_tool_calls(
        pending_tool_calls: List[Tuple[Dict[str, Any], str, Dict[str, Any]]],
    ) -> str:
        if not pending_tool_calls:
            return ""
        summaries = [
            {"id": tool_call.get("id"), "tool": tool_name, "arguments": arguments}
            for tool_call, tool_name, arguments in pending_tool_calls
        ]
        return (
            "\n以下工具调用因为达到工具轮数上限没有继续执行，请不要再次调用它们，"
            f"只基于已有数据完成最终 JSON：{json.dumps(summaries, ensure_ascii=False)}"
        )

    async def _finalize_with_json(
        self,
        client: httpx.AsyncClient,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tool_calls_log: List[Dict[str, Any]],
        total_usage: TokenUsage,
        pending_tool_calls: Optional[List[Tuple[Dict[str, Any], str, Dict[str, Any]]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], TokenUsage]:
        """Make a final call WITHOUT tools, forcing JSON output.

        DeepSeek models tend to loop forever calling tools and never produce a
        final answer. This method strips tools from the request and sets
        response_format to json_object so the model is forced to output JSON
        based on the tool results already accumulated.
        """
        clean_messages = self._messages_without_unanswered_tool_calls(messages)
        pending_summary = self._format_pending_tool_calls(pending_tool_calls or [])
        clean_messages.append({
            "role": "user",
            "content": (
                "你已经收集了足够的数据。请现在根据以上所有已成功返回的工具结果，生成最终的 JSON 行程计划。"
                "不要继续请求工具调用。只输出一个合法的 JSON 对象，不要包含任何解释、markdown 标记或工具调用。"
                f"{pending_summary}"
            ),
        })
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": clean_messages,
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            body = response.json()
            usage = body.get("usage", {})
            total_usage.prompt_tokens += usage.get("prompt_tokens", 0)
            total_usage.completion_tokens += usage.get("completion_tokens", 0)
            total_usage.total_tokens += usage.get("total_tokens", 0)
            content = body["choices"][0]["message"].get("content", "")
            return content, tool_calls_log, total_usage
        except Exception as exc:
            logger.warning("LLM finalize JSON call failed: %s", exc)
            return "", tool_calls_log, total_usage
