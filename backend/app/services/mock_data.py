from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List

from app.models.schemas import Attraction, Hotel, Location, Meal, WeatherInfo


CITY_CENTERS: Dict[str, Location] = {
    "北京": Location(longitude=116.397128, latitude=39.916527),
    "上海": Location(longitude=121.473667, latitude=31.230525),
    "杭州": Location(longitude=120.15507, latitude=30.274084),
    "成都": Location(longitude=104.066541, latitude=30.572269),
    "广州": Location(longitude=113.264385, latitude=23.129112),
    "深圳": Location(longitude=114.057868, latitude=22.543099),
    "西安": Location(longitude=108.93977, latitude=34.341574),
}


CITY_ATTRACTIONS: Dict[str, List[Dict]] = {
    "北京": [
        {"name": "故宫博物院", "address": "北京市东城区景山前街4号", "price": 60, "category": "历史文化"},
        {"name": "天坛公园", "address": "北京市东城区天坛东路甲1号", "price": 34, "category": "历史文化"},
        {"name": "颐和园", "address": "北京市海淀区新建宫门路19号", "price": 30, "category": "皇家园林"},
        {"name": "八达岭长城", "address": "北京市延庆区G6京藏高速58号出口", "price": 40, "category": "历史文化"},
        {"name": "什刹海", "address": "北京市西城区地安门西大街", "price": 0, "category": "城市漫步"},
        {"name": "国家博物馆", "address": "北京市东城区东长安街16号", "price": 0, "category": "博物馆"},
        {"name": "圆明园", "address": "北京市海淀区清华西路28号", "price": 25, "category": "历史遗址"},
        {"name": "南锣鼓巷", "address": "北京市东城区南锣鼓巷", "price": 0, "category": "美食街区"},
        {"name": "北海公园", "address": "北京市西城区文津街1号", "price": 10, "category": "园林"},
    ],
    "上海": [
        {"name": "外滩", "address": "上海市黄浦区中山东一路", "price": 0, "category": "城市地标"},
        {"name": "上海博物馆", "address": "上海市黄浦区人民大道201号", "price": 0, "category": "博物馆"},
        {"name": "豫园", "address": "上海市黄浦区福佑路168号", "price": 40, "category": "历史园林"},
        {"name": "武康路", "address": "上海市徐汇区武康路", "price": 0, "category": "城市漫步"},
        {"name": "陆家嘴", "address": "上海市浦东新区陆家嘴", "price": 0, "category": "城市地标"},
        {"name": "田子坊", "address": "上海市黄浦区泰康路210弄", "price": 0, "category": "文艺街区"},
    ],
}

DEFAULT_ATTRACTIONS: List[Dict] = [
    {"name": "城市博物馆", "address": "市中心文化区", "price": 0, "category": "博物馆"},
    {"name": "历史街区", "address": "老城核心区", "price": 0, "category": "城市漫步"},
    {"name": "城市公园", "address": "中心公园", "price": 0, "category": "自然风光"},
    {"name": "地标广场", "address": "主城区", "price": 0, "category": "城市地标"},
    {"name": "特色美食街", "address": "商业步行街", "price": 0, "category": "美食"},
    {"name": "艺术中心", "address": "文化创意园", "price": 30, "category": "艺术"},
]


def get_city_center(city: str) -> Location:
    return CITY_CENTERS.get(city, Location(longitude=116.397128, latitude=39.916527))


def build_mock_attractions(city: str, preferences: str, days: int) -> List[Attraction]:
    raw_items = CITY_ATTRACTIONS.get(city, DEFAULT_ATTRACTIONS)
    center = get_city_center(city)
    required = max(days * 3, 6)
    attractions: List[Attraction] = []

    for index in range(required):
        item = raw_items[index % len(raw_items)]
        suffix = "" if city in CITY_ATTRACTIONS else f"·{city}"
        attractions.append(
            Attraction(
                name=f"{item['name']}{suffix}",
                address=item["address"] if city in CITY_ATTRACTIONS else f"{city}{item['address']}",
                location=Location(
                    longitude=center.longitude + (index % 3 - 1) * 0.035,
                    latitude=center.latitude + (index // 3 - 1) * 0.025,
                ),
                visit_duration=90 + (index % 3) * 30,
                description=f"适合{preferences}偏好的{item['category']}目的地，建议结合周边餐饮和交通安排游览。",
                category=item["category"],
                rating=4.3 + (index % 5) * 0.1,
                data_source="mock",
                coordinate_verified=False,
                ticket_price=item["price"],
            )
        )
    return attractions


def build_mock_hotels(city: str, accommodation: str, budget: str) -> List[Hotel]:
    center = get_city_center(city)
    base_price = {"经济": 260, "中等": 420, "舒适": 520, "高": 780, "豪华": 980}
    price = 360
    for keyword, value in base_price.items():
        if keyword in accommodation or keyword in budget:
            price = value
            break

    return [
        Hotel(
            name=f"{city}{accommodation}精选酒店",
            address=f"{city}市中心商圈",
            location=center,
            price_range=f"{price}-{price + 160}元/晚",
            rating="4.6",
            distance="距核心景点约2公里",
            type=accommodation,
            estimated_cost=price,
        ),
        Hotel(
            name=f"{city}交通便利型酒店",
            address=f"{city}地铁枢纽附近",
            location=Location(longitude=center.longitude + 0.018, latitude=center.latitude - 0.015),
            price_range=f"{max(price - 80, 180)}-{price + 80}元/晚",
            rating="4.4",
            distance="距地铁站约300米",
            type=accommodation,
            estimated_cost=max(price - 80, 180),
        ),
    ]


def build_mock_weather(start_date: str, days: int) -> List[WeatherInfo]:
    start = date.fromisoformat(start_date)
    weather_cycle = [("晴", "多云"), ("多云", "晴"), ("小雨", "阴"), ("阴", "多云")]
    forecasts: List[WeatherInfo] = []
    for index in range(days):
        day_weather, night_weather = weather_cycle[index % len(weather_cycle)]
        forecasts.append(
            WeatherInfo(
                date=(start + timedelta(days=index)).isoformat(),
                day_weather=day_weather,
                night_weather=night_weather,
                day_temp=24 + index,
                night_temp=16 + index,
                wind_direction="东南",
                wind_power="3级",
            )
        )
    return forecasts


def build_mock_meals(city: str, day_index: int, budget: str) -> List[Meal]:
    costs = {"经济": (18, 45, 55), "中等": (28, 70, 85), "舒适": (38, 100, 130), "高": (58, 160, 220), "豪华": (78, 230, 350)}
    breakfast, lunch, dinner = (28, 70, 85)
    for keyword, values in costs.items():
        if keyword in budget:
            breakfast, lunch, dinner = values
            break

    day_no = day_index + 1
    return [
        Meal(type="breakfast", name=f"{city}本地早餐 第{day_no}天", description="选择酒店附近的本地早餐。", estimated_cost=breakfast, data_source="mock"),
        Meal(type="lunch", name=f"{city}特色午餐 第{day_no}天", description="结合上午景点周边餐厅。", estimated_cost=lunch, data_source="mock"),
        Meal(type="dinner", name=f"{city}风味晚餐 第{day_no}天", description="安排在交通便利的商圈。", estimated_cost=dinner, data_source="mock"),
    ]
