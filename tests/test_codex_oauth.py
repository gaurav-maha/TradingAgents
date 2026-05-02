"""Unit tests for the ChatGPT Pro/Plus OAuth integration.

These tests do not contact ``auth.openai.com`` or ``chatgpt.com``. Network
calls are stubbed via monkeypatch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from tradingagents.llm_clients import codex_http, codex_oauth
from tradingagents.llm_clients.factory import create_llm_client


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Auth file isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_auth_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_generate_pkce_is_valid_s256():
    pkce = codex_oauth.generate_pkce()
    assert 43 <= len(pkce.verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pkce.verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert pkce.challenge == expected
    # No padding, URL-safe alphabet only.
    assert "=" not in pkce.challenge
    assert "+" not in pkce.challenge and "/" not in pkce.challenge


def test_generate_pkce_is_unique():
    a = codex_oauth.generate_pkce()
    b = codex_oauth.generate_pkce()
    assert a.verifier != b.verifier
    assert a.challenge != b.challenge


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def _make_entry(**overrides):
    entry = {
        "type": "oauth",
        "access": "access-1",
        "refresh": "refresh-1",
        "expires": int(time.time() * 1000) + 3_600_000,
        "accountId": "acct-123",
    }
    entry.update(overrides)
    return entry


def test_save_and_load_tokens_roundtrip(tmp_path):
    entry = _make_entry()
    codex_oauth.save_tokens(entry)
    path = tmp_path / "auth.json"
    assert path.exists()
    # File mode is 0600 on POSIX.
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = codex_oauth.load_tokens()
    assert loaded == entry


def test_save_tokens_preserves_other_provider_keys(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"anthropic": {"type": "api_key", "key": "sk-ant"}}))
    codex_oauth.save_tokens(_make_entry())
    data = json.loads(path.read_text())
    assert data["anthropic"] == {"type": "api_key", "key": "sk-ant"}
    assert data["openai"]["access"] == "access-1"


def test_load_tokens_returns_none_when_missing():
    assert codex_oauth.load_tokens() is None


def test_load_tokens_returns_none_for_non_oauth_entry(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"openai": {"type": "api_key"}}))
    assert codex_oauth.load_tokens() is None


def test_load_tokens_imports_codex_cli_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_HOME", raising=False)
    monkeypatch.setattr(codex_oauth.Path, "home", lambda: tmp_path)

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    access = _fake_jwt({"exp": int(time.time()) + 3600})
    (codex_dir / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access,
            "refresh_token": "codex-refresh",
            "account_id": "codex-account",
        },
    }))

    loaded = codex_oauth.load_tokens()
    assert loaded["access"] == access
    assert loaded["refresh"] == "codex-refresh"
    assert loaded["accountId"] == "codex-account"
    assert loaded["expires"] > int(time.time() * 1000)


def test_clear_tokens_removes_only_openai(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "openai": _make_entry(),
        "anthropic": {"type": "api_key", "key": "sk"},
    }))
    assert codex_oauth.clear_tokens() is True
    data = json.loads(path.read_text())
    assert "openai" not in data
    assert data["anthropic"]["key"] == "sk"
    assert codex_oauth.clear_tokens() is False


# ---------------------------------------------------------------------------
# JWT claim parsing
# ---------------------------------------------------------------------------

def _fake_jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.sig"


def test_extract_account_id_from_id_token():
    token = _fake_jwt({"chatgpt_account_id": "acct-from-id"})
    assert codex_oauth.extract_account_id({"id_token": token}) == "acct-from-id"


def test_extract_account_id_from_orgs_fallback():
    token = _fake_jwt({"organizations": [{"id": "org-1"}]})
    assert codex_oauth.extract_account_id({"id_token": token}) == "org-1"


def test_extract_account_id_returns_none_when_absent():
    token = _fake_jwt({"sub": "user"})
    assert codex_oauth.extract_account_id({"id_token": token}) is None


# ---------------------------------------------------------------------------
# Token entry conversion / refresh
# ---------------------------------------------------------------------------

def test_tokens_to_entry_falls_back_to_old_refresh():
    raw = {"access_token": "a2", "expires_in": 60}
    entry = codex_oauth.tokens_to_entry(raw, fallback_refresh="r-old")
    assert entry["access"] == "a2"
    assert entry["refresh"] == "r-old"
    assert entry["expires"] > int(time.time() * 1000)


def test_get_valid_access_token_refreshes_when_expired(monkeypatch):
    expired = _make_entry(
        access="old-access",
        refresh="old-refresh",
        expires=int(time.time() * 1000) - 1000,
    )
    codex_oauth.save_tokens(expired)

    calls = []

    def fake_refresh(refresh_token):
        calls.append(refresh_token)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(codex_oauth, "refresh_access_token", fake_refresh)
    token = codex_oauth.get_valid_access_token()
    assert token == "new-access"
    assert calls == ["old-refresh"]
    persisted = codex_oauth.load_tokens()
    assert persisted["access"] == "new-access"
    assert persisted["refresh"] == "new-refresh"
    # accountId from the previous entry is preserved when refresh response
    # has no id_token.
    assert persisted["accountId"] == "acct-123"


def test_get_valid_access_token_skips_refresh_when_fresh(monkeypatch):
    fresh = _make_entry(expires=int(time.time() * 1000) + 3_600_000)
    codex_oauth.save_tokens(fresh)
    monkeypatch.setattr(
        codex_oauth, "refresh_access_token",
        lambda r: pytest.fail("should not refresh fresh token"),
    )
    assert codex_oauth.get_valid_access_token() == fresh["access"]


def test_get_valid_access_token_raises_when_logged_out():
    with pytest.raises(RuntimeError, match="Not logged in"):
        codex_oauth.get_valid_access_token()


# ---------------------------------------------------------------------------
# httpx URL rewriting + auth injection
# ---------------------------------------------------------------------------

def test_rewrite_url_strips_v1_prefix():
    url = httpx.URL("https://api.openai.com/v1/responses")
    assert str(codex_http._rewrite_url(url)) == "https://chatgpt.com/backend-api/codex/responses"


def test_rewrite_url_handles_chat_completions():
    url = httpx.URL("https://api.openai.com/v1/chat/completions")
    rewritten = str(codex_http._rewrite_url(url))
    assert rewritten == "https://chatgpt.com/backend-api/codex/chat/completions"


def test_rewrite_url_passes_through_other_hosts():
    url = httpx.URL("https://api.x.ai/v1/responses")
    assert codex_http._rewrite_url(url) == url


def test_codex_http_client_injects_auth_and_rewrites_url(monkeypatch):
    codex_oauth.save_tokens(_make_entry(access="real-access", accountId="acct-x"))
    monkeypatch.setattr(codex_oauth, "refresh_access_token", lambda r: pytest.fail("no refresh"))

    captured = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["account"] = request.headers.get("ChatGPT-Account-Id")
        captured["originator"] = request.headers.get("originator")
        return httpx.Response(200, json={"ok": True})

    client = codex_http.CodexHTTPClient(transport=httpx.MockTransport(transport_handler))
    resp = client.post(
        "https://api.openai.com/v1/responses",
        json={"model": "gpt-5-codex"},
        headers={"Authorization": "Bearer dummy-sdk-key"},
    )
    assert resp.status_code == 200
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["auth"] == "Bearer real-access"
    assert captured["account"] == "acct-x"
    assert captured["originator"] == "tradingagents"


def test_codex_http_client_moves_system_input_to_instructions(monkeypatch):
    codex_oauth.save_tokens(_make_entry(access="real-access", accountId="acct-x"))
    monkeypatch.setattr(codex_oauth, "refresh_access_token", lambda r: pytest.fail("no refresh"))

    captured = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["stream_payload"] = json.loads(b"".join(request.stream))
        return httpx.Response(200, json={"ok": True})

    client = codex_http.CodexHTTPClient(transport=httpx.MockTransport(transport_handler))
    resp = client.post(
        "https://chatgpt.com/backend-api/codex/responses",
        json={
            "model": "gpt-5.5",
            "input": [
                {"role": "system", "type": "message", "content": "Follow the system prompt."},
                {"role": "user", "type": "message", "content": "SPY"},
            ],
        },
    )

    assert resp.status_code == 200
    assert captured["payload"]["store"] is False
    assert captured["payload"]["instructions"] == "Follow the system prompt."
    assert captured["payload"]["input"] == [
        {"role": "user", "type": "message", "content": "SPY"}
    ]
    assert captured["stream_payload"] == captured["payload"]


def test_codex_http_client_adds_default_instructions_for_user_only_input(monkeypatch):
    """String prompts become user-only Responses input; Codex still requires instructions."""
    codex_oauth.save_tokens(_make_entry(access="real-access", accountId="acct-x"))
    monkeypatch.setattr(codex_oauth, "refresh_access_token", lambda r: pytest.fail("no refresh"))

    captured = {}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["stream_payload"] = json.loads(b"".join(request.stream))
        return httpx.Response(200, json={"ok": True})

    client = codex_http.CodexHTTPClient(transport=httpx.MockTransport(transport_handler))
    resp = client.post(
        "https://chatgpt.com/backend-api/codex/responses",
        json={
            "model": "gpt-5.5",
            "stream": True,
            "input": [
                {"role": "user", "type": "message", "content": "Analyze SPY."},
            ],
        },
    )

    assert resp.status_code == 200
    assert captured["payload"]["store"] is False
    assert captured["payload"]["instructions"] == "You are a helpful AI assistant."
    assert captured["payload"]["input"] == [
        {"role": "user", "type": "message", "content": "Analyze SPY."}
    ]
    assert captured["stream_payload"] == captured["payload"]


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------

def test_factory_builds_codex_client_without_calling_network(monkeypatch):
    """Constructing the codex client must not require login or network."""
    client = create_llm_client(provider="codex", model="gpt-5-codex")
    # Avoid actually instantiating ChatOpenAI here — the openai SDK wants a
    # real api_key shape. Just sanity-check attributes that drove construction.
    assert client.provider == "codex"
    assert client.model == "gpt-5-codex"


def test_codex_llm_uses_streaming_responses(monkeypatch):
    """Codex backend requires streamed /responses calls."""
    codex_oauth.save_tokens(_make_entry())
    llm = create_llm_client(provider="codex", model="gpt-5.5").get_llm()
    assert llm.streaming is True
    assert llm.use_responses_api is True


def test_codex_provider_is_openai_compatible():
    from tradingagents.llm_clients.factory import _OPENAI_COMPATIBLE
    assert "codex" in _OPENAI_COMPATIBLE


def test_codex_models_in_catalog():
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
    assert "codex" in MODEL_OPTIONS
    quick_ids = [model_id for _, model_id in MODEL_OPTIONS["codex"]["quick"]]
    deep_ids = [model_id for _, model_id in MODEL_OPTIONS["codex"]["deep"]]
    assert "gpt-5.5" in quick_ids
    assert "gpt-5.5" in deep_ids


def test_trading_graph_passes_reasoning_effort_for_codex():
    """Codex provider must plumb openai_reasoning_effort like openai does."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = {"llm_provider": "codex", "openai_reasoning_effort": "xhigh"}
    instance = TradingAgentsGraph.__new__(TradingAgentsGraph)
    instance.config = cfg
    kwargs = instance._get_provider_kwargs()
    assert kwargs == {"reasoning_effort": "xhigh"}


