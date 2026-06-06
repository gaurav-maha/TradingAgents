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

if ! command -v mail >/dev/null 2>&1; then
  echo "mail command not found; summary is available at $SUMMARY_FILE" >&2
  exit 0
fi

BEST_TICKER="$("${TRADINGAGENTS_UV:-uv}" run python - "$SUMMARY_FILE" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
payload_path = summary_path.with_suffix(".json")
try:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
except Exception:
    print("None")
else:
    print(payload.get("best_ticker") or "None")
PY
)"

{
  printf 'TradingAgents universe run finished.\n\n'
  printf 'Best ticker: %s\n' "$BEST_TICKER"
  printf 'Summary path: %s\n\n' "$SUMMARY_FILE"
  cat "$SUMMARY_FILE"
} | mail -s "TradingAgents universe result: ${BEST_TICKER}" "$EMAIL_TO"

echo "Emailed TradingAgents summary to $EMAIL_TO from $SUMMARY_FILE"
