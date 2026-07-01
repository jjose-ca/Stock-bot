#!/bin/bash
# =============================================================================
#  PUSH LOGS — commits VPS trade log JSON files to GitHub once daily
#  Cron: 10 20 * * 1-5  /bin/bash /root/Stock-bot/push_logs.sh
#  Runs at 4:10pm ET (after reconciliation at 4:05pm ET)
# =============================================================================

cd /root/Stock-bot || exit 1

git config user.name  "vps-stock-bot"
git config user.email "vps-stock-bot@noreply.github.com"

# Stage all JSON trade logs
git add soxl_intraday_trade_log.json \
        soxl_gate_blocks.json \
        soxl_swing_gate_blocks.json \
        tqqq_gate_blocks.json \
        soxl_trade_log.json \
        trade_log.json \
        soxl_earnings_cache.json \
        earnings_cache.json 2>/dev/null

# Exit early if nothing changed
if git diff --staged --quiet; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes to push." >> /root/logs/git_sync.log
    exit 0
fi

# Commit the staged changes
git commit -m "chore: daily trade log update $(date '+%Y-%m-%d') [skip ci]"

# Pull remote changes using merge (not rebase) to avoid unstaged-changes error
# --no-edit accepts the merge commit message automatically
git pull --no-rebase --no-edit origin master >> /root/logs/git_sync.log 2>&1

# Push to GitHub
git push origin master >> /root/logs/git_sync.log 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Daily log push completed." >> /root/logs/git_sync.log
