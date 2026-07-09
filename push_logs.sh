#!/bin/bash
cd /root/Stock-bot || exit 1
DISCORD_URL="${DISCORD_URL:-}"
git config core.fileMode false
git config user.name  "vps-stock-bot"
git config user.email "vps-stock-bot@noreply.github.com"
git add soxl_intraday_trade_log.json soxl_gate_blocks.json soxl_swing_gate_blocks.json tqqq_gate_blocks.json soxl_trade_log.json trade_log.json soxl_earnings_cache.json earnings_cache.json tqqq_intraday_trade_log.json tqqq_intraday_rejections.jsonl 2>/dev/null
if git diff --staged --quiet; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes to push." >> /root/logs/git_sync.log
    exit 0
fi
git commit -m "chore: daily trade log update $(date +%Y-%m-%d) [skip ci]"
git pull --rebase origin master >> /root/logs/git_sync.log 2>&1
GIT_PUSH_OUTPUT=$(git push origin master 2>&1)
GIT_EXIT_CODE=$?
if [ $GIT_EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Daily log push completed." >> /root/logs/git_sync.log
    echo "$GIT_PUSH_OUTPUT" >> /root/logs/git_sync.log
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: git push FAILED." >> /root/logs/git_sync.log
    echo "$GIT_PUSH_OUTPUT" >> /root/logs/git_sync.log
    CLEANED_ERROR=$(echo "$GIT_PUSH_OUTPUT" | tr '"' "'")
    if [ -n "$DISCORD_URL" ]; then
    curl -s -X POST "$DISCORD_URL" -H "Content-Type: application/json" -d '{"content": "🚨 VPS push_logs.sh failed! Check /root/logs/git_sync.log"}' >> /root/logs/git_sync.log 2>&1
    fi
fi
