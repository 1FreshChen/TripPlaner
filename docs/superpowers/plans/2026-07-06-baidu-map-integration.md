# Baidu Map Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Baidu Map data as a non-breaking second provider for restaurant details and route estimates in the trip planner.

**Architecture:** Keep AMap as the existing default provider and add a focused `BaiduMapService` with two LLM tools registered beside existing tools. Configuration defaults to an empty Baidu API key, so local tests and deployments continue to work without a key. Planner prompts teach the LLM when to use Baidu for restaurant ratings, price, business hours, and route duration.

**Tech Stack:** Python, FastAPI, Pydantic Settings, requests, pytest.

---

## File Structure

- Create `backend/app/services/baidu_map_service.py`: Baidu HTTP client, POI parsing, city geocoding, and route search helpers.
- Create `backend/app/tools/implementations/baidu_poi_search.py`: LLM tool that exposes Baidu restaurant/food POI search.
- Create `backend/app/tools/implementations/baidu_direction.py`: LLM tool that exposes Baidu DirectionLite route estimates.
- Modify `backend/app/tools/implementations/__init__.py`: export the two new tools.
- Modify `backend/app/config.py`: add `BAIDU_MAP_API_KEY`.
- Modify `backend/.env` and `.env.example`: add empty `BAIDU_MAP_API_KEY` placeholder.
- Modify `backend/app/tools/bootstrap.py`: instantiate/register Baidu tools while preserving old arguments.
- Modify `backend/app/orchestration/bootstrap.py`: accept and pass an optional Baidu service to tool bootstrap.
- Modify `backend/app/agents/trip_planner.py`: include Baidu tools and restaurant quality instruction in planner query.
- Add `backend/tests/test_baidu_map_service.py`: service parsing and disabled-key behavior tests.
- Modify `backend/tests/test_tools.py`: test new tools and registry list.
- Modify `backend/tests/test_config.py`: test Baidu API key default/env override.
- Modify `backend/tests/test_llm_planner_bootstrap.py`: test planner query mentions Baidu guidance.

---

### Task 1: Configuration Tests

**Files:**
- Modify: `backend/tests/test_config.py`
- Modify: `backend/app/config.py`
- Modify: `backend/.env`
- Modify: `.env.example`

- [ ] **Step 1: Write failing config tests**

Add:

```python
def test_settings_baidu_map_api_key_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("BAIDU_MAP_API_KEY", raising=False)

    settings = Settings()

    assert settings.baidu_map_api_key == ""


def test_settings_baidu_map_api_key_can_be_configured(monkeypatch):
    monkeypatch.setenv("BAIDU_MAP_API_KEY", "baidu-test-key")

    settings = Settings()

    assert settings.baidu_map_api_key == "baidu-test-key"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_config.py -q
```

Expected: fails with `Settings` missing `baidu_map_api_key`.

- [ ] **Step 3: Add settings field and env placeholders**

Add to `Settings`:

```python
baidu_map_api_key: str = Field(default="", alias="BAIDU_MAP_API_KEY")
```

Add to env files:

```dotenv
BAIDU_MAP_API_KEY=
```

- [ ] **Step 4: Run config tests**

Run:

```bash
python -m pytest tests/test_config.py -q
```

Expected: all config tests pass.

---

### Task 2: Baidu Service Tests and Implementation

**Files:**
- Create: `backend/tests/test_baidu_map_service.py`
- Create: `backend/app/services/baidu_map_service.py`

- [ ] **Step 1: Write failing service tests**

Cover:

```python
def test_baidu_service_returns_empty_when_disabled():
    service = BaiduMapService("")
    assert service.enabled is False
    assert service.search_pois("川菜", "北京") == []
    assert service.geo_city("北京") is None
    assert service.get_direction("故宫", "颐和园", "北京") == []


def test_search_pois_sends_scope_2_and_sort_params(monkeypatch):
    # monkeypatch requests.get, assert endpoint /place/v2/search and params:
    # query, region, tag, scope=2, output=json, ak, page_size, sort_name.
```

```python
def test_poi_to_restaurant_extracts_baidu_detail_fields():
    poi = {
        "name": "测试川菜",
        "address": "东城区",
        "location": {"lng": 116.4, "lat": 39.9},
        "detail_info": {
            "overall_rating": "4.8",
            "taste_rating": "4.9",
            "service_rating": "4.7",
            "environment_rating": "4.6",
            "price": "120",
            "shop_hours": "10:00-22:00",
            "comment_num": "188",
        },
    }

    restaurant = BaiduMapService("key").poi_to_restaurant(poi)

    assert restaurant["name"] == "测试川菜"
    assert restaurant["rating"] == 4.8
    assert restaurant["taste_rating"] == 4.9
    assert restaurant["price"] == 120
    assert restaurant["location"] == {"longitude": 116.4, "latitude": 39.9}
```

```python
def test_geo_city_uses_geocoding_response(monkeypatch):
    # monkeypatch /geocoding/v3 response with result.location.
    assert service.geo_city("北京").longitude == 116.4
```

```python
def test_get_direction_parses_route_distance_and_duration(monkeypatch):
    # monkeypatch place search for origin/destination and directionlite response.
    assert route["distance_m"] == 15200
    assert route["duration_text"] == "约45分钟"
```

- [ ] **Step 2: Run service tests to verify they fail**

Run:

```bash
python -m pytest tests/test_baidu_map_service.py -q
```

Expected: import failure because service does not exist.

- [ ] **Step 3: Implement `BaiduMapService`**

Implement:

```python
class BaiduMapService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.place_url = "https://api.map.baidu.com/place/v2"
        self.geocoding_url = "https://api.map.baidu.com/geocoding/v3"
        self.direction_url = "https://api.map.baidu.com/directionlite/v1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)
```

