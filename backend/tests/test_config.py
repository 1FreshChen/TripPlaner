from app.config import Settings


def test_settings_planner_backend_defaults_to_pydantic_ai(monkeypatch):
    monkeypatch.delenv("PLANNER_BACKEND", raising=False)

    settings = Settings()

    assert settings.planner_backend == "pydantic_ai"


def test_settings_planner_backend_can_select_openai_tools(monkeypatch):
    monkeypatch.setenv("PLANNER_BACKEND", "openai_tools")

    settings = Settings()

    assert settings.planner_backend == "openai_tools"


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
