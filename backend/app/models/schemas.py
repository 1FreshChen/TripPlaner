from __future__ import annotations

from datetime import date
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.content_filter import detect_injection, sanitize_text


def _sanitize_and_check_text(value: str) -> str:
    cleaned = sanitize_text(value)
    if detect_injection(cleaned):
        raise ValueError("输入包含潜在有害内容")
    return cleaned


class Location(BaseModel):
    """位置信息(经纬度坐标)"""

    longitude: float = Field(..., description="经度", ge=-180, le=180)
    latitude: float = Field(..., description="纬度", ge=-90, le=90)


class Attraction(BaseModel):
    """景点信息"""

    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., description="建议游览时间(分钟)", gt=0)
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, ge=0, le=5, description="评分")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    poi_id: Optional[str] = Field(default=None, description="地图服务POI唯一标识")
    data_source: Optional[str] = Field(default=None, description="景点与坐标数据来源")
    image_source: Optional[str] = Field(default=None, description="图片来源")
    coordinate_verified: bool = Field(default=False, description="坐标是否经地图POI核验")
    ticket_price: int = Field(default=0, ge=0, description="门票价格(元)")


class Meal(BaseModel):
    """餐饮信息"""

    type: str = Field(..., description="餐饮类型：breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用(元)")
    # 百度地图独有字段 — 餐厅精细化数据
    rating: Optional[float] = Field(default=None, ge=0, le=5, description="综合评分(0-5)")
    price_per_person: Optional[int] = Field(default=None, ge=0, description="人均消费(元)")
    shop_hours: Optional[str] = Field(default=None, description="营业时间")
    comment_num: Optional[int] = Field(default=None, ge=0, description="评论数量")
    data_source: Optional[str] = Field(default=None, description="餐厅明细数据来源")
    poi_id: Optional[str] = Field(default=None, description="地图服务餐厅POI唯一标识")


class Hotel(BaseModel):
    """酒店信息"""

    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用(元/晚)")

    @field_validator("rating", mode="before")
    @classmethod
    def stringify_rating(cls, value: Any) -> str:
        if value in (None, "", []):
            return ""
        return str(value)


class Budget(BaseModel):
    """预算信息"""

    total_attractions: int = Field(default=0, ge=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, ge=0, description="酒店总费用")
    total_meals: int = Field(default=0, ge=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, ge=0, description="交通总费用")
    total: int = Field(default=0, ge=0, description="总费用")


