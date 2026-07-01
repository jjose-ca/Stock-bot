#!/bin/bash
# =============================================================================
#  PUSH LOGS — commits and pushes trade log JSON files to GitHub once daily
#  Runs as a cron job: 5 20 * * 1-5 /bin/bash /root/Stock-bot/push_logs.sh
#  (8:05pm UTC = 4:05pm ET — just after market close)
# =============================================================================

cd /root/Stock-bot || exit 1

git config user.name  "vps-stock-bot"
git config user.email "vps-stock-bot@noreply.github.com"

# Add all JSON trade logs — these are the files worth versioning
git add soxl_intraday_trade_log.json \
        soxl_gate_blocks.json \
        tqqq_gate_blocks.json \
        soxl_trade_log.json \
        trade_log.json \
        soxl_earnings_cache.json \
        earnings_cache.json 2>/dev/null

# Only commit if there are actual changes
git diff --staged --quiet && echo "[$(date)] No changes to push." >> /root/logs/git_sync.log && exit 0

git commit -m "chore: daily trade log update $(date '+%Y-%m-%d') [skip ci]"
git pull --rebase origin master >> /root/logs/git_sync.log 2>&1
git push origin master >> /root/logs/git_sync.log 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Daily log push completed" >> /root/logs/git_sync.log
