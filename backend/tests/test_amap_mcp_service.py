import asyncio
import json
from types import SimpleNamespace

import pytest

from hello_agents.tools import MCPTool

from app.agents.trip_planner import TripPlannerAgent
from app.services import amap_mcp_service
from app.services.amap_mcp_service import AmapMCPService, _PersistentMCPConnection
from app.tools.bootstrap import bootstrap_tools


class FakeConnection:
    def __init__(self):
        self.available_tools = [
            {"name": "maps_text_search", "description": "", "input_schema": {}},
            {"name": "maps_search_detail", "description": "", "input_schema": {}},
            {"name": "maps_weather", "description": "", "input_schema": {}},
        ]
        self.start_calls = 0
        self.close_calls = 0
        self.calls = []

    def start(self):
        self.start_calls += 1

    def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "maps_text_search":
            return json.dumps(
                {
                    "pois": [
                        {
                            "id": "poi-1",
                            "name": "Test Museum",
                            "address": "Center",
                            "rating": "4.8",
                        }
                    ]
                }
            )
        if tool_name == "maps_search_detail":
            return json.dumps(
                {
                    "pois": [
                        {
                            "id": "poi-1",
                            "location": {"lng": 116.397128, "lat": 39.916527},
                            "type": "museum",
                        }
                    ]
                }
            )
        if tool_name == "maps_weather":
            return json.dumps(
                {
                    "forecasts": [
                        {
                            "casts": [
                                {
                                    "date": "2026-07-15",
                                    "dayweather": "sunny",
                                    "nightweather": "cloudy",
                                    "daytemp": "30",
                                    "nighttemp": "22",
                                    "daywind": ["east"],
                                    "daypower": ["3"],
                                }
                            ]
                        }
                    ]
                }
            )
        raise AssertionError(f"Unexpected tool: {tool_name}")

    def close(self):
        self.close_calls += 1


def build_service(connection=None):
    return AmapMCPService(
        api_key="test-key",
        server_command=["npx", "-y", "@amap/amap-maps-mcp-server"],
        http_fallback=False,
        startup_timeout=1,
        call_timeout=1,
        connection=connection or FakeConnection(),
    )


def test_weather_parser_supports_official_flat_forecasts():
    casts = AmapMCPService._extract_weather_casts(
        json.dumps(
            {
                "city": "TestCity",
                "forecasts": [
                    {
                        "date": "2026-07-15",
                        "dayweather": "sunny",
                        "nightweather": "cloudy",
                    }
                ],
            }
        )
    )

    assert casts == [
        {"date": "2026-07-15", "dayweather": "sunny", "nightweather": "cloudy"}
    ]


def test_amap_mcp_service_reuses_connection_and_normalizes_results():
    connection = FakeConnection()
    service = build_service(connection)

    pois = service.search_pois("museum", "TestCity", offset=5)
    weather = service.get_weather("TestCity")

    assert connection.start_calls == 1
    assert pois == [
        {
            "id": "poi-1",
            "name": "Test Museum",
            "address": "Center",
            "rating": "4.8",
            "biz_ext": {"rating": "4.8"},
            "location": "116.397128,39.916527",
            "type": "museum",
        }
    ]
    assert [call[0] for call in connection.calls] == [
        "maps_text_search",
        "maps_search_detail",
        "maps_weather",
    ]
    assert weather[0].date == "2026-07-15"
    assert weather[0].day_temp == 30
    assert weather[0].wind_direction == "east"


def test_shared_mcp_tool_does_not_write_to_stdout(capsys):
    service = build_service()

    captured = capsys.readouterr()

    assert captured.out == ""
    assert isinstance(service.mcp_tool, MCPTool)


def test_trip_planner_agents_and_registered_tools_share_one_mcp_tool():
    service = build_service()
    planner = TripPlannerAgent(amap_service=service, enable_external_services=False)
    registry = bootstrap_tools(amap_service=service)

    assert isinstance(service.mcp_tool, MCPTool)
    assert planner.mcp_tool is service.mcp_tool
    assert planner.attraction_agent.mcp_tool is service.mcp_tool
    assert planner.weather_agent.mcp_tool is service.mcp_tool
    assert planner.hotel_agent.mcp_tool is service.mcp_tool
    assert registry.get("amap_poi_search")._amap is service
    assert registry.get("amap_weather")._amap is service
    assert registry.get("hotel_search")._amap is service


def test_persistent_connection_enters_client_context_once():
    class FakeClient:
        def __init__(self):
            self.enter_calls = 0
            self.exit_calls = 0
            self.calls = []

        async def __aenter__(self):
            self.enter_calls += 1
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.exit_calls += 1

        async def list_tools(self):
            return [{"name": "maps_weather", "description": "", "input_schema": {}}]

        async def call_tool(self, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return arguments

    client = FakeClient()
    connection = _PersistentMCPConnection(
        server_command=["fake-server"],
        env={},
        startup_timeout=1,
        call_timeout=1,
        client_factory=lambda: client,
    )

    try:
        assert connection.call_tool("maps_weather", {"city": "A"}) == {"city": "A"}
        assert connection.call_tool("maps_weather", {"city": "B"}) == {"city": "B"}
    finally:
        connection.close()

    assert client.enter_calls == 1
    assert client.exit_calls == 1
    assert len(client.calls) == 2


def test_persistent_connection_cancels_startup_after_timeout():
    class HangingClient:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connection = _PersistentMCPConnection(
        server_command=["fake-server"],
        env={},
        startup_timeout=0.05,
        call_timeout=0.1,
        client_factory=HangingClient,
    )

    with pytest.raises(TimeoutError):
        connection.start()

    assert connection._stopped.is_set()
    assert connection._thread is not None
    assert not connection._thread.is_alive()


def test_get_amap_mcp_service_is_process_singleton(monkeypatch):
    settings = SimpleNamespace(
        amap_api_key="",
        amap_mcp_command="npx",
        amap_mcp_args=["-y", "@amap/amap-maps-mcp-server"],
        amap_mcp_enabled=True,
        amap_mcp_http_fallback=True,
        amap_mcp_startup_timeout_seconds=1,
        amap_mcp_call_timeout_seconds=1,
    )
    amap_mcp_service.close_amap_mcp_service()
    monkeypatch.setattr(amap_mcp_service, "get_settings", lambda: settings)

    try:
        first = amap_mcp_service.get_amap_mcp_service()
        second = amap_mcp_service.get_amap_mcp_service()
        assert first is second
        assert first.mcp_tool is second.mcp_tool
    finally:
        amap_mcp_service.close_amap_mcp_service()


def test_mcp_failure_returns_empty_when_http_fallback_is_disabled():
    class FailedConnection(FakeConnection):
        def start(self):
            raise RuntimeError("server unavailable")

    service = build_service(FailedConnection())

    assert service.search_pois("museum", "TestCity") == []
    assert service.get_weather("TestCity") == []
