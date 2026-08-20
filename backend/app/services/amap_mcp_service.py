from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from hello_agents.tools import MCPTool

from app.config import get_settings
from app.models.schemas import WeatherInfo
from app.services.amap_service import (
    AmapService,
    ServiceResult,
    get_amap_rate_limiter,
    is_qps_limit_error,
)


logger = logging.getLogger(__name__)


@dataclass
class _MCPRequest:
    tool_name: str
    arguments: Dict[str, Any]
    future: Future

class _FastMCPClientAdapter:
    """Normalize FastMCP results without using HelloAgents' console-printing client."""

    def __init__(self, server_command: List[str], env: Dict[str, str]):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command=server_command[0],
            args=server_command[1:],
            env=env,
        )
        self._client = Client(transport)

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._client.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._client.list_tools()
        tools = result.tools if hasattr(result, "tools") else result
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": getattr(tool, "inputSchema", {}),
            }
            for tool in tools
        ]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        result = await self._client.call_tool(tool_name, arguments)
        content = getattr(result, "content", None)
        if not content:
            return getattr(result, "data", None)
        if len(content) == 1:
            item = content[0]
            if hasattr(item, "text"):
                return item.text
            if hasattr(item, "data"):
                return item.data
        return [
            getattr(item, "text", getattr(item, "data", str(item)))
            for item in content
        ]



class _PersistentMCPConnection:
    """Keep one MCP client context alive on a dedicated event-loop thread."""

    def __init__(
        self,
        server_command: List[str],
        env: Dict[str, str],
        startup_timeout: float,
        call_timeout: float,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self._server_command = server_command
        self._env = env
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._client_factory = client_factory
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None
        self._active_call: Optional[asyncio.Task] = None
        self._tools: List[Dict[str, Any]] = []
        self._startup_error: Optional[BaseException] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._closing = threading.Event()
        self._start_lock = threading.Lock()

    @property
    def available_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools)

    @property
    def is_healthy(self) -> bool:
        thread_stopped = self._thread is not None and self._stopped.is_set()
        return not self._closing.is_set() and not thread_stopped

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            if self._stopped.is_set():
                raise RuntimeError("MCP connection has already been closed")
            self._thread = threading.Thread(
                target=self._run,
                name="amap-mcp-client",
                daemon=True,
            )
            self._thread.start()

        if not self._ready.wait(self._startup_timeout):
            self.close()
            raise TimeoutError("Timed out while starting the AMap MCP server")
        if self._startup_error is not None:
            raise RuntimeError("Failed to start the AMap MCP server") from self._startup_error

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        self.start()
        if self._loop is None or self._queue is None or self._stopped.is_set():
            raise RuntimeError("AMap MCP server is not available")

        future: Future = Future()
        request = _MCPRequest(tool_name=tool_name, arguments=arguments, future=future)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, request)
        try:
            return future.result(timeout=self._call_timeout + 1)
        except FutureTimeoutError as exc:
            future.cancel()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._cancel_active_call)
            raise TimeoutError(f"AMap MCP tool '{tool_name}' timed out") from exc

    def _cancel_active_call(self) -> None:
        if self._active_call is not None and not self._active_call.done():
            self._active_call.cancel()

    def close(self) -> None:
        self._closing.set()
        thread = self._thread
        if thread is None:
            self._stopped.set()
            return
        if self._loop is not None and thread.is_alive():
            if self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        if thread is not threading.current_thread():
            thread.join(timeout=self._call_timeout + 5)
        if thread.is_alive():
            logger.warning("AMap MCP client thread did not stop before timeout")

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            if not self._closing.is_set():
                self._startup_error = exc
                logger.warning("AMap MCP connection stopped unexpectedly: %s", exc)
        finally:
            self._stopped.set()
            self._ready.set()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._queue = asyncio.Queue()
        first_connection = True

        while not self._closing.is_set():
            reconnect = False
            client_context = self._create_client()
            async with client_context as client:
                self._tools = await client.list_tools()
                if first_connection:
                    self._ready.set()
                    first_connection = False

                while not self._closing.is_set():
                    request = await self._queue.get()
                    if request is None:
                        return
                    if request.future.cancelled():
                        continue
                    try:
                        self._active_call = asyncio.create_task(
                            client.call_tool(request.tool_name, request.arguments)
                        )
                        result = await asyncio.wait_for(
                            self._active_call,
                            timeout=self._call_timeout,
                        )
                    except TimeoutError:
                        logger.warning("AMap MCP tool '%s' timed out; reconnecting", request.tool_name)
                        if not request.future.cancelled():
                            request.future.set_exception(
                                TimeoutError(f"AMap MCP tool '{request.tool_name}' timed out")
                            )
                        reconnect = True
                        break
                    except asyncio.CancelledError:
                        if self._closing.is_set():
                            raise
                        if not request.future.cancelled():
                            request.future.set_exception(
                                RuntimeError("AMap MCP tool call was cancelled")
                            )
                        reconnect = True
                        break
                    except Exception as exc:
                        if not request.future.cancelled():
                            request.future.set_exception(exc)
                    else:
                        if not request.future.cancelled():
                            request.future.set_result(result)
                    finally:
                        self._active_call = None

            if reconnect:
                logger.warning("Rebuilding AMap MCP client after an interrupted tool call")
                continue
            break

    def _create_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        return _FastMCPClientAdapter(self._server_command, self._env)


