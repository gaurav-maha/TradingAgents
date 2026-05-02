"""ChatGPT Pro/Plus OAuth flow (Codex client_id) for TradingAgents.

Mirrors the public Codex CLI auth flow used by opencode's `codex.ts` plugin:
PKCE authorization-code grant against ``auth.openai.com``, with a local
redirect server on ``localhost:1455``. The resulting access token is used
to call ``chatgpt.com/backend-api/codex/responses`` instead of the metered
``api.openai.com`` endpoints, so the user pays via their ChatGPT
subscription rather than per-token API billing.

Token storage lives at ``$TRADINGAGENTS_HOME/auth.json`` (default
``~/.tradingagents/auth.json``) with file mode ``0600``. The schema is
identical in spirit to opencode's ``auth.json``::

    {
      "openai": {
        "type": "oauth",
        "access": "<short-lived JWT>",
        "refresh": "<long-lived refresh token>",
        "expires": 1759180000000,           # Unix ms
        "accountId": "<chatgpt account id, optional>"
      }
    }
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Public Codex CLI client. Same value used by the official Codex CLI and by
# opencode's `codex.ts` — this is a public OAuth client_id, not a secret.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
DEFAULT_REDIRECT_PORT = 1455
DEFAULT_REDIRECT_PATH = "/auth/callback"
SCOPES = "openid profile email offline_access"

# Refresh slightly before the server's stated expiry to avoid races where a
# token is "valid" at request build time but expires mid-flight.
REFRESH_LEEWAY_SECONDS = 60


def _auth_file() -> Path:
    home = os.environ.get("TRADINGAGENTS_HOME")
    if home:
        return Path(home).expanduser() / "auth.json"
    return Path.home() / ".tradingagents" / "auth.json"


def _codex_cli_auth_file() -> Path:
    return Path.home() / ".codex" / "auth.json"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

@dataclass
class PkceCodes:
    verifier: str
    challenge: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> PkceCodes:
    """Generate a PKCE verifier/challenge pair (S256).

    Verifier is 64 URL-safe characters which sits comfortably inside the
    RFC 7636 43-128 range. ``secrets.token_urlsafe`` already returns
    URL-safe characters so no additional sanitisation is required.
    """
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkceCodes(verifier=verifier, challenge=challenge)


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def load_tokens() -> Optional[dict]:
    """Return stored Codex OAuth tokens or ``None`` if not logged in."""
    path = _auth_file()
    if not path.exists():
        return _load_codex_cli_tokens()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return _load_codex_cli_tokens()
    entry = data.get("openai") if isinstance(data, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "oauth":
        return _load_codex_cli_tokens()
    if not parse_jwt_claims(str(entry.get("access") or "")):
        # Ignore malformed/mock entries and recover from the Codex CLI login
        # when available.
        imported = _load_codex_cli_tokens()
        if imported:
            save_tokens(imported)
            return imported
    return entry


def _load_codex_cli_tokens() -> Optional[dict]:
    """Import OAuth tokens from the official Codex CLI auth file if present."""
    if os.environ.get("TRADINGAGENTS_HOME"):
        return None

    path = _codex_cli_auth_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None

    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access or not refresh:
        return None

    claims = parse_jwt_claims(access)
    expires = int(claims.get("exp", 0)) * 1000 if claims.get("exp") else 0
    if not expires:
        expires = int(time.time() * 1000) + 3600 * 1000

    return {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": expires,
        "accountId": tokens.get("account_id") or extract_account_id(tokens) or "",
    }


def save_tokens(entry: dict) -> None:
    """Write Codex tokens to ``auth.json`` with mode 0600.

    Other providers stored under different keys are preserved.
    """
    path = _auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing["openai"] = entry
    # Write through a temp file so a crash mid-write doesn't truncate
    # the user's other provider creds.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def clear_tokens() -> bool:
    """Remove the Codex token entry. Returns True if anything was removed."""
    path = _auth_file()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or "openai" not in data:
        return False
    data.pop("openai", None)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)
    return True


# ---------------------------------------------------------------------------
# JWT id_token parsing (no signature verification — we only read claims that
# the user just received over TLS from auth.openai.com)
# ---------------------------------------------------------------------------

def parse_jwt_claims(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # Add padding back for urlsafe_b64decode
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return {}


def extract_account_id(token_response: dict) -> Optional[str]:
    """Extract the ChatGPT account id from id_token / access_token claims."""
    for key in ("id_token", "access_token"):
        token = token_response.get(key)
        if not token:
            continue
        claims = parse_jwt_claims(token)
        if not claims:
            continue
        account_id = (
            claims.get("chatgpt_account_id")
            or claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        )
        if account_id:
            return account_id
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs:
            first = orgs[0]
            if isinstance(first, dict) and first.get("id"):
                return first["id"]
    return None


# ---------------------------------------------------------------------------
# Token endpoint calls
# ---------------------------------------------------------------------------

def _post_form(url: str, body: dict, timeout: float = 30.0) -> dict:
    encoded = urllib.parse.urlencode(body).encode("ascii")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict:
    return _post_form(
        f"{ISSUER}/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )


def refresh_access_token(refresh_token: str) -> dict:
    return _post_form(
        f"{ISSUER}/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )


def tokens_to_entry(tokens: dict, fallback_refresh: Optional[str] = None) -> dict:
    """Convert a raw token-endpoint response into the on-disk entry shape."""
    expires_in = int(tokens.get("expires_in") or 3600)
    return {
        "type": "oauth",
        "access": tokens["access_token"],
        # OpenAI rotates refresh tokens; if the new response does not include
        # one (some flows don't), keep the old one.
        "refresh": tokens.get("refresh_token") or fallback_refresh or "",
        "expires": int(time.time() * 1000) + expires_in * 1000,
        "accountId": extract_account_id(tokens) or "",
    }


def get_valid_access_token(force_refresh: bool = False) -> str:
    """Return a non-expired access token, refreshing it if necessary.

    Raises ``RuntimeError`` when the user is not logged in or refresh fails.
    """
    entry = load_tokens()
    if not entry:
        raise RuntimeError(
            "Not logged in. Run `tradingagents auth login` to authorize "
            "with your ChatGPT Pro/Plus account."
        )

    expires_ms = int(entry.get("expires") or 0)
    now_ms = int(time.time() * 1000)
    if force_refresh or expires_ms - REFRESH_LEEWAY_SECONDS * 1000 <= now_ms:
        if not entry.get("refresh"):
            raise RuntimeError(
                "Stored Codex token has no refresh token. Re-run "
                "`tradingagents auth login`."
            )
        new_tokens = refresh_access_token(entry["refresh"])
        entry = tokens_to_entry(new_tokens, fallback_refresh=entry.get("refresh"))
        # Preserve previously-recorded accountId if the refresh response
        # didn't carry an id_token.
        if not entry["accountId"] and load_tokens():
            previous = load_tokens() or {}
            entry["accountId"] = previous.get("accountId", "")
        save_tokens(entry)

    return entry["access"]


# ---------------------------------------------------------------------------
# Local redirect server (browser flow)
# ---------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    # Populated by the surrounding login() before serve_forever.
    expected_state: str = ""
    callback_path: str = DEFAULT_REDIRECT_PATH
    result: dict = {}
    completed: threading.Event = threading.Event()

    def do_GET(self):  # noqa: N802 — http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.callback_path:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]

        if error:
            type(self).result = {"error": error}
        elif state != type(self).expected_state:
            type(self).result = {"error": "state_mismatch"}
        elif not code:
            type(self).result = {"error": "missing_code"}
        else:
            type(self).result = {"code": code}

        body = (
            "<html><body style='font-family:sans-serif'>"
            "<h2>TradingAgents</h2>"
            "<p>Authorization received. You can close this window.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).completed.set()

    def log_message(self, format, *args):  # silence default stderr logging
        pass


def login(open_browser: bool = True, timeout: float = 300.0) -> dict:
    """Run the PKCE browser flow and persist tokens. Returns the stored entry.

    Raises ``RuntimeError`` on user denial, state mismatch, or timeout.
    """
    pkce = generate_pkce()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{DEFAULT_REDIRECT_PORT}{DEFAULT_REDIRECT_PATH}"

    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "tradingagents",
    }
    authorize_url = f"{ISSUER}/oauth/authorize?{urllib.parse.urlencode(auth_params)}"

    handler_cls = type(
        "_BoundCallbackHandler",
        (_CallbackHandler,),
        {
            "expected_state": state,
            "callback_path": DEFAULT_REDIRECT_PATH,
            "result": {},
            "completed": threading.Event(),
        },
    )

    with socketserver.TCPServer(("127.0.0.1", DEFAULT_REDIRECT_PORT), handler_cls) as httpd:
        httpd.allow_reuse_address = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        print(f"\nOpen this URL in your browser to authorize:\n  {authorize_url}\n")
        if open_browser:
            try:
                webbrowser.open(authorize_url)
            except Exception:  # pragma: no cover — never block on browser failure
                pass

        if not handler_cls.completed.wait(timeout=timeout):
            httpd.shutdown()
            raise RuntimeError(f"Authorization timed out after {timeout}s.")
        httpd.shutdown()
        thread.join(timeout=2.0)

    result = handler_cls.result
    if "error" in result:
        raise RuntimeError(f"Authorization failed: {result['error']}")

    tokens = exchange_code(result["code"], redirect_uri, pkce.verifier)
    entry = tokens_to_entry(tokens)
    save_tokens(entry)
    return entry
