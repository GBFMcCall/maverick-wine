#!/usr/bin/env bash
# Launches the Maverick CPS with the userspace serial bridge in front of it.
#
# This is the ONLY supported way to start Maverick now -- it is not enough to
# just run `wine Maverick.exe`, because COM4 in the Wine prefix has to be a
# live pty held open by bridge/serial_bridge.py, not a direct symlink to the
# radio (see README.md for why).
#
# What this script does, every time it's run:
#   0. Checks the registry value behind fix 1 (Wine upgrades reset it) and
#      re-applies it if needed.
#   1. Checks whether the bridge is already running (by pidfile); if not,
#      starts it in the background. If it IS already running, this is a
#      no-op for the bridge -- reusing one long-lived bridge across many CPS
#      launches is the normal case, not a special one. It survives radio
#      unplug/replug and Maverick opening/closing on its own.
#   2. Waits briefly for COM4 to be ready (near-instant: the bridge creates
#      its upstream pty immediately even if the radio isn't plugged in yet).
#   3. Launches Maverick.exe under Wine, in the foreground.
#
# The bridge is deliberately left running when Maverick exits -- there's no
# real cost to it idling, and it means the next launch is instant. To stop it
# (e.g. before unplugging things for good, or to pick up a different
# --idle-ms), run:  bin/maverick-cps.sh --stop-bridge
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

WINEPREFIX="${MAVERICK_WINEPREFIX:-$HOME/.wine32-maverick}"
WINEARCH=win32
MAVERICK_EXE="${MAVERICK_EXE:-$WINEPREFIX/drive_c/Program Files/Maverick_1.05/Maverick.exe}"
RADIO_DEV="${MAVERICK_RADIO_DEV:-/dev/anytone-cps}"
IDLE_MS="${MAVERICK_IDLE_MS:-4}"   # settled empirically -- see README.md

RUN_DIR="${XDG_RUNTIME_DIR:-/tmp}"
mkdir -p "$RUN_DIR"
PIDFILE="$RUN_DIR/maverick-bridge.pid"
BRIDGE_LOG="$RUN_DIR/maverick-bridge.log"

# Launched from a desktop/Favorites icon there's no visible terminal, so this
# script's own status lines (bridge started/reused/missing-radio) go to a log
# too, not just stdout -- otherwise a launch problem has nowhere to be seen.
exec > >(tee -a "$RUN_DIR/maverick-cps-launcher.log") 2>&1

bridge_is_running() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && grep -q serial_bridge.py "/proc/$pid/cmdline" 2>/dev/null
}

start_bridge() {
    echo "[maverick-cps] starting bridge (idle-ms=$IDLE_MS, downstream=$RADIO_DEV)..."
    python3 "$REPO_DIR/bridge/serial_bridge.py" \
        --downstream "$RADIO_DEV" \
        --upstream-symlink "$WINEPREFIX/dosdevices/com4" \
        --idle-ms "$IDLE_MS" \
        --log "$BRIDGE_LOG" \
        > "$RUN_DIR/maverick-bridge.stdout" 2> "$RUN_DIR/maverick-bridge.stderr" &
    local pid=$!
    disown
    echo "$pid" > "$PIDFILE"
    # wait for the upstream pty to be ready -- should be near-instant
    for _ in $(seq 1 50); do
        [ -L "$WINEPREFIX/dosdevices/com4" ] && return 0
        sleep 0.05
    done
    echo "[maverick-cps] WARNING: COM4 symlink didn't appear within 2.5s -- check $RUN_DIR/maverick-bridge.stderr" >&2
}

if [ "${1:-}" = "--stop-bridge" ]; then
    if bridge_is_running; then
        pid=$(cat "$PIDFILE")
        echo "[maverick-cps] stopping bridge (pid $pid)..."
        kill -TERM "$pid"
    else
        echo "[maverick-cps] bridge is not running"
    fi
    exit 0
fi

# Fix 1 (see README.md) lives in the prefix's registry, and Wine resets it:
# after a Wine upgrade, the first launch refreshes the prefix and puts the
# Ports class (Default) back to "Ports (COM & LPT)", which empties the CPS's
# COM port list again. So re-check it on every launch and re-apply if needed.
# The first `wine` call here is also the one that triggers any pending prefix
# refresh, and it finishes before reg runs, so the check sees the post-refresh
# value.
PORTS_KEY='HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E978-E325-11CE-BFC1-08002BE10318}'

ports_class_value() {
    WINEPREFIX="$WINEPREFIX" WINEARCH="$WINEARCH" WINEDEBUG=-all \
        wine reg query "$PORTS_KEY" /ve 2>/dev/null \
        | sed -n 's/^.*REG_SZ[[:space:]]*//p' | tr -d '\r'
}

ensure_ports_class() {
    local current
    current=$(ports_class_value)
    [ "$current" = "Ports" ] && return 0
    echo "[maverick-cps] Ports class default is \"$current\" (likely reset by a Wine update) -- re-applying fix 1"
    WINEPREFIX="$WINEPREFIX" WINEARCH="$WINEARCH" WINEDEBUG=-all \
        wine reg add "$PORTS_KEY" /ve /t REG_SZ /d "Ports" /f >/dev/null 2>&1
    current=$(ports_class_value)
    if [ "$current" = "Ports" ]; then
        echo "[maverick-cps] fix 1 re-applied"
    else
        echo "[maverick-cps] WARNING: could not set Ports class default (still \"$current\") -- the CPS will show no COM ports" >&2
    fi
}

ensure_ports_class

if bridge_is_running; then
    echo "[maverick-cps] bridge already running (pid $(cat "$PIDFILE")), reusing it"
else
    [ -e "$RADIO_DEV" ] || echo "[maverick-cps] NOTE: $RADIO_DEV not present yet -- radio plugged in / powered on? Starting the bridge anyway; it'll pick the radio up once it appears."
    start_bridge
fi

export WINEPREFIX WINEARCH
exec wine "$MAVERICK_EXE"