class SharedMCPTool(MCPTool):
    """MCPTool backed by the service's single persistent connection."""

    def __init__(self, server_command: List[str], env: Dict[str, str]):
        self._call_handler: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        super().__init__(
            name="amap_mcp",
            server_command=server_command,
            env=env,
            auto_expand=True,
        )

    def _prepare_env(
        self,
        env: Optional[Dict[str, str]],
        env_keys: Optional[List[str]],
        server_command: Optional[List[str]],
    ) -> Dict[str, str]:
        return dict(env or {})
    def _discover_tools(self) -> None:
        # The persistent connection performs discovery. Skipping eager discovery
        # prevents MCPTool from starting a temporary second server process.
        self._available_tools = []

    def bind(
        self,
        available_tools: List[Dict[str, Any]],
        call_handler: Callable[[str, Dict[str, Any]], Any],
    ) -> None:
        self._available_tools = list(available_tools)
        self._call_handler = call_handler

    def run(self, parameters: Dict[str, Any]) -> Any:
        action = parameters.get("action", "").lower()
        if not action and parameters.get("tool_name"):
            action = "call_tool"
        if action == "list_tools":
            return list(self._available_tools)
        if action != "call_tool":
            raise ValueError(f"Unsupported AMap MCP action: {action or 'missing'}")
        if self._call_handler is None:
            raise RuntimeError("AMap MCP tool has not been connected")
        tool_name = parameters.get("tool_name")
        if not tool_name:
            raise ValueError("tool_name is required for an MCP tool call")
        return self._call_handler(tool_name, parameters.get("arguments", {}))


