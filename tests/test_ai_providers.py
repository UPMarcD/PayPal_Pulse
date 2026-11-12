"""Unit tests for Multi-Provider AI Copilot Dispatcher (OpenAI, Gemini, xAI, Anthropic, Groq, Ollama)."""

import json
from unittest.mock import MagicMock, patch
import httpx
import pytest

from pulse.ai_copilot import _call_groq_chat, ask_copilot, verify_groq_connection
from pulse.config import PulseSettings


def test_provider_auto_detection() -> None:
    """Verify that PulseSettings correctly infers provider, base URL, and model from keys."""
    # Groq
    s_groq = PulseSettings(groq_api_key="gsk_test")
    assert s_groq.active_ai_provider == "groq"
    assert s_groq.active_ai_base_url == "https://api.groq.com/openai/v1"
    assert "llama" in s_groq.active_ai_model

    # OpenAI
    s_openai = PulseSettings(openai_api_key="sk-proj-test")
    assert s_openai.active_ai_provider == "openai"
    assert s_openai.active_ai_base_url == "https://api.openai.com/v1"
    assert "gpt" in s_openai.active_ai_model

    # Gemini
    s_gemini = PulseSettings(gemini_api_key="AIzaSyTest")
    assert s_gemini.active_ai_provider == "gemini"
    assert "googleapis.com" in s_gemini.active_ai_base_url
    assert "gemini" in s_gemini.active_ai_model

    # xAI Grok
    s_xai = PulseSettings(xai_api_key="xai-test-key")
    assert s_xai.active_ai_provider == "xai"
    assert "api.x.ai" in s_xai.active_ai_base_url
    assert "grok" in s_xai.active_ai_model

    # Anthropic
    s_ant = PulseSettings(anthropic_api_key="sk-ant-test")
    assert s_ant.active_ai_provider == "anthropic"
    assert "anthropic.com" in s_ant.active_ai_base_url
    assert "claude" in s_ant.active_ai_model

    # Ollama
    s_ollama = PulseSettings(ai_provider="ollama")
    assert s_ollama.active_ai_provider == "ollama"
    assert "11434" in s_ollama.active_ai_base_url
    assert s_ollama.has_ai_credentials is True


def test_openai_compatible_mock_chat() -> None:
    """Verify that OpenAI-compatible endpoint is called with correct headers and payload."""
    settings = PulseSettings(openai_api_key="sk-mock-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Our net take-home is $5,545.44 after fees.",
            }
        }],
        "usage": {"completion_tokens": 12, "completion_time": 0.05},
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        ans, model, latency, tps, tools = _call_groq_chat(
            messages=[{"role": "user", "content": "What is our net margin?"}],
            settings=settings,
        )
        assert "5,545.44" in ans
        assert "gpt-4o-mini" in model
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "api.openai.com" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer sk-mock-key"


def test_anthropic_messages_mock_chat() -> None:
    """Verify that Anthropic Messages API translates messages and headers properly."""
    settings = PulseSettings(anthropic_api_key="sk-ant-mock-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": "Claude verified: Healthy 84/100 score."}],
        "usage": {"output_tokens": 8},
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        ans, model, latency, tps, tools = _call_groq_chat(
            messages=[
                {"role": "system", "content": "You are Pulse."},
                {"role": "user", "content": "How is our store health?"},
            ],
            settings=settings,
        )
        assert "Claude verified" in ans
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "api.anthropic.com/v1/messages" in args[0]
        assert kwargs["headers"]["x-api-key"] == "sk-ant-mock-key"
        assert kwargs["json"]["system"] == "You are Pulse."


def test_anthropic_tool_use_translation() -> None:
    """Verify that OpenAI tool schemas are translated to Anthropic input_schema and tool_use blocks return as tool_calls."""
    from pulse.ai_copilot import PAYPAL_COPILOT_TOOLS

    settings = PulseSettings(anthropic_api_key="sk-ant-mock-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01A09q90tc1q9",
                "name": "create_paypal_order",
                "input": {"amount": 29.99, "currency": "USD", "description": "Pro Tier"},
            }
        ],
        "usage": {"output_tokens": 15},
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        ans, model, latency, tps, tool_calls = _call_groq_chat(
            messages=[{"role": "user", "content": "Create an order for 29.99 USD"}],
            settings=settings,
            tools=PAYPAL_COPILOT_TOOLS,
        )
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "create_paypal_order"
        args_parsed = json.loads(tc["function"]["arguments"])
        assert args_parsed["amount"] == 29.99
        assert args_parsed["currency"] == "USD"

        # Check request schema translation
        post_args, post_kwargs = mock_post.call_args
        sent_tools = post_kwargs["json"]["tools"]
        assert len(sent_tools) > 0
        first_tool = sent_tools[0]
        assert "input_schema" in first_tool
        assert "name" in first_tool


def test_provider_model_cache_isolation() -> None:
    """Verify that model cache does not leak across different providers."""
    s_openai = PulseSettings(openai_api_key="sk-openai-key")
    s_ant = PulseSettings(anthropic_api_key="sk-ant-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"completion_tokens": 2},
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        _call_groq_chat([{"role": "user", "content": "test"}], settings=s_openai)
        sent_model_openai = mock_post.call_args[1]["json"]["model"]
        assert "gpt" in sent_model_openai

    mock_ant_resp = MagicMock()
    mock_ant_resp.status_code = 200
    mock_ant_resp.json.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"output_tokens": 2},
    }

    with patch("httpx.Client.post", return_value=mock_ant_resp) as mock_post:
        _call_groq_chat([{"role": "user", "content": "test"}], settings=s_ant)
        sent_model_ant = mock_post.call_args[1]["json"]["model"]
        assert "claude" in sent_model_ant
        assert "gpt" not in sent_model_ant


def test_verify_connection_provider_isolation() -> None:
    """Verify that verify_groq_connection does not pollute Groq active model when verifying other providers."""
    from pulse.ai_copilot import _ACTIVE_AI_MODELS, _ACTIVE_GROQ_MODEL, get_active_groq_model

    s_ant = PulseSettings(anthropic_api_key="sk-ant-test")
    s_groq = PulseSettings(groq_api_key="gsk-test")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.Client.post", return_value=mock_resp):
        res = verify_groq_connection(s_ant)
        assert res.connected is True
        assert "ANTHROPIC" in res.model
        # Groq active model must NOT be overwritten by Claude
        groq_model = get_active_groq_model(s_groq)
        assert "claude" not in groq_model
        assert "llama" in groq_model
