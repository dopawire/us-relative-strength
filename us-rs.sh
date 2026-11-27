#!/usr/bin/env bash
# One-click launcher for the US Relative-Strength dashboard.
#
#   bash us-rs.sh            # start (if needed), update stale prices, open the browser
#   bash us-rs.sh --stop     # stop the background server
#   bash us-rs.sh --install  # add "US Relative Strength" to the desktop app menu
#
# First run also creates the Python venv (~/.venvs/us-rs).  The server runs in
# the background on http://127.0.0.1:8001 (US_RS_PORT to change; the NSE
# dashboard uses 8000) and logs to server.log.
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"
PORT="${US_RS_PORT:-8001}"
URL="http://127.0.0.1:$PORT"
PIDFILE="$DIR/.server.pid"
VENV="$HOME/.venvs/us-rs"

say() {   # desktop notification when launched from the app menu, plus stdout
    echo "$1"
    command -v notify-send >/dev/null && notify-send -a "US Relative Strength" "US Relative Strength" "$1" 2>/dev/null || true
}
up() { curl -s -m 2 -o /dev/null "$URL/api/meta"; }

case "${1:-}" in
--install)
    app="$HOME/.local/share/applications/us-relative-strength.desktop"
    mkdir -p "$(dirname "$app")"
    cat > "$app" <<EOF
[Desktop Entry]
Type=Application
Name=US Relative Strength
Comment=GICS sector & industry relative strength vs S&P 500
Exec=/bin/bash "$DIR/us-rs.sh"
Icon=utilities-system-monitor
Terminal=false
Categories=Office;Finance;
Actions=stop;

[Desktop Action stop]
Name=Stop server
Exec=/bin/bash "$DIR/us-rs.sh" --stop
EOF
    command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$app")" 2>/dev/null
    echo "Installed: search \"US Relative Strength\" in your app menu ($app)"
    exit 0 ;;
--stop)
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
        rm -f "$PIDFILE"; say "Server stopped."
    else
        rm -f "$PIDFILE"; say "Server was not running."
    fi
    exit 0 ;;
esac

# ---- first run: Python environment ----
if [ ! -x "$VENV/bin/python" ]; then
    say "First run: setting up Python (one-time, ~1 min)…"
    python3 -m venv "$VENV" \
        && "$VENV/bin/pip" install -q -r requirements.txt -r backend/requirements.txt \
        || { say "Setup failed — see the terminal output."; exit 1; }
fi
. ./find_python.sh

# ---- server (background, survives closing this launcher) ----
if ! up; then
    US_RS_PORT="$PORT" nohup bash run_server.sh >> server.log 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 60); do up && break; sleep 0.5; done
    if ! up; then
        say "Server did not start — see $DIR/server.log"
        exit 1
    fi
fi

# ---- prices: top up in the background when a session is missing ----
if ! "$PY" audit_data.py --stale-only >/dev/null 2>&1; then
    if curl -s -m 5 -X POST "$URL/api/update-prices" | grep -q started; then
        say "Updating to the latest session in the background (~3 min) — the page refreshes its data when done."
    fi
fi

xdg-open "$URL" >/dev/null 2>&1 &