def test_get_llm_routes_through_codex_backend(monkeypatch):
    """End-to-end: factory → ChatOpenAI → httpx → Codex backend.

    We replace the sync transport on the http_client with a MockTransport
    and invoke a tiny chat through ChatOpenAI's underlying openai SDK to
    confirm the Authorization header, URL rewrite, and ChatGPT-Account-Id
    header all reach the wire correctly.
    """
    codex_oauth.save_tokens(_make_entry(access="real-access", accountId="acct-x"))
    monkeypatch.setattr(codex_oauth, "refresh_access_token", lambda r: pytest.fail("no refresh"))

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["account"] = request.headers.get("ChatGPT-Account-Id")
        captured["payload"] = json.loads(request.content)
        # Minimal Responses-API SSE reply so ChatOpenAI's streaming path can
        # aggregate a final AIMessage.
        sse = "\n\n".join([
            'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","object":"response","created_at":0,"status":"in_progress","model":"gpt-5-codex","output":[]}}',
            'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"in_progress","content":[]}}',
            'event: response.content_part.added\ndata: {"type":"response.content_part.added","item_id":"msg_1","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}',
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"ok"}',
            'event: response.output_text.done\ndata: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,"content_index":0,"text":"ok"}',
            'event: response.content_part.done\ndata: {"type":"response.content_part.done","item_id":"msg_1","output_index":0,"content_index":0,"part":{"type":"output_text","text":"ok","annotations":[]}}',
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"ok","annotations":[]}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","object":"response","created_at":0,"status":"completed","model":"gpt-5-codex","output":[{"id":"msg_1","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"ok","annotations":[]}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}',
            "data: [DONE]",
        ]) + "\n\n"
        return httpx.Response(
            200,
            content=sse,
            headers={"content-type": "text/event-stream"},
        )

    sync = codex_http.CodexHTTPClient(transport=httpx.MockTransport(handler))
    async_client = codex_http.CodexAsyncHTTPClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    client = create_llm_client(
        provider="codex",
        model="gpt-5-codex",
        http_client=sync,
        http_async_client=async_client,
    )
    llm = client.get_llm()
    result = llm.invoke("hello")
    assert "ok" in (result.content or "")
    assert captured["url"].startswith("https://chatgpt.com/backend-api/codex/")
    assert captured["auth"] == "Bearer real-access"
    assert captured["account"] == "acct-x"
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["store"] is False
