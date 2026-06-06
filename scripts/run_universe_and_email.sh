#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd "$REPO_DIR"

"${TRADINGAGENTS_UV:-uv}" run tradingagents analyze "$@"

RESULTS_ROOT="${TRADINGAGENTS_RESULTS_DIR:-$HOME/.tradingagents/logs}"
SUMMARY_ROOT="$RESULTS_ROOT/universe/nyse_nasdaq_top"
SUMMARY_FILE="$(find "$SUMMARY_ROOT" -name universe_summary.md -type f -print0 2>/dev/null \
  | xargs -0 ls -t 2>/dev/null \
  | head -n 1 || true)"

if [ -z "$SUMMARY_FILE" ]; then
  echo "No universe_summary.md found under $SUMMARY_ROOT; skipping email." >&2
  exit 0
fi

EMAIL_TO="${TRADINGAGENTS_EMAIL_TO:-}"
if [ -z "$EMAIL_TO" ]; then
  echo "TRADINGAGENTS_EMAIL_TO is not set; summary is available at $SUMMARY_FILE"
  exit 0
fi

EMAIL_BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/tradingagents-email.XXXXXX.txt")"
EMAIL_HTML_FILE="$(mktemp "${TMPDIR:-/tmp}/tradingagents-email.XXXXXX.html")"
trap 'rm -f "$EMAIL_BODY_FILE" "$EMAIL_HTML_FILE"' EXIT

BEST_TICKER="$("${TRADINGAGENTS_UV:-uv}" run python - "$SUMMARY_FILE" "$EMAIL_BODY_FILE" "$EMAIL_HTML_FILE" <<'PY'
import html
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
body_path = Path(sys.argv[2])
html_path = Path(sys.argv[3])
payload_path = summary_path.with_suffix(".json")


def html_table(rows, headers):
    if not rows:
        return "<p>None.</p>"
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        body.append(
            "<tr>"
            + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in row)
            + "</tr>"
        )
    return (
        "<table>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
    )


try:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
except Exception:
    markdown = summary_path.read_text(encoding="utf-8")
    plain = (
        "TradingAgents universe run finished.\n\n"
        "Best ticker: None\n\n"
        f"Summary path: {summary_path}\n\n"
        f"{markdown}"
    )
    body_path.write_text(plain, encoding="utf-8")
    html_path.write_text(
        "<html><body>"
        "<h1>TradingAgents Universe Result</h1>"
        "<p><strong>Best ticker:</strong> None</p>"
        f"<p><strong>Summary path:</strong> {html.escape(str(summary_path))}</p>"
        f"<pre>{html.escape(markdown)}</pre>"
        "</body></html>",
        encoding="utf-8",
    )
    print("None")
else:
    best = payload.get("best_ticker") or "None"
    ranked = payload.get("ranked_results") or []
    failed = payload.get("failed_results") or []
    lines = [
        "TradingAgents universe run finished.",
        "",
        f"Best ticker: {best}",
        f"Summary path: {summary_path}",
        "",
        "Top ranked tickers:",
    ]
    if ranked:
        for idx, item in enumerate(ranked[:20], start=1):
            lines.append(
                f"{idx}. {item.get('ticker', '')} | "
                f"{item.get('rating', '')} | score {item.get('score', '')} | "
                f"market cap/assets {item.get('market_cap') or 'n/a'}"
            )
    else:
        lines.append("None.")
    lines.extend(["", "Failed tickers:"])
    if failed:
        for item in failed[:20]:
            lines.append(
                f"- {item.get('ticker', '')}: {item.get('error') or item.get('rating') or 'Error'}"
            )
        if len(failed) > 20:
            lines.append(f"- ... and {len(failed) - 20} more failures")
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "Files:",
            f"- Markdown summary: {summary_path}",
            f"- JSON summary: {payload_path}",
        ]
    )
    body_path.write_text("\n".join(lines), encoding="utf-8")
    ranked_rows = [
        [
            idx,
            item.get("ticker", ""),
            item.get("rating", ""),
            item.get("score", ""),
            item.get("market_cap") or "n/a",
        ]
        for idx, item in enumerate(ranked[:20], start=1)
    ]
    failed_rows = [
        [
            item.get("ticker", ""),
            item.get("error") or item.get("rating") or "Error",
        ]
        for item in failed[:20]
    ]
    more_failures = ""
    if len(failed) > 20:
        more_failures = f"<p>... and {len(failed) - 20} more failures.</p>"
    html_path.write_text(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <style>
      body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111827; line-height: 1.45; }}
      h1 {{ font-size: 22px; margin-bottom: 4px; }}
      h2 {{ font-size: 16px; margin-top: 24px; }}
      .meta {{ color: #4b5563; margin: 0 0 14px; }}
      .best {{ font-size: 18px; font-weight: 700; }}
      table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
      th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; }}
      th {{ background: #f3f4f6; }}
      code {{ background: #f3f4f6; padding: 2px 4px; border-radius: 4px; }}
    </style>
  </head>
  <body>
    <h1>TradingAgents Universe Result</h1>
    <p class="best">Best ticker: {best}</p>
    <p class="meta">Summary path: <code>{summary_path}</code></p>
    <h2>Top ranked tickers</h2>
    {ranked_table}
    <h2>Failed tickers</h2>
    {failed_table}
    {more_failures}
    <h2>Files</h2>
    <p>Markdown summary: <code>{summary_path}</code><br>
    JSON summary: <code>{payload_path}</code></p>
  </body>
</html>
""".format(
            best=html.escape(str(best)),
            summary_path=html.escape(str(summary_path)),
            payload_path=html.escape(str(payload_path)),
            ranked_table=html_table(ranked_rows, ["Rank", "Ticker", "Rating", "Score", "Market Cap / Assets"]),
            failed_table=html_table(failed_rows, ["Ticker", "Error"]),
            more_failures=more_failures,
        ),
        encoding="utf-8",
    )
    print(best)
PY
)"

SUBJECT="TradingAgents universe result: ${BEST_TICKER}"

"${TRADINGAGENTS_UV:-uv}" run python - "$EMAIL_TO" "$SUBJECT" "$EMAIL_BODY_FILE" "$EMAIL_HTML_FILE" "$SUMMARY_FILE" <<'PY'
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

to_address, subject, plain_path, html_path, summary_path = sys.argv[1:]
smtp_host = os.environ.get("TRADINGAGENTS_SMTP_HOST", "smtp.gmail.com")
smtp_port = int(os.environ.get("TRADINGAGENTS_SMTP_PORT", "587"))
smtp_user = os.environ.get("TRADINGAGENTS_SMTP_USER", "")
smtp_password = os.environ.get("TRADINGAGENTS_SMTP_PASSWORD", "")
from_address = os.environ.get("TRADINGAGENTS_EMAIL_FROM", smtp_user)

if not smtp_user or not smtp_password or not from_address:
    raise SystemExit(
        "TRADINGAGENTS_SMTP_USER, TRADINGAGENTS_SMTP_PASSWORD, and "
        "TRADINGAGENTS_EMAIL_FROM must be set for SMTP email."
    )

message = EmailMessage()
message["From"] = from_address
message["To"] = to_address
message["Subject"] = subject
message.set_content(Path(plain_path).read_text(encoding="utf-8"))
message.add_alternative(Path(html_path).read_text(encoding="utf-8"), subtype="html")

with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
    server.starttls()
    server.login(smtp_user, smtp_password)
    server.send_message(message)

print(f"Emailed TradingAgents summary to {to_address} via SMTP from {from_address} using {summary_path}")
PY
