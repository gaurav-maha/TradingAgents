"""``httpx`` clients that route OpenAI SDK calls through Codex with OAuth.

The OpenAI Python SDK accepts an ``http_client`` (sync) and
``http_async_client`` (async) — both ``httpx.Client``/``AsyncClient``
instances. langchain-openai's ``ChatOpenAI`` forwards both kwargs through.
We subclass each one so that, on every outgoing request, we:

  1. Refresh the OAuth access token if it has expired (or is about to).
  2. Strip the SDK's placeholder ``Authorization`` header.
  3. Set ``Authorization: Bearer <real access token>``.
  4. Set ``ChatGPT-Account-Id`` and ``originator`` headers expected by the
     Codex backend.
  5. Defensively rewrite any ``api.openai.com`` URL the SDK might construct
     to the equivalent ``chatgpt.com/backend-api/codex`` URL. With a
     correctly-configured ``base_url`` this is a no-op, but the SDK has
     historically reconstructed URLs in unexpected places (admin/files
     endpoints, etc.) so it is cheap insurance.

This mirrors opencode's ``codex.ts`` ``auth.loader`` fetch interceptor.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx

from . import codex_oauth

# Note: trailing slash is significant for httpx URL joining.
CODEX_BASE = "https://chatgpt.com/backend-api/codex"


def _rewrite_url(url: httpx.URL) -> httpx.URL:
    """Rewrite ``api.openai.com/v1/...`` → ``chatgpt.com/backend-api/codex/...``.

    Only the ``/v1`` prefix is stripped; everything after it is preserved.
    Hosts other than ``api.openai.com`` are returned unchanged so that any
    third-party endpoints the user has configured still work.
    """
    if url.host != "api.openai.com":
        return url
    path = url.path or ""
    if path.startswith("/v1/"):
        path = path[len("/v1"):]
    return url.copy_with(scheme="https", host="chatgpt.com", port=None, path=f"/backend-api/codex{path}")


def _apply_auth(request: httpx.Request) -> None:
    access = codex_oauth.get_valid_access_token()
    entry = codex_oauth.load_tokens() or {}

    # Strip whatever the SDK put there (typically "Bearer <dummy api key>").
    request.headers.pop("authorization", None)
    request.headers.pop("Authorization", None)
    request.headers["Authorization"] = f"Bearer {access}"

    account_id = entry.get("accountId")
    if account_id:
        request.headers["ChatGPT-Account-Id"] = account_id
    request.headers.setdefault("originator", "tradingagents")
    # Some Codex endpoints require a user-agent that looks like a CLI.
    request.headers.setdefault("User-Agent", "tradingagents-codex/0.1")

    new_url = _rewrite_url(request.url)
    if new_url != request.url:
        request.url = new_url


def _message_text(message: dict) -> str:
    """Extract plain text from an OpenAI Responses input message."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _adapt_responses_payload(request: httpx.Request) -> None:
    """Move system messages into ``instructions`` for the Codex backend.

    The public OpenAI Responses API accepts system messages in ``input``.
    ``chatgpt.com/backend-api/codex/responses`` requires a top-level
    ``instructions`` field instead. LangChain emits the public shape, so the
    Codex httpx shim normalizes it immediately before the request is sent.
    """
    if request.method.upper() != "POST" or not request.url.path.endswith("/responses"):
        return

    try:
        payload = json.loads(request.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(payload, dict):
        return

    payload["store"] = False

    if payload.get("instructions"):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request.headers["content-length"] = str(len(body))
        request._content = body
        request.stream = httpx.ByteStream(body)
        return

    input_items = payload.get("input")
    if not isinstance(input_items, list):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request.headers["content-length"] = str(len(body))
        request._content = body
        request.stream = httpx.ByteStream(body)
        return

    instructions = []
    remaining = []
    for item in input_items:
        if isinstance(item, dict) and item.get("role") in {"system", "developer"}:
            text = _message_text(item)
            if text:
                instructions.append(text)
            continue
        remaining.append(item)

    if not instructions:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request.headers["content-length"] = str(len(body))
        request._content = body
        request.stream = httpx.ByteStream(body)
        return

    payload["instructions"] = "\n\n".join(instructions)
    payload["input"] = remaining
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request.headers["content-length"] = str(len(body))
    request._content = body
    request.stream = httpx.ByteStream(body)


class CodexHTTPClient(httpx.Client):
    """Sync httpx client that injects Codex OAuth on every request."""

    def send(self, request: httpx.Request, *args, **kwargs):  # type: ignore[override]
        _adapt_responses_payload(request)
        _apply_auth(request)
        try:
            return super().send(request, *args, **kwargs)
        except httpx.HTTPStatusError as exc:
            # On 401 we force a refresh once and retry. The retry path is
            # important because access tokens occasionally get invalidated
            # on the server side before their stated expiry (e.g. password
            # change, sub renewal).
            if exc.response.status_code == 401:
                codex_oauth.get_valid_access_token(force_refresh=True)
                _apply_auth(request)
                return super().send(request, *args, **kwargs)
            raise


class CodexAsyncHTTPClient(httpx.AsyncClient):
    """Async variant of :class:`CodexHTTPClient`."""

    async def send(self, request: httpx.Request, *args, **kwargs):  # type: ignore[override]
        _adapt_responses_payload(request)
        _apply_auth(request)
        try:
            return await super().send(request, *args, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                codex_oauth.get_valid_access_token(force_refresh=True)
                _apply_auth(request)
                return await super().send(request, *args, **kwargs)
            raise


def build_sync_client(timeout: Optional[float] = 600.0) -> CodexHTTPClient:
    return CodexHTTPClient(timeout=timeout)


def build_async_client(timeout: Optional[float] = 600.0) -> CodexAsyncHTTPClient:
    return CodexAsyncHTTPClient(timeout=timeout)
