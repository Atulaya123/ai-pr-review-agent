from backend.core.config import Settings, _langsmith_env_vars


def test_langsmith_disabled_by_default_yields_no_env_vars():
    settings = Settings(langsmith_tracing=False, langsmith_api_key="unused")
    assert _langsmith_env_vars(settings) == {}


def test_langsmith_without_api_key_yields_no_env_vars():
    settings = Settings(langsmith_tracing=True, langsmith_api_key=None)
    assert _langsmith_env_vars(settings) == {}


def test_langsmith_enabled_with_key_yields_both_var_generations():
    settings = Settings(langsmith_tracing=True, langsmith_api_key="fake-key", langsmith_project="my-project")
    env = _langsmith_env_vars(settings)

    assert env["LANGCHAIN_TRACING_V2"] == "true"
    assert env["LANGCHAIN_API_KEY"] == "fake-key"
    assert env["LANGCHAIN_PROJECT"] == "my-project"
    assert env["LANGSMITH_TRACING"] == "true"
    assert env["LANGSMITH_API_KEY"] == "fake-key"
    assert env["LANGSMITH_PROJECT"] == "my-project"
