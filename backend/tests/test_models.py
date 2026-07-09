import pytest
from pydantic import ValidationError

from app.models.schemas import Hotel, Location, TripPlanRequest, WeatherInfo


def test_weather_temperature_accepts_chinese_temperature_suffix():
    weather = WeatherInfo(
        date="2026-06-01",
        day_weather="晴",
        night_weather="多云",
        day_temp="26℃",
        night_temp="18°C",
        wind_direction="东南",
        wind_power="3级",
    )

    assert weather.day_temp == 26
    assert weather.night_temp == 18


def test_location_rejects_invalid_coordinate_range():
    with pytest.raises(ValidationError):
        Location(longitude=200, latitude=39.9)


def test_trip_plan_request_rejects_reversed_date_range():
    with pytest.raises(ValidationError):
        TripPlanRequest(
            city="北京",
            start_date="2026-06-03",
            end_date="2026-06-01",
            days=3,
            preferences="历史文化",
            budget="中等",
            transportation="公共交通",
            accommodation="经济型酒店",
        )



def test_hotel_rating_accepts_numeric_llm_output_as_string():
    hotel = Hotel(name="测试酒店", rating=4.7)

    assert hotel.rating == "4.7"
