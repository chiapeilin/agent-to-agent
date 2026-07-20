#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 背景 agent 與 router 使用相同設定。
if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o}"

cd "$ROOT_DIR"

for port in 8001 8002 8003 8004; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "✗ Port $port is already in use. Stop the existing agent before starting a new one." >&2
    exit 1
  fi
done

uv run python -m code_review_agent > /tmp/code_review_agent.log 2>&1 &
uv run python -m translation_agent > /tmp/translation_agent.log 2>&1 &
uv run python -m uppercase_agent > /tmp/uppercase_agent.log 2>&1 &
uv run python -m image_analyzer_agent > /tmp/image_analyzer_agent.log 2>&1 &

sleep 2

echo "Started built-in A2A agents:"
echo "  - Code Review Agent: http://127.0.0.1:8001"
echo "  - Translation Agent: http://127.0.0.1:8002"
echo "  - Uppercase Agent: http://127.0.0.1:8003"
echo "  - Image Analyzer Agent: http://127.0.0.1:8004"
echo "Logs:"
echo "  - /tmp/code_review_agent.log"
echo "  - /tmp/translation_agent.log"
echo "  - /tmp/uppercase_agent.log"
echo "  - /tmp/image_analyzer_agent.log"
