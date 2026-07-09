from app.agents.prompts import PLANNER_AGENT_PROMPT, PLANNER_AGENT_PROMPT_LEGACY


def test_enhanced_planner_prompt_contains_phase1_quality_rules():
    required_fragments = [
        "经验丰富的当地导游",
        "每天同类景点不超过 2 个",
        "至少覆盖 min(天数 × 2, 5) 个不同景点类别",
        "每个 attraction.description ≥ 80 字",
        "overall_suggestions ≥ 5 条",
        "晴天（晴/多云）",
        "雨天（小雨/中雨/大雨）",
        "不要每天平均分配预算",
        "禁止以下表述",
        "建议每天预留30-60分钟机动时间",
        "北京 2 日",
    ]

    for fragment in required_fragments:
        assert fragment in PLANNER_AGENT_PROMPT


def test_legacy_planner_prompt_keeps_original_basic_requirements():
    assert "你是行程规划专家" in PLANNER_AGENT_PROMPT_LEGACY
    assert "每天安排2-3个景点" in PLANNER_AGENT_PROMPT_LEGACY
