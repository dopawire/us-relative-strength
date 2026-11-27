#!/usr/bin/env bash
# Daily automated universe refresh + build + audit (cron entry point).
#
# Scheduled by install_cron.sh at ~17:30 New York time (after the 16:00 close,
# once Yahoo's EOD bars settle) plus an hourly stale-gated catch-up.
# Named cron_us.sh, not cron_daily.sh, on purpose: the NSE project's
# install_cron.sh drops every crontab line mentioning "cron_daily.sh".
#
# Optional Telegram failure alerts: put TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
# in ~/.config/us-rs/notify.env (free Bot API; set TELEGRAM_NOTIFY_OK=1 to
# also get success pings).
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

. ./find_python.sh

# exported (set -a): send_brief.py reads the Telegram settings too
if [ -f "$HOME/.config/us-rs/notify.env" ]; then
    set -a; . "$HOME/.config/us-rs/notify.env"; set +a
fi

notify() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--if-stale" ]; then
    if "$PY" audit_data.py --stale-only >/dev/null 2>&1; then
        echo "[$(date '+%F %T')] cron retry: data already current — skipping" >> run.log
        exit 0
    fi
    echo "[$(date '+%F %T')] cron: data stale — rebuilding" >> run.log
fi

# new listings / renames / delistings first (best effort — the build still
# runs on the existing master if the listing sources are unreachable)
echo "[$(date '+%F %T')] refresh_universe.py" >> run.log
timeout 1800 "$PY" refresh_universe.py >> run.log 2>&1 \
    || echo "[$(date '+%F %T')] universe refresh failed (rc=$?) — continuing" >> run.log

# upcoming earnings dates + priced IPOs (best effort)
timeout 300 "$PY" refresh_events.py >> run.log 2>&1 || true

bash run_daily.sh
build_rc=$?
"$PY" audit_data.py >> run.log 2>&1
audit_rc=$?

if [ $build_rc -ne 0 ] || [ $audit_rc -ne 0 ]; then
    notify "⚠️ us-rs daily issue (build=$build_rc audit=$audit_rc) — see run.log"
    exit 1
fi
# evening brief + watchlist alerts → Telegram (no-op without a bot token;
# once per session)
"$PY" send_brief.py >> run.log 2>&1 || notify "⚠️ us-rs brief failed — see run.log"

if [ "${TELEGRAM_NOTIFY_OK:-0}" = "1" ]; then
    notify "✅ us-rs daily build + audit OK ($(date '+%F %T'))"
fi

# optional: publish the snapshot to the public site (PUBLISH_SITE=1)
if [ "${PUBLISH_SITE:-0}" = "1" ]; then
    if /bin/bash publish_site.sh >> run.log 2>&1; then
        echo "[$(date '+%F %T')] site published" >> run.log
    else
        notify "⚠️ us-rs site publish failed — see run.log"
    fi
fi
