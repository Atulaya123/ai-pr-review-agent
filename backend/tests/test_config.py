import os

from backend.core.config import get_settings


def test_langsmith_tracing_disabled_by_default_does_not_export_env(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    get_settings.cache_clear()
    get_settings()
    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGSMITH_TRACING" not in os.environ
    get_settings.cache_clear()


def test_langsmith_tracing_enabled_exports_both_var_generations(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key-123")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
    for key in (
        "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT",
        "LANGSMITH_API_KEY_EXPORTED_MARKER",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.langsmith_tracing is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "test-key-123"
    assert os.environ["LANGCHAIN_PROJECT"] == "test-project"
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "test-key-123"
    assert os.environ["LANGSMITH_PROJECT"] == "test-project"
    get_settings.cache_clear()


def test_langsmith_tracing_without_api_key_does_not_export(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    get_settings.cache_clear()

    get_settings()

    assert "LANGCHAIN_TRACING_V2" not in os.environ
    get_settings.cache_clear()
