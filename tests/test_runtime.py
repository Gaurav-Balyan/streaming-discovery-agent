"""ADR-003: OpenAI Agents SDK + Groq (Chat Completions), the SDK's OpenAI trace export switched off."""
import pytest


def test_importing_the_runtime_disables_sdk_tracing():
    from agents.tracing import get_trace_provider
    from discovery_agent import runtime  # noqa: F401
    provider = get_trace_provider()
    assert getattr(provider, "_disabled", None) is True


def test_the_model_is_groq_through_chat_completions(monkeypatch):
    from agents import OpenAIChatCompletionsModel
    from discovery_agent import config, runtime
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(config, "GROQ_MODEL", "test-model")
    m = runtime.model()
    assert isinstance(m, OpenAIChatCompletionsModel)
    assert str(m._client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    assert m.model == "test-model"


def test_no_model_without_configuration(monkeypatch):
    from discovery_agent import config, runtime
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        runtime.model()


def test_the_agent_builds_with_no_tools_in_phase_1(monkeypatch):
    from discovery_agent import config, runtime
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(config, "GROQ_MODEL", "test-model")
    a = runtime.discovery_agent()
    assert a.name == "discovery" and a.tools == [] and "untrusted" in a.instructions