class AmapMCPService(AmapService):
    """AMap provider that routes POI and weather calls through one MCPTool."""

    SEARCH_TOOL_NAMES = ("maps_text_search", "amap_maps_text_search")
    DETAIL_TOOL_NAMES = ("maps_search_detail", "amap_maps_search_detail")
    WEATHER_TOOL_NAMES = ("maps_weather", "amap_maps_weather")

    def __init__(
        self,
        api_key: str,
        server_command: List[str],
        enable_mcp: bool = True,
        http_fallback: bool = True,
        startup_timeout: float = 90.0,
        call_timeout: float = 20.0,
        connection: Optional[_PersistentMCPConnection] = None,
    ):
        super().__init__(api_key)
        self._mcp_enabled = enable_mcp and bool(api_key)
        self._http_fallback = http_fallback
        self._server_command = _normalize_server_command(server_command)
        self._env = {
            "AMAP_API_KEY": api_key,
            "AMAP_MAPS_API_KEY": api_key,
        }
        self._connection = connection or _PersistentMCPConnection(
            server_command=self._server_command,
            env=self._env,
            startup_timeout=startup_timeout,
            call_timeout=call_timeout,
        )
        self.mcp_tool = SharedMCPTool(self._server_command, self._env)
        self._bind_lock = threading.Lock()
        self._bound = False

    @property
    def healthy(self) -> bool:
        return bool(getattr(self._connection, "is_healthy", True))

    def search_pois(self, keywords: str, city: str, offset: int = 10) -> ServiceResult[Dict]:
        if not self.enabled:
            return ServiceResult(
                error="AMap API key is not configured",
                error_kind="configuration",
                source="amap_mcp",
            )
        if not self._mcp_enabled:
            return self._fallback_search(keywords, city, offset)

        limit = max(1, min(offset, 20))
        try:
            payload = self._call_mcp(
                self.SEARCH_TOOL_NAMES,
                {"keywords": keywords, "city": city},
                cost=3.0,
            )
            pois = self._extract_pois(payload)
            normalized = self._enrich_pois(pois[:limit])
            results = [item for item in normalized if item]
            if results:
                return ServiceResult(data=results, source="amap_mcp")
            fallback = self._fallback_search(keywords, city, limit)
            if fallback.data or fallback.is_error:
                fallback.fallback_from = "amap_mcp_empty"
                return fallback
            return ServiceResult(source="amap_mcp")
        except Exception as exc:
            logger.warning("AMap MCP POI search failed; using fallback: %s", exc)
            fallback = self._fallback_search(keywords, city, limit)
            if fallback.data:
                fallback.fallback_from = f"amap_mcp: {exc}"
                return fallback
            if fallback.is_error:
                fallback.error = f"AMap MCP failed: {exc}; HTTP fallback failed: {fallback.error}"
                fallback.fallback_from = "amap_mcp"
                return fallback
            return ServiceResult(
                error=f"AMap MCP failed: {exc}",
                error_kind=_error_kind(exc),
                source="amap_mcp",
            )

    def get_weather(self, city: str) -> ServiceResult[WeatherInfo]:
        if not self.enabled:
            return ServiceResult(
                error="AMap API key is not configured",
                error_kind="configuration",
                source="amap_mcp",
            )
        if not self._mcp_enabled:
            return self._fallback_weather(city)

        try:
            payload = self._call_mcp(self.WEATHER_TOOL_NAMES, {"city": city})
            weather = [self._weather_from_cast(item) for item in self._extract_weather_casts(payload)]
            if weather:
                return ServiceResult(data=weather, source="amap_mcp")
            fallback = self._fallback_weather(city)
            if fallback.data or fallback.is_error:
                fallback.fallback_from = "amap_mcp_empty"
                return fallback
            return ServiceResult(source="amap_mcp")
        except Exception as exc:
            logger.warning("AMap MCP weather query failed; using fallback: %s", exc)
            fallback = self._fallback_weather(city)
            if fallback.data:
                fallback.fallback_from = f"amap_mcp: {exc}"
                return fallback
            if fallback.is_error:
                fallback.error = f"AMap MCP failed: {exc}; HTTP fallback failed: {fallback.error}"
                fallback.fallback_from = "amap_mcp"
                return fallback
            return ServiceResult(
                error=f"AMap MCP failed: {exc}",
                error_kind=_error_kind(exc),
                source="amap_mcp",
            )

    def close(self) -> None:
        self._connection.close()

    def _ensure_bound(self) -> None:
        if self._bound:
            return
        with self._bind_lock:
            if self._bound:
                return
            self._connection.start()
            self.mcp_tool.bind(
                self._connection.available_tools,
                self._connection.call_tool,
            )
            self._bound = True

    def _call_mcp(self, aliases: tuple[str, ...], arguments: Dict[str, Any], cost: float = 1.0) -> Any:
        settings = get_settings()
        for attempt in range(settings.amap_qps_retry_attempts + 1):
            get_amap_rate_limiter().wait(cost)
            if attempt > 0:
                time.sleep(settings.amap_qps_retry_delay_seconds)
            try:
                self._ensure_bound()
                tool_name = self._resolve_tool_name(aliases)
                return self.mcp_tool.run(
                    {
                        "action": "call_tool",
                        "tool_name": tool_name,
                        "arguments": arguments,
                    }
                )
            except LookupError:
                raise
            except Exception as exc:
                if not is_qps_limit_error(exc) or attempt >= settings.amap_qps_retry_attempts:
                    raise
                logger.warning(
                    "AMap MCP QPS limit hit; retrying '%s': %s",
                    aliases[0],
                    exc,
                )

    def _resolve_tool_name(self, aliases: tuple[str, ...]) -> str:
        available = {
            tool.get("name", "")
            for tool in self._connection.available_tools
            if isinstance(tool, dict)
        }
        for alias in aliases:
            if alias in available:
                return alias
        for name in available:
            if any(name.endswith(alias) for alias in aliases):
                return name
        raise LookupError(f"AMap MCP server does not provide any of: {', '.join(aliases)}")

    def _enrich_pois(self, pois: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = [self._normalize_poi(poi) for poi in pois]
        detail_calls = 0
        detail_limit = get_settings().amap_enrich_detail_limit
        for poi in normalized:
            if self.parse_location(poi.get("location")) or not poi.get("id"):
                continue
            if detail_limit > 0 and detail_calls >= detail_limit:
                continue
            detail_calls += 1
            detail = self._fetch_poi_detail(poi["id"])
            if detail:
                poi.update(
                    {
                        key: value
                        for key, value in detail.items()
                        if value not in (None, "", [])
                    }
                )
        return normalized

    def _fetch_poi_detail(self, poi_id: str) -> Optional[Dict[str, Any]]:
        try:
            payload = self._call_mcp(self.DETAIL_TOOL_NAMES, {"id": poi_id})
            details = self._extract_pois(payload)
            if details:
                return self._normalize_poi(details[0])
        except LookupError:
            logger.debug("AMap MCP detail tool unavailable; skipping detail for %s", poi_id)
        except Exception as exc:
            logger.debug("AMap MCP POI detail failed for %s: %s", poi_id, exc)
        return None

    @staticmethod
    def _normalize_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(poi)
        location = normalized.get("location")
        if isinstance(location, dict):
            longitude = location.get("longitude", location.get("lng"))
            latitude = location.get("latitude", location.get("lat"))
            if longitude is not None and latitude is not None:
                normalized["location"] = f"{longitude},{latitude}"
        if not normalized.get("biz_ext") and normalized.get("rating") not in (None, "", []):
            normalized["biz_ext"] = {"rating": normalized["rating"]}
        return normalized

    def _fallback_search(self, keywords: str, city: str, offset: int) -> ServiceResult[Dict]:
        if not self._http_fallback:
            return ServiceResult(source="amap_mcp")
        result = super().search_pois(keywords, city, offset)
        result.source = "amap_http_fallback"
        return result

    def _fallback_weather(self, city: str) -> ServiceResult[WeatherInfo]:
        if not self._http_fallback:
            return ServiceResult(source="amap_mcp")
        result = super().get_weather(city)
        result.source = "amap_http_fallback"
        return result

    @classmethod
    def _extract_pois(cls, payload: Any) -> List[Dict[str, Any]]:
        data = cls._coerce_payload(payload)
        if isinstance(data, list):
            if len(data) == 1 and isinstance(data[0], str):
                return cls._extract_pois(data[0])
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("pois", "results", "data"):
            if key in data:
                nested = cls._extract_pois(data[key])
                if nested:
                    return nested
        if data.get("name") or data.get("id"):
            return [data]
        return []

    @classmethod
    def _extract_weather_casts(cls, payload: Any) -> List[Dict[str, Any]]:
        data = cls._coerce_payload(payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        casts = data.get("casts")
        if isinstance(casts, list):
            return [item for item in casts if isinstance(item, dict)]
        forecasts = data.get("forecasts")
        if isinstance(forecasts, list) and forecasts:
            forecast_items = [item for item in forecasts if isinstance(item, dict)]
            if forecast_items and all("date" in item for item in forecast_items):
                return forecast_items
            return cls._extract_weather_casts(forecasts[0])
        if "data" in data:
            return cls._extract_weather_casts(data["data"])
        return []

    @staticmethod
    def _coerce_payload(payload: Any) -> Any:
        if not isinstance(payload, str):
            return payload
        cleaned = payload.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            starts = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
            if not starts:
                return payload
            start = min(starts)
            end = max(cleaned.rfind("}"), cleaned.rfind("]"))
            if end <= start:
                return payload
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return payload

    @staticmethod
    def _weather_from_cast(item: Dict[str, Any]) -> WeatherInfo:
        return WeatherInfo(
            date=item.get("date", ""),
            day_weather=_first_value(item.get("dayweather", item.get("dayWeather")), "unknown"),
            night_weather=_first_value(item.get("nightweather", item.get("nightWeather")), "unknown"),
            day_temp=_first_value(item.get("daytemp", item.get("dayTemp")), 0),
            night_temp=_first_value(item.get("nighttemp", item.get("nightTemp")), 0),
            wind_direction=_first_value(item.get("daywind", item.get("dayWind")), "unknown"),
            wind_power=_first_value(item.get("daypower", item.get("dayPower")), "unknown"),
        )


def _first_value(value: Any, default: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else default
    return default if value in (None, "") else value


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    return "unexpected"


def _normalize_server_command(server_command: List[str]) -> List[str]:
    if not server_command:
        raise ValueError("AMap MCP server command cannot be empty")
    command, *args = server_command
    is_windows_script = command.lower().endswith((".cmd", ".bat"))
    if os.name == "nt" and (
        command.lower() in {"npm", "npx", "pnpm", "yarn"} or is_windows_script
    ):
        return ["cmd.exe", "/d", "/s", "/c", command, *args]
    return list(server_command)


_shared_service: Optional[AmapMCPService] = None
_shared_service_lock = threading.Lock()


def get_amap_mcp_service() -> AmapMCPService:
    global _shared_service
    if _shared_service is None or not _shared_service.healthy:
        with _shared_service_lock:
            if _shared_service is None or not _shared_service.healthy:
                stale_service = _shared_service
                settings = get_settings()
                _shared_service = AmapMCPService(
                    api_key=settings.amap_api_key,
                    server_command=[settings.amap_mcp_command, *settings.amap_mcp_args],
                    enable_mcp=settings.amap_mcp_enabled,
                    http_fallback=settings.amap_mcp_http_fallback,
                    startup_timeout=settings.amap_mcp_startup_timeout_seconds,
                    call_timeout=settings.amap_mcp_call_timeout_seconds,
                )
                if stale_service is not None:
                    stale_service.close()
    return _shared_service


def close_amap_mcp_service() -> None:
    global _shared_service
    with _shared_service_lock:
        service = _shared_service
        _shared_service = None
    if service is not None:
        service.close()


atexit.register(close_amap_mcp_service)
