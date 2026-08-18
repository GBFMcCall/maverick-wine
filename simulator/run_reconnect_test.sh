#!/usr/bin/env bash
# Exercises serial_bridge.py's auto-reconnect behavior end-to-end, offline.
#
# Mirrors the real sequence observed on hardware: a session completes, the
# downstream device disappears (radio leaves programming mode / re-enumerates),
# and later reappears at the SAME stable path (like udev's /dev/anytone-cps)
# but as a physically different device underneath. The point being tested:
# the bridge survives this without exiting, and Wine's COM4 mapping (the
# bridge's upstream pty / --upstream-symlink) never has to change.
#
# Uses a symlink as the stand-in for a udev-pinned stable path, re-pointed at
# a brand-new fake_radio instance mid-test -- ptys have no equivalent of a
# name that survives being freed, so the symlink indirection is what makes
# this representable at all.
set -u
cd "$(dirname "$0")"

WORKDIR=$(mktemp -d /tmp/maverick-reconnect-test.XXXXXX)
STABLE_DEV="$WORKDIR/simulated-anytone-cps"
trap 'kill $(jobs -p) 2>/dev/null' EXIT

echo "--- workdir: $WORKDIR ---"

# --- Phase 1: fake_radio #1 up, bridge points at the stable symlink ---
python3 fake_radio.py --blocks 10 --split-mode none --log "$WORKDIR/radio1.log" \
    > "$WORKDIR/radio1_path.txt" 2>"$WORKDIR/radio1_stderr.txt" &
RADIO1_PID=$!
for _ in $(seq 1 50); do [ -s "$WORKDIR/radio1_path.txt" ] && break; sleep 0.02; done
ln -sfn "$(cat "$WORKDIR/radio1_path.txt")" "$STABLE_DEV"
echo "phase 1: fake_radio #1 up at $(cat "$WORKDIR/radio1_path.txt"), symlinked to $STABLE_DEV"

python3 ../bridge/serial_bridge.py --downstream "$STABLE_DEV" --idle-ms 15 \
    --reconnect-interval-ms 100 --reconnect-heartbeat-s 1 --verbose \
    --log "$WORKDIR/bridge.log" > "$WORKDIR/bridge_path.txt" 2>"$WORKDIR/bridge_stderr.txt" &
BRIDGE_PID=$!
for _ in $(seq 1 50); do [ -s "$WORKDIR/bridge_path.txt" ] && break; sleep 0.02; done
BRIDGE_DEV=$(cat "$WORKDIR/bridge_path.txt")
echo "bridge up, app-side device: $BRIDGE_DEV (this path must NEVER change during this test)"

# --- Session A: a full client session against fake_radio #1, through the bridge ---
python3 test_client.py "$BRIDGE_DEV" --blocks 10 --poll-timeout-ms 2000 2>"$WORKDIR/client1.log"
RC1=$?
echo "session A (fake_radio #1) exit code: $RC1"

# fake_radio #1 exits on its own after answering its 10 blocks (see its finally/drain_before_close).
wait "$RADIO1_PID" 2>/dev/null
echo "fake_radio #1 has exited -- bridge's downstream should now be disconnected"
sleep 0.3   # let the bridge notice and start retrying

if ! grep -q "downstream disconnected" "$WORKDIR/bridge_stderr.txt"; then
    echo "FAIL: bridge log never shows a disconnect after fake_radio #1 exited"
    cat "$WORKDIR/bridge_stderr.txt"
    exit 1
fi
echo "confirmed: bridge noticed the disconnect and is retrying (did not exit)"

# --- Phase 2: fake_radio #2 up as a BRAND NEW pty, stable symlink re-pointed at it ---
python3 fake_radio.py --blocks 10 --split-mode none --log "$WORKDIR/radio2.log" \
    > "$WORKDIR/radio2_path.txt" 2>"$WORKDIR/radio2_stderr.txt" &
RADIO2_PID=$!
for _ in $(seq 1 50); do [ -s "$WORKDIR/radio2_path.txt" ] && break; sleep 0.02; done
ln -sfn "$(cat "$WORKDIR/radio2_path.txt")" "$STABLE_DEV"
echo "phase 2: fake_radio #2 up at $(cat "$WORKDIR/radio2_path.txt"), re-pointed $STABLE_DEV at it"

# give the bridge's retry loop a moment to pick it up
sleep 0.5
if ! grep -q "downstream reconnected" "$WORKDIR/bridge_stderr.txt"; then
    echo "FAIL: bridge log never shows a reconnect after fake_radio #2 appeared"
    tail -20 "$WORKDIR/bridge_stderr.txt"
    exit 1
fi
echo "confirmed: bridge reconnected to fake_radio #2 automatically"

# --- Session B: a NEW client session against fake_radio #2, same bridge, same upstream path ---
BRIDGE_DEV_AFTER=$(cat "$WORKDIR/bridge_path.txt")
if [ "$BRIDGE_DEV_AFTER" != "$BRIDGE_DEV" ]; then
    echo "FAIL: bridge's upstream device path changed ($BRIDGE_DEV -> $BRIDGE_DEV_AFTER) -- COM4 mapping would have broken"
    exit 1
fi
python3 test_client.py "$BRIDGE_DEV" --blocks 10 --poll-timeout-ms 2000 2>"$WORKDIR/client2.log"
RC2=$?
echo "session B (fake_radio #2, same bridge/upstream throughout) exit code: $RC2"

wait "$RADIO2_PID" 2>/dev/null
kill "$BRIDGE_PID" 2>/dev/null
wait "$BRIDGE_PID" 2>/dev/null

echo
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
    echo "=== PASS: both sessions completed through one long-lived bridge across a downstream disconnect/reconnect ==="
    rm -rf "$WORKDIR"
    exit 0
else
    echo "=== FAIL: rc1=$RC1 rc2=$RC2 -- logs kept in $WORKDIR ==="
    exit 1
fi
