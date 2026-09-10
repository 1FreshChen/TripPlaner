from app.config import get_settings
from app.services.baidu_map_service import (
    BaiduMapService,
    BaiduRateLimiter,
    is_baidu_qps_limit_error,
    reset_baidu_rate_limiter,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_baidu_service_returns_empty_when_disabled():
    service = BaiduMapService("")

    assert service.enabled is False
    assert service.search_pois("川菜", "北京") == []
    assert service.geo_city("北京") is None
    assert service.get_direction("故宫", "颐和园", "北京") == []


def test_search_pois_sends_scope_2_and_sort_params(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse({"results": [{"name": "测试餐厅"}]})

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)
    service = BaiduMapService("baidu-key")

    results = service.search_pois("川菜", "北京", tag="川菜", sort_by="taste_rating", offset=8)

    assert results == [{"name": "测试餐厅"}]
    assert calls[0]["url"] == "https://api.map.baidu.com/place/v2/search"
    assert calls[0]["params"] == {
        "query": "川菜",
        "region": "北京",
        "scope": 2,
        "output": "json",
        "ak": "baidu-key",
        "page_size": 8,
        "page_num": 0,
        "tag": "川菜",
        "sort_name": "taste_rating",
    }
    assert calls[0]["timeout"] == 10


def test_search_pois_supports_nearby_location(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": params})
        return FakeResponse({"status": 0, "results": []})

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)
    service = BaiduMapService("baidu-key")

    service.search_pois(
        "早餐",
        "青岛",
        tag="美食",
        location=service.parse_location({"lng": 120.3, "lat": 36.0}),
        radius=8000,
    )

    assert "region" not in calls[0]["params"]
    assert calls[0]["params"]["location"] == "36.0,120.3"
    assert calls[0]["params"]["radius"] == 8000


def test_get_poi_detail_uses_uid_and_scope_2(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": params})
        return FakeResponse({"status": 0, "result": {"uid": "poi-1", "name": "餐厅"}})

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)

    result = BaiduMapService("baidu-key").get_poi_detail("poi-1")

    assert result == {"uid": "poi-1", "name": "餐厅"}
    assert calls[0]["url"].endswith("/place/v2/detail")
    assert calls[0]["params"]["uid"] == "poi-1"
    assert calls[0]["params"]["scope"] == 2


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
    assert restaurant["address"] == "东城区"
    assert restaurant["rating"] == 4.8
    assert restaurant["taste_rating"] == 4.9
    assert restaurant["service_rating"] == 4.7
    assert restaurant["environment_rating"] == 4.6
    assert restaurant["price"] == 120
    assert restaurant["shop_hours"] == "10:00-22:00"
    assert restaurant["comment_num"] == 188
    assert restaurant["location"] == {"longitude": 116.4, "latitude": 39.9}


def test_parse_location_accepts_zero_coordinates():
    location = BaiduMapService.parse_location({"lng": 0, "lat": 0})

    assert location is not None
    assert location.longitude == 0
    assert location.latitude == 0


def test_geo_city_uses_geocoding_response(monkeypatch):
    def fake_get(url, params, timeout):
        assert url == "https://api.map.baidu.com/geocoding/v3"
        assert params == {"address": "北京", "output": "json", "ak": "baidu-key"}
        assert timeout == 10
        return FakeResponse({"result": {"location": {"lng": 116.4, "lat": 39.9}}})

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)
    service = BaiduMapService("baidu-key")

    location = service.geo_city("北京")

    assert location is not None
    assert location.longitude == 116.4
    assert location.latitude == 39.9


def test_get_direction_parses_route_distance_and_duration(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append({"url": url, "params": params})
        if url.endswith("/place/v2/search"):
            lng = 116.4 if params["query"] == "故宫" else 116.2
            lat = 39.9 if params["query"] == "故宫" else 39.8
            return FakeResponse({"results": [{"location": {"lng": lng, "lat": lat}}]})
        return FakeResponse({"result": {"routes": [{"distance": 15200, "duration": 2700}]}})

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)
    service = BaiduMapService("baidu-key")

    routes = service.get_direction("故宫", "颐和园", "北京", mode="transit")

    assert routes == [
        {
            "distance_m": 15200,
            "duration_s": 2700,
            "duration_text": "约45分钟",
        }
    ]
    assert calls[-1]["url"] == "https://api.map.baidu.com/directionlite/v1/transit"
    assert calls[-1]["params"] == {
        "origin": "39.9,116.4",
        "destination": "39.8,116.2",
        "ak": "baidu-key",
    }


def test_baidu_qps_detection_and_rate_limiter_spacing():
    now = [100.0]
    sleeps = []
    limiter = BaiduRateLimiter(2.0, clock=lambda: now[0], sleeper=sleeps.append)

    assert is_baidu_qps_limit_error({"status": 401, "message": "当前并发量已经超过约定并发配额，限制访问"})
    assert not is_baidu_qps_limit_error({"status": 5, "message": "AK不存在"})
    limiter.wait()
    limiter.wait()

    assert sleeps == [0.5]


def test_baidu_qps_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("BAIDU_QPS_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("BAIDU_QPS_RETRY_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    reset_baidu_rate_limiter()
    payloads = [
        {"status": 401, "message": "当前并发量已经超过约定并发配额，限制访问"},
        {"status": 0, "results": [{"uid": "poi-1", "name": "餐厅"}]},
    ]
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr("app.services.baidu_map_service.requests.get", fake_get)

    results = BaiduMapService("baidu-key").search_pois("早餐", "南京")

    assert results == [{"uid": "poi-1", "name": "餐厅"}]
    assert len(calls) == 2
