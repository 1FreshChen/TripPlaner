from app.config import Settings


def test_settings_use_enhanced_prompt_defaults_to_true(monkeypatch):
    monkeypatch.delenv("USE_ENHANCED_PROMPT", raising=False)

    settings = Settings()

    assert settings.use_enhanced_prompt is True


def test_settings_use_enhanced_prompt_can_be_disabled(monkeypatch):
    monkeypatch.setenv("USE_ENHANCED_PROMPT", "false")

    settings = Settings()

    assert settings.use_enhanced_prompt is False


def test_settings_enable_llm_tool_planning_defaults_to_true(monkeypatch):
    monkeypatch.delenv("ENABLE_LLM_TOOL_PLANNING", raising=False)

    settings = Settings()

    assert settings.enable_llm_tool_planning is True


def test_settings_enable_llm_tool_planning_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_LLM_TOOL_PLANNING", "false")

    settings = Settings()

    assert settings.enable_llm_tool_planning is False


def test_settings_enable_plan_critique_defaults_to_true(monkeypatch):
    monkeypatch.delenv("ENABLE_PLAN_CRITIQUE", raising=False)

    settings = Settings()

    assert settings.enable_plan_critique is True


def test_settings_enable_plan_critique_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_PLAN_CRITIQUE", "false")

    settings = Settings()

    assert settings.enable_plan_critique is False


def test_settings_refinement_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("MAX_REFINEMENT_ROUNDS", "2")
    monkeypatch.setenv("MIN_PASS_SCORE", "8.5")

    settings = Settings()

    assert settings.max_refinement_rounds == 2
    assert settings.min_pass_score == 8.5


def test_settings_baidu_map_api_key_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("BAIDU_MAP_API_KEY", raising=False)
    monkeypatch.setenv("BAIDU_MAP_API_KEY", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.baidu_map_api_key == ""


def test_settings_baidu_map_api_key_can_be_configured(monkeypatch):
    monkeypatch.setenv("BAIDU_MAP_API_KEY", "baidu-test-key")

    settings = Settings()

    assert settings.baidu_map_api_key == "baidu-test-key"
