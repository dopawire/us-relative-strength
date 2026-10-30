#!/usr/bin/env bash
# Install the daily cron entries for the US RS pipeline (idempotent).
#
# The US session closes 16:00 New York time; the build runs ~17:30 NY.  Cron
# uses the system time zone and New York shifts with DST, so both the EDT and
# the EST local equivalents are installed — all stale-gated (--if-stale), so
# whichever fires second is a no-op.  Plus an hourly catch-up and @reboot for
# when the machine was off.  CRON_TZ is deliberately NOT used: it would also
# re-time every crontab line after it (other projects' jobs).
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MARK="# us-rs daily build (managed by install_cron.sh)"
. "$DIR/find_python.sh"

# local "MIN HOUR DOW" of Mon–Fri 17:30 New York, for a winter + a summer date
times="$("$PY" - <<'PY'
import datetime as dt
from zoneinfo import ZoneInfo
ny = ZoneInfo("America/New_York")
seen = []
for day in (dt.date(2026, 1, 12), dt.date(2026, 7, 13)):      # Mondays (EST / EDT)
    loc = dt.datetime.combine(day, dt.time(17, 30), ny).astimezone()
    shift = (loc.date() - day).days                             # local day after?
    dows = ",".join(str((d + shift) % 7) for d in range(1, 6))  # cron: 0 = Sunday
    t = f"{loc.minute} {loc.hour} * * {dows}"
    if t not in seen:
        seen.append(t)
print("\n".join(seen))
PY
)"

existing="$(crontab -l 2>/dev/null || true)"
# drop this project's previous block only (its mark + lines running its cron_us.sh)
cleaned="$(grep -vF -e "$DIR/cron_us.sh" -e "$MARK" <<<"$existing" | sed '/^$/d' || true)"

{
    [ -n "$cleaned" ] && echo "$cleaned"
    echo "$MARK"
    while read -r t; do
        echo "$t /bin/bash $DIR/cron_us.sh --if-stale"
    done <<<"$times"
    echo "15 * * * * /bin/bash $DIR/cron_us.sh --if-stale"
    echo "@reboot sleep 420 && /bin/bash $DIR/cron_us.sh --if-stale"
} | crontab -

echo "installed (system TZ: $(date +%Z)):"
crontab -l | sed -n "/$(printf '%s' "$MARK" | sed 's/[^a-zA-Z0-9 ]/./g')/,\$p"
