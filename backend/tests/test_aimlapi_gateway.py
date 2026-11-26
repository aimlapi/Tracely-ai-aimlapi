"""AI/ML API as a second LLM gateway, next to OpenRouter.

A workspace opts in by saving its key with `provider: "aimlapi"`; a deployment by setting
`AIMLAPI_API_KEY` with no `OPENROUTER_API_KEY`. Everyone else must route exactly as before, and an
AI/ML API key must only ever reach `api.aimlapi.com`, never OpenRouter or a redirect target.
"""

from __future__ import annotations

import types

import httpx
import pytest

from tracely.config import settings
from tracely.infrastructure.db import repositories as repo_module
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.embeddings import Embedder, embeddings_enabled


class _DeadRedis:
    def _boom(self, *a, **kw):
        raise ConnectionError("redis down")

    get = setex = delete = _boom


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(provider, "_redis", lambda: _DeadRedis())
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "aimlapi_api_key", "")
    monkeypatch.setattr(settings, "llm_judge_api_key", "")
    monkeypatch.setattr(settings, "require_project_llm_key", False)
    monkeypatch.setattr(settings, "secrets_encryption_key", "y" * 40)


def _project(monkeypatch, plain: str, llm_provider: str | None):
    token = provider.encrypt_project_key(plain)
    monkeypatch.setattr(
        repo_module,
        "project_get",
        lambda s, pid: types.SimpleNamespace(
            openrouter_api_key_encrypted=token, llm_provider=llm_provider
        ),
    )


# ── routing ───────────────────────────────────────────────────────────────────


