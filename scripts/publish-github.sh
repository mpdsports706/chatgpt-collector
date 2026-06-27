#!/usr/bin/env bash
# Publish chatgpt-collector to GitHub (run after: gh auth login)
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
GH="${GH:-/opt/homebrew/bin/gh}"

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "Not logged in. Run: gh auth login -h github.com -p https -w"
  exit 1
fi

USER="$("$GH" api user -q .login)"
REMOTE="https://github.com/${USER}/chatgpt-collector.git"

if ! "$GH" repo view "${USER}/chatgpt-collector" >/dev/null 2>&1; then
  "$GH" repo create chatgpt-collector \
    --public \
    --source=. \
    --remote=origin \
    --description "Local-first ChatGPT web history capture for personal memory hubs" \
    --push
else
  git remote add origin "$REMOTE" 2>/dev/null || git remote set-url origin "$REMOTE"
  git push -u origin main
  git push origin v0.1.0
fi

"$GH" release create v0.1.0 \
  --title "chatgpt-collector v0.1.0" \
  --notes "$(cat <<'EOF'
## chatgpt-collector v0.1.0

Local-first ChatGPT **web** history capture for personal memory hubs.

### Why
- Official export = manual batch
- MCP recorders = IDE sessions only, not chatgpt.com
- This = incremental backfill + watch + hub-ready JSON

### Features
- Headed Google OAuth login with OTP-friendly Enter-to-confirm
- Bearer-aware backend-api client (not DOM scraping)
- SQLite staging with content-hash dedup
- Export to `.chatgpt_web.json` (ChatGPT export shape)

### Limitations
- Rate limits (429) — re-run with delay
- Some stale threads (412) — skipped
- Personal/local use; respect OpenAI ToS

### Quick start
```bash
pip install -e ".[stealth]" && playwright install chromium
chatgpt-collector login
chatgpt-collector backfill --headed
chatgpt-collector export
```

### AI Telemetry Hub
Import via the `chatgpt_web` connector: https://github.com/michaeldriscoll/ai-telemetry-hub/blob/main/docs/CHATGPT_WEB_COLLECTOR.md
EOF
)"

echo "Done: https://github.com/${USER}/chatgpt-collector/releases/tag/v0.1.0"
