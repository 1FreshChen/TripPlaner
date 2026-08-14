from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

import httpx
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, ModelSettings
from pydantic_ai.usage import RequestUsage

from app.services.llm_service import LLMService


class ToolLoopState(Protocol):
    """Minimal state shared by the provider adapter and the planner."""

    model_requests: int
    tool_rounds: int
    total_tool_calls: int
    max_tool_rounds: int
    max_tool_calls: int
    force_finalize: bool


class OpenAICompatiblePydanticModel(Model):
    """Pydantic AI model adapter for the project's OpenAI-compatible HTTP API.

    The project pins ``hello-agents==0.2.9``, which requires ``openai<2``.
    Pydantic AI 2.30's OpenAI provider requires a newer OpenAI SDK, so this
    adapter deliberately uses the existing HTTP transport instead of importing
    ``pydantic_ai.models.openai``. Pydantic AI still owns the agent loop,
    typed tools, output validation, retries, messages, and usage limits.
    """

    def __init__(
        self,
        llm_service: LLMService,
        loop_state: ToolLoopState,
        *,
        timeout_seconds: float = 180.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        self._llm_service = llm_service
        self._loop_state = loop_state
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    @property
    def model_name(self) -> str:
        return self._llm_service.model

    @property
    def system(self) -> str:
        return self._llm_service._infer_provider()

    @property
    def base_url(self) -> str:
        return self._llm_service.base_url

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_settings, params = self.prepare_request(model_settings, model_request_parameters)
        state = self._loop_state
        state.model_requests += 1

        force_finalize = (
            state.force_finalize
            or state.tool_rounds >= state.max_tool_rounds
            or state.total_tool_calls >= state.max_tool_calls
        )
        function_tools = [] if force_finalize else params.function_tools
        request_messages = self._serialize_messages(messages)
        self._append_instruction_parts(request_messages, params)

        if not function_tools:
            state.force_finalize = True
            request_messages.append(
                {
                    "role": "user",
                    "content": (
                        "工具阶段已经结束，现有 baseline 和已返回的工具结果已经是全部可用信息。"
                        "现在必须停止搜索并直接输出完整 TripPlan JSON；不要描述工具，也不要请求更多信息。"
                    ),
                }
            )

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": request_messages,
            "temperature": float((model_settings or {}).get("temperature", 0.4)),
        }
        if function_tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in function_tools]
            payload["tool_choice"] = "auto"
        else:
            # PromptedOutput is parsed and validated by Pydantic AI. JSON mode
            # gives OpenAI-compatible providers an additional transport-level
            # constraint during the forced-finalization turn.
            payload["response_format"] = {"type": "json_object"}

        body = await self._post(payload)
        choice = body["choices"][0]
        message = choice.get("message") or {}
        parts = self._response_parts(message)
        if any(isinstance(part, ToolCallPart) for part in parts):
            state.tool_rounds += 1
            if state.tool_rounds >= state.max_tool_rounds:
                state.force_finalize = True

        usage = body.get("usage") or {}
        return ModelResponse(
            parts=parts,
            usage=RequestUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            model_name=body.get("model") or self.model_name,
            provider_name=self.system,
            provider_url=self.base_url,
            provider_response_id=body.get("id"),
            finish_reason=self._map_finish_reason(choice.get("finish_reason")),
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._llm_service.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._llm_service.base_url}/chat/completions"
        if self._http_client is not None:
            response = await self._http_client.post(url, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Provider error bodies contain protocol diagnostics but never the
            # Authorization header. Keep the excerpt bounded for safe logs.
            detail = response.text[:1000].strip()
            raise RuntimeError(
                f"LLM provider returned HTTP {response.status_code}: {detail or 'empty response body'}"
            ) from exc
        return response.json()

    @staticmethod
    def _serialize_tool(tool: Any) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.parameters_json_schema,
            },
        }

    @staticmethod
    def _append_instruction_parts(
        messages: list[dict[str, Any]],
        params: ModelRequestParameters,
    ) -> None:
        contents = [
            part.content
            for part in (params.instruction_parts or [])
            if getattr(part, "content", None)
        ]
        if contents:
            instruction_text = "\n\n".join(contents)
            first_system = next((item for item in messages if item.get("role") == "system"), None)
            if first_system is not None:
                first_system["content"] = f"{first_system.get('content', '')}\n\n{instruction_text}"
            else:
                messages.insert(0, {"role": "system", "content": instruction_text})

    @classmethod
    def _serialize_messages(cls, messages: Iterable[ModelMessage]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        system_contents: list[str] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                if message.instructions:
                    system_contents.append(message.instructions)
                for part in message.parts:
                    if isinstance(part, SystemPromptPart):
                        system_contents.append(part.content)
                    elif isinstance(part, UserPromptPart):
                        serialized.append({"role": "user", "content": cls._text_content(part.content)})
                    elif isinstance(part, ToolReturnPart):
                        serialized.append(
                            {
                                "role": "tool",
                                "tool_call_id": part.tool_call_id,
                                "content": cls._json_content(part.content),
                            }
                        )
                    elif isinstance(part, RetryPromptPart):
                        retry_content = cls._json_content(part.content)
                        if part.tool_name and part.tool_call_id:
                            serialized.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": part.tool_call_id,
                                    "content": retry_content,
                                }
                            )
                        else:
                            serialized.append({"role": "user", "content": retry_content})
            elif isinstance(message, ModelResponse):
                assistant: dict[str, Any] = {"role": "assistant", "content": ""}
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for part in message.parts:
                    if isinstance(part, TextPart):
                        text_parts.append(part.content)
                    elif isinstance(part, ThinkingPart):
                        thinking_parts.append(part.content)
                    elif isinstance(part, ToolCallPart):
                        tool_calls.append(
                            {
                                "id": part.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": part.tool_name,
                                    "arguments": part.args_as_json_str(),
                                },
                            }
                        )
                assistant["content"] = "\n".join(text_parts)
                if thinking_parts:
                    # DeepSeek thinking mode requires this exact field to be
                    # replayed on subsequent tool-call turns.
                    assistant["reasoning_content"] = "\n".join(thinking_parts)
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                    if "reasoning_content" not in assistant and any(
                        item.get("reasoning_content") is not None for item in serialized
                    ):
                        assistant["reasoning_content"] = ""
                serialized.append(assistant)
        if system_contents:
            unique_contents = list(dict.fromkeys(system_contents))
            serialized.insert(0, {"role": "system", "content": "\n\n".join(unique_contents)})
        return serialized

    @staticmethod
    def _map_finish_reason(value: Any) -> Any:
        return {
            "stop": "stop",
            "length": "length",
            "tool_calls": "tool_call",
            "function_call": "tool_call",
            "content_filter": "content_filter",
        }.get(value)

    @staticmethod
    def _response_parts(message: dict[str, Any]) -> list[Any]:
        parts: list[Any] = []
        reasoning = message.get("reasoning_content")
        if reasoning:
            parts.append(ThinkingPart(content=str(reasoning)))
        content = message.get("content")
        if content:
            parts.append(TextPart(content=str(content)))
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            parts.append(
                ToolCallPart(
                    tool_name=str(function.get("name") or ""),
                    args=function.get("arguments") or "{}",
                    tool_call_id=str(tool_call.get("id") or ""),
                )
            )
        if not parts:
            parts.append(TextPart(content=""))
        return parts

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        return OpenAICompatiblePydanticModel._json_content(content)

    @staticmethod
    def _json_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if hasattr(content, "model_dump"):
            content = content.model_dump(mode="json")
        return json.dumps(content, ensure_ascii=False, default=str)