class DayPlan(BaseModel):
    """单日行程"""

    date: str = Field(..., description="日期")
    day_index: int = Field(..., ge=0, description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿安排")
    hotel: Optional[Hotel] = Field(default=None, description="酒店信息")
    attractions: List[Attraction] = Field(default_factory=list, description="景点列表")
    meals: List[Meal] = Field(default_factory=list, description="餐饮安排")


class WeatherInfo(BaseModel):
    """天气信息"""

    date: str = Field(..., description="日期")
    day_weather: str = Field(..., description="白天天气")
    night_weather: str = Field(..., description="夜间天气")
    day_temp: int = Field(..., description="白天温度(摄氏度)")
    night_temp: int = Field(..., description="夜间温度(摄氏度)")
    wind_direction: str = Field(..., description="风向")
    wind_power: str = Field(..., description="风力")

    @field_validator("day_temp", "night_temp", mode="before")
    @classmethod
    def parse_temperature(cls, value):
        """解析温度字符串："16°C" -> 16"""
        if isinstance(value, str):
            cleaned = value.replace("°C", "").replace("℃", "").replace("°", "").strip()
            try:
                return int(float(cleaned))
            except ValueError:
                return 0
        return value


class TripPlan(BaseModel):
    """旅行计划"""

    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(default_factory=list, description="每日行程")
    weather_info: List[WeatherInfo] = Field(default_factory=list, description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")


class CritiqueScores(BaseModel):
    """行程质量审视分数。"""

    attraction_diversity: int = Field(..., ge=0, le=10, description="景点类型是否多样")
    description_quality: int = Field(..., ge=0, le=10, description="描述是否具体有参考价值")
    weather_compatibility: int = Field(..., ge=0, le=10, description="天气与景点类型是否匹配")
    schedule_feasibility: int = Field(..., ge=0, le=10, description="每日行程时间是否合理")
    budget_realism: int = Field(..., ge=0, le=10, description="预算是否合理")

    @property
    def average_score(self) -> float:
        values = [
            self.attraction_diversity,
            self.description_quality,
            self.weather_compatibility,
            self.schedule_feasibility,
            self.budget_realism,
        ]
        return round(sum(values) / len(values), 2)


class CritiqueIssue(BaseModel):
    """行程质量问题。"""

    severity: Literal["low", "medium", "high"] = Field(..., description="问题严重程度")
    day: Optional[int] = Field(default=None, ge=0, description="关联天数，从 0 开始；无法定位时为空")
    problem: str = Field(..., min_length=1, description="问题描述")
    suggestion: str = Field(..., min_length=1, description="修正建议")


class CritiqueResult(BaseModel):
    """行程质量审视结果。"""

    scores: CritiqueScores
    issues: List[CritiqueIssue] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    needs_revision: bool = False
    revision_summary: str = ""

    @property
    def average_score(self) -> float:
        return self.scores.average_score


class TripPlanRequest(BaseModel):
    """旅行计划请求"""

    session_id: Optional[str] = Field(default=None, description="会话ID")
    city: str = Field(..., min_length=1, description="目的地城市")
    start_date: str = Field(..., description="开始日期，格式 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期，格式 YYYY-MM-DD")
    days: int = Field(..., ge=1, le=15, description="旅行天数")
    preferences: str = Field(default="经典景点", description="旅行偏好")
    budget: str = Field(default="中等", description="预算等级")
    transportation: str = Field(default="公共交通", description="交通方式")
    accommodation: str = Field(default="经济型酒店", description="住宿类型")

    @field_validator("city", "preferences", "budget", "transportation", "accommodation", mode="before")
    @classmethod
    def sanitize_and_check(cls, value: str) -> str:
        return _sanitize_and_check_text(value)

    @model_validator(mode="after")
    def validate_date_range(self) -> "TripPlanRequest":
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as exc:
            raise ValueError("start_date 和 end_date 必须使用 YYYY-MM-DD 格式") from exc
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        return self


class TripPlanResponse(TripPlan):
    """带状态管理元数据的旅行计划响应"""

    plan_id: str = Field(..., description="计划ID")
    status: str = Field(..., description="计划状态")
    version: int = Field(..., ge=1, description="当前版本号")


TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
TaskPhase = Literal[
    "queued",
    "preparing",
    "collecting_context",
    "draft_planning",
    "critiquing",
    "refining",
    "llm_planning",
    "validating",
    "meal_enrichment",
    "saving",
    "completed",
    "failed",
    "cancelled",
    "expired",
]


class TripPlanTaskCreatedResponse(BaseModel):
    task_id: str
    status: TaskStatus
    status_url: str
    events_url: str
    result_url: str


class TripPlanTaskStatusResponse(BaseModel):
    task_id: str
    status: TaskStatus
    phase: TaskPhase
    progress: int = Field(..., ge=0, le=100)
    message: str
    queued_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str
    elapsed_ms: int = Field(default=0, ge=0)
    phase_elapsed_ms: int = Field(default=0, ge=0)
    phase_timings: dict[str, int] = Field(default_factory=dict)
    result_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class TripPlanTaskPendingResult(BaseModel):
    status: TaskStatus
    message: str


class TripPlanUpdateRequest(BaseModel):
    """旅行计划编辑请求"""

    plan_json: TripPlan = Field(..., description="修改后的完整旅行计划")
    expected_version: int = Field(..., ge=1, description="客户端当前持有的版本号，用于冲突检测")
    change_summary: str = Field(default="手动编辑", description="修改摘要")

    @field_validator("change_summary", mode="before")
    @classmethod
    def sanitize_change_summary(cls, value: str) -> str:
        return _sanitize_and_check_text(value)


class PlanVersionSummary(BaseModel):
    version: int
    change_summary: Optional[str] = None
    created_at: str


class PlanVersionsResponse(BaseModel):
    versions: List[PlanVersionSummary]


class SessionCreateResponse(BaseModel):
    session_id: str
    user_id: str
    created_at: str
    is_new: bool = True


class SessionTripPlanSummary(BaseModel):
    id: str
    city: str
    start_date: str
    end_date: str
    status: str
    version: int
    created_at: str
    updated_at: str


class SessionDetailResponse(BaseModel):
    session_id: str
    user_id: str
    trip_plans: List[SessionTripPlanSummary] = Field(default_factory=list)
    conversation_count: int = 0
    created_at: str
    updated_at: str


class ConversationRequest(BaseModel):
    message: str = Field(..., min_length=1)
    referenced_plan_id: Optional[str] = None
    apply_to_plan: bool = False

    @field_validator("message", mode="before")
    @classmethod
    def sanitize_and_check_message(cls, value: str) -> str:
        return _sanitize_and_check_text(value)


class ConversationMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    tool_calls: Optional[List[dict[str, Any]]] = None
    created_at: str
    plan_updated: bool = False
    plan_update_failed: bool = False


class ConversationResponse(BaseModel):
    message_id: str
    role: str
    content: str
    tool_calls: List[dict[str, Any]] = Field(default_factory=list)
    updated_plan: Optional[TripPlanResponse] = None
    plan_update_failed: bool = False


class ConversationListResponse(BaseModel):
    messages: List[ConversationMessageResponse]
    has_more: bool = False


class UserPreferenceResponse(BaseModel):
    preferred_categories: List[str] = Field(default_factory=list)
    budget_profile: dict[str, Any] = Field(default_factory=dict)
    travel_style: str = ""
    favorite_cities: List[str] = Field(default_factory=list)


class UserPreferenceUpdateRequest(BaseModel):
    preferred_categories: List[str] = Field(default_factory=list)
    budget_profile: dict[str, Any] = Field(default_factory=dict)
    travel_style: str = ""
    favorite_cities: List[str] = Field(default_factory=list)


class SavedItemCreateRequest(BaseModel):
    item_type: Literal["attraction", "hotel", "restaurant", "trip_plan"]
    item_data: dict[str, Any]
    tags: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class SavedItemResponse(SavedItemCreateRequest):
    id: str
    created_at: str
