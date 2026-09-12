#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

START_FILE=.paper_run_start
[ -f "$START_FILE" ] || date +%s > "$START_FILE"
ELAPSED_DAYS=$(( ($(date +%s) - $(cat "$START_FILE")) / 86400 ))

if [ "$ELAPSED_DAYS" -ge 7 ]; then
    echo "=== $(date) : 7 days elapsed, removing cron job ===" >> run.log
    crontab -l | grep -v "run_daily.sh" | crontab -
    exit 0
fi

echo "=== $(date) (day $ELAPSED_DAYS) ===" >> run.log
python bot.py auto >> run.log 2>&1
python bot.py auto --crypto --notional 10 >> run.log 2>&1
python bot.py rebalance >> run.log 2>&1