def test_aimlapi_workspace_routes_to_exact_host(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    with provider.use_project_key("p1"):
        assert provider.effective_gateway() == "aimlapi"
        m = provider.get_chat_model("openai/gpt-6-luna")
        assert m.openai_api_base == "https://api.aimlapi.com/v1"
        assert m.openai_api_key.get_secret_value() == "aiml-workspace-key"
        assert m.model_name == "openai/gpt-6-luna"
    assert provider.effective_gateway() == "openrouter"


def test_existing_openrouter_workspace_unchanged(monkeypatch):
    """A key saved before this change has `llm_provider` NULL: still OpenRouter."""
    _project(monkeypatch, "sk-or-theirs", None)
    with provider.use_project_key("p1"):
        assert provider.effective_gateway() == "openrouter"
        m = provider.get_chat_model()
        assert m.openai_api_base == settings.openrouter_base_url
        assert "aimlapi" not in str(m.openai_api_base)


def test_server_openrouter_key_wins_over_aimlapi_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-server")
    monkeypatch.setattr(settings, "aimlapi_api_key", "aiml-server")
    assert provider.effective_gateway() == "openrouter"
    assert provider.effective_openrouter_key() == "sk-or-server"
    assert provider.get_chat_model().openai_api_base == settings.openrouter_base_url


def test_server_aimlapi_key_alone_routes_to_aimlapi(monkeypatch):
    monkeypatch.setattr(settings, "aimlapi_api_key", "aiml-server")
    assert provider.llm_enabled()
    assert provider.get_chat_model().openai_api_base == "https://api.aimlapi.com/v1"
    with provider.use_server_key():
        assert provider.effective_gateway() == "aimlapi"
        assert provider.effective_openrouter_key() == "aiml-server"


def test_hosted_gate_ignores_server_aimlapi_key(monkeypatch):
    monkeypatch.setattr(settings, "aimlapi_api_key", "aiml-server")
    monkeypatch.setattr(settings, "require_project_llm_key", True)
    assert not provider.llm_enabled()
    assert provider.effective_openrouter_key() == ""


def test_aimlapi_key_never_sent_to_openrouter_catalog(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    seen: dict = {}

    def fake_get(url, headers=None, **kw):
        seen.update(url=url, headers=headers or {})
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(provider, "_models_cache", {"ts": 0.0, "by_id": None})
    with provider.use_project_key("p1"):
        provider._openrouter_models()
    assert "openrouter.ai" in seen["url"]
    assert "Authorization" not in seen["headers"]


def test_decision_models_refuse_aimlapi_key(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: pytest.fail("key sent to Decisions API"))
    with provider.use_project_key("p1"):
        with pytest.raises(RuntimeError, match="OpenRouter"):
            provider.run_decision({}, {"q": {"type": "noul"}}, model="typesafe/jev-1.13")
        assert provider.default_column_model_id() == provider.default_model_id()


def test_embeddings_follow_the_gateway(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    with provider.use_project_key("p1"):
        assert embeddings_enabled()
        e = Embedder()
        assert e.base_url == "https://api.aimlapi.com/v1"
        assert e.api_key == "aiml-workspace-key"


# ── key and redirects: only api.aimlapi.com ────────────────────────────────────


def _mock_transport(calls: list, redirect_to: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if redirect_to and request.url.host == "api.aimlapi.com":
            return httpx.Response(307, headers={"location": redirect_to})
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    return httpx.MockTransport(handler)


def _patch_clients(monkeypatch, transport):
    """Keep the real clients' redirect policy, swap only the network transport."""
    sync_client, _ = provider.aimlapi_http_clients()
    monkeypatch.setattr(sync_client, "_transport", transport)


def test_chat_request_carries_key_only_to_aimlapi(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    calls: list[httpx.Request] = []
    _patch_clients(monkeypatch, _mock_transport(calls))
    with provider.use_project_key("p1"):
        out = provider.get_chat_model("openai/gpt-4o-mini").invoke("hi")
    assert out.content == "ok"
    assert [str(c.url) for c in calls] == ["https://api.aimlapi.com/v1/chat/completions"]
    assert calls[0].headers["authorization"] == "Bearer aiml-workspace-key"


def test_redirect_off_aimlapi_does_not_carry_key(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    calls: list[httpx.Request] = []
    _patch_clients(monkeypatch, _mock_transport(calls, redirect_to="https://evil.example/v1/x"))
    monkeypatch.setattr(provider.settings, "llm_max_output_tokens", 16)
    with provider.use_project_key("p1"):
        m = provider.get_chat_model("openai/gpt-4o-mini")
        m.max_retries = 0
        with pytest.raises(Exception):
            m.invoke("hi")
    assert calls and all(c.url.host == "api.aimlapi.com" for c in calls)


def test_aimlapi_clients_do_not_follow_redirects():
    sync_client, async_client = provider.aimlapi_http_clients()
    assert sync_client.follow_redirects is False
    assert async_client.follow_redirects is False


# ── model list ────────────────────────────────────────────────────────────────


def test_list_models_uses_aimlapi_chat_catalog(monkeypatch):
    _project(monkeypatch, "aiml-workspace-key", "aimlapi")
    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {
                        "id": "openai/gpt-6-luna",
                        "type": "openai/chat-completions",
                        "info": {"name": "GPT-6 Luna", "contextLength": 1050000},
                    },
                    {
                        "id": "anthropic/claude-sonnet-5",
                        "type": "anthropic/messages",
                        "info": {"name": "Claude Sonnet 5"},
                    },
                ]
            }

    def fake_get(url, headers=None, **kw):
        seen.update(url=url, headers=headers, params=kw.get("params"))
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(provider, "_aimlapi_models_cache", {"ts": 0.0, "by_id": None})
    with provider.use_project_key("p1"):
        models = provider.list_models()
    assert seen["url"] == "https://api.aimlapi.com/v1/models"
    assert seen["params"] == {"include": "all"}
    assert not seen["headers"]  # public catalog: no key
    assert [m["id"] for m in models] == ["openai/gpt-6-luna"]
    assert models[0]["context_tokens"] == 1050000
    assert all(m["kind"] == "llm" for m in models)


# ── persistence ───────────────────────────────────────────────────────────────


def test_repo_writes_provider_with_key_and_clears_it_with_key():
    proj = types.SimpleNamespace(openrouter_api_key_encrypted=None, llm_provider=None)

    class _S:
        def get(self, _model, _pid):
            return proj

        def commit(self):
            pass

    assert repo_module.project_set_openrouter_key(_S(), "p1", "enc", "aimlapi")
    assert (proj.openrouter_api_key_encrypted, proj.llm_provider) == ("enc", "aimlapi")
    assert repo_module.project_set_openrouter_key(_S(), "p1", "enc2")
    assert proj.llm_provider is None
    repo_module.project_set_openrouter_key(_S(), "p1", None, "aimlapi")
    assert proj.llm_provider is None


def test_llm_key_body_defaults_to_openrouter_and_rejects_unknown_gateways():
    from pydantic import ValidationError

    from tracely.api.routers.admin import OpenRouterKeyIn

    assert OpenRouterKeyIn(api_key="k").provider == "openrouter"
    assert OpenRouterKeyIn(api_key="k", provider="aimlapi").provider == "aimlapi"
    with pytest.raises(ValidationError):
        OpenRouterKeyIn(api_key="k", provider="evil.example")