Use `requests.get(..., timeout=10)`, catch exceptions, log warnings, and return empty data on failures. Parse `location` from either Baidu dict form (`{"lng": ..., "lat": ...}`) or comma-separated fallback strings.

- [ ] **Step 4: Run service tests**

Run:

```bash
python -m pytest tests/test_baidu_map_service.py -q
```

Expected: all Baidu service tests pass.

---

### Task 3: Baidu Tool Tests and Implementation

**Files:**
- Modify: `backend/tests/test_tools.py`
- Create: `backend/app/tools/implementations/baidu_poi_search.py`
- Create: `backend/app/tools/implementations/baidu_direction.py`
- Modify: `backend/app/tools/implementations/__init__.py`
- Modify: `backend/app/tools/bootstrap.py`

- [ ] **Step 1: Write failing tool tests**

Update bootstrap expectation:

```python
registry = bootstrap_tools(FakeAmapService(), FakeUnsplashService(), FakeBaiduService(), reset=True)
assert [tool.name for tool in registry.list_all()] == [
    "amap_poi_search",
    "amap_weather",
    "hotel_search",
    "baidu_poi_search",
    "baidu_direction",
    "budget_calculator",
    "unsplash_image",
]
assert len(registry.get_openai_functions()) == 7
```

Add tests:

```python
def test_baidu_poi_search_tool_normalizes_limit_and_returns_restaurants():
    result = asyncio.run(BaiduPOISearchTool(FakeBaiduService()).execute("川菜", "北京", tag="川菜", limit=50))
    assert result["success"] is True
    assert result["count"] == 1
    assert result["restaurants"][0]["taste_rating"] == 4.9


def test_baidu_direction_tool_returns_routes():
    result = asyncio.run(BaiduDirectionTool(FakeBaiduService()).execute("故宫", "颐和园", "北京"))
    assert result["success"] is True
    assert result["routes"][0]["duration_text"] == "约45分钟"
```

- [ ] **Step 2: Run tool tests to verify they fail**

Run:

```bash
python -m pytest tests/test_tools.py -q
```

Expected: imports/registry assertions fail because tools are not implemented.

- [ ] **Step 3: Implement tools and registration**

`BaiduPOISearchTool.execute()` clamps `limit` to `1..20`, calls `search_pois()`, converts each POI with `poi_to_restaurant()`, and returns:

```python
{"restaurants": restaurants, "count": len(restaurants), "city": city, "success": True}
```

`BaiduDirectionTool.execute()` clamps `mode` to `driving/walking/transit/riding`, calls `get_direction()`, and returns:

```python
{"routes": routes, "count": len(routes), "city": city, "success": True}
```

Update `bootstrap_tools(amap_service=None, unsplash_service=None, baidu_service=None, reset=False)` and register Baidu tools after hotel search.

- [ ] **Step 4: Run tool tests**

Run:

```bash
python -m pytest tests/test_tools.py -q
```

Expected: all tool tests pass.

---

### Task 4: Orchestration and Prompt Integration

**Files:**
- Modify: `backend/app/orchestration/bootstrap.py`
- Modify: `backend/app/agents/trip_planner.py`
- Modify: `backend/tests/test_llm_planner_bootstrap.py`

- [ ] **Step 1: Write failing prompt/bootstrap tests**

Add assertions:

```python
assert "baidu_poi_search" in query
assert "baidu_direction" in query
assert "口味" in query
assert "人均" in query
```

Add bootstrap injection test:

```python
def test_bootstrap_passes_baidu_service_to_tool_bootstrap(monkeypatch):
    captured = {}

    def fake_bootstrap_tools(**kwargs):
        captured.update(kwargs)
        return FakeToolRegistry()

    monkeypatch.setattr("app.orchestration.bootstrap.bootstrap_tools", fake_bootstrap_tools)
    baidu_service = object()

    bootstrap_orchestration(
        llm_service=FakeLLMService(),
        enable_external_services=True,
        tool_registry=None,
        baidu_service=baidu_service,
    )

    assert captured["baidu_service"] is baidu_service
```

- [ ] **Step 2: Run target tests to verify they fail**

Run:

```bash
python -m pytest tests/test_llm_planner_bootstrap.py -q
```

Expected: prompt assertions and/or `baidu_service` argument fail.

- [ ] **Step 3: Implement orchestration and prompt updates**

Add `baidu_service` optional argument to `bootstrap_orchestration()` and pass it to `bootstrap_tools(amap_service=amap, baidu_service=baidu_service)`.

Update planner query available tools block with `baidu_poi_search` and `baidu_direction`, then add restaurant quality guidance as item 6.

- [ ] **Step 4: Run target tests**

Run:

```bash
python -m pytest tests/test_llm_planner_bootstrap.py tests/test_trip_planner.py -q
```

Expected: all selected tests pass.

---

### Task 5: Full Verification and Review

**Files:**
- Review all modified files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python -m pytest tests/test_baidu_map_service.py tests/test_tools.py tests/test_config.py tests/test_llm_planner_bootstrap.py tests/test_trip_planner.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full backend tests**

Run:

```bash
python -m pytest tests -q
```

Expected: all backend tests pass.

- [ ] **Step 3: Review consistency**

Check:

- New imports match actual file names.
- Empty `BAIDU_MAP_API_KEY` keeps tools callable but service returns empty data.
- Existing AMap tools and call signatures remain compatible.
- Prompt names match registered tool names.
- No real API key is hard-coded.

- [ ] **Step 4: Report remaining user-supplied information**

Tell the user to fill:

- `BAIDU_MAP_API_KEY` in `backend/.env`.
- Optionally mirror it in deployment environment variables.

