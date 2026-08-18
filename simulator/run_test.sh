#!/usr/bin/env bash
# Orchestrates one end-to-end trial: fake_radio <-> serial_bridge <-> test_client.
# Fully headless -- no Wine, no GUI, safe to run any number of times.
#
# Usage: run_test.sh [--blocks N] [--split-mode MODE] [--gap-ms MS]
#                     [--force-split-at I] [--seed N] [--idle-ms MS]
#                     [--direct] [--quiet] [--bridge-verbose] [--mode read|write]
#
# --mode write flips which side sends the large frame: app sends the block,
#   fake_radio sends a short ack -- for exercising the bridge's outbound
#   pass-through and ack-reassembly ahead of a real write to the radio. This
#   framing is invented (no real write capture exists), see protocol.py.
#
# --direct runs test_client straight against fake_radio, bypassing the
#   bridge -- use this to (re)confirm the bug still reproduces without a fix
#   in the way. Default is bridged (the thing actually being tested).
#
# --idle-ms default is 15, not the bridge's own default of 5: MEASURED
#   python time.sleep() jitter on this machine put a nominal 3ms gap as high
#   as 5.4ms under load (see dev notes), so idle-ms=5/gap-ms=3 has ~zero
#   safety margin and produces false failures that are a test-harness timing
#   artifact, not a bridge bug -- confirmed by seeing them vanish at
#   idle-ms=15 across 100+ trials. This says nothing about the right value
#   against real hardware, which needs measuring separately.
#
# Exit code is test_client's: 0 = every frame arrived whole and verified.
set -u
cd "$(dirname "$0")"

BLOCKS=100
SPLIT_MODE=realistic
GAP_MS=3
FORCE_SPLIT_AT=""
SEED=""
IDLE_MS=15
DIRECT=0
QUIET=0
BRIDGE_VERBOSE=0
MODE=read

while [ $# -gt 0 ]; do
    case "$1" in
        --blocks) BLOCKS="$2"; shift 2 ;;
        --split-mode) SPLIT_MODE="$2"; shift 2 ;;
        --gap-ms) GAP_MS="$2"; shift 2 ;;
        --force-split-at) FORCE_SPLIT_AT="$2"; shift 2 ;;
        --seed) SEED="$2"; shift 2 ;;
        --idle-ms) IDLE_MS="$2"; shift 2 ;;
        --direct) DIRECT=1; shift ;;
        --quiet) QUIET=1; shift ;;
        --bridge-verbose) BRIDGE_VERBOSE=1; shift ;;
        --mode) MODE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

WORKDIR=$(mktemp -d /tmp/maverick-bridge-test.XXXXXX)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORKDIR"' EXIT

RADIO_ARGS=(--blocks "$BLOCKS" --mode "$MODE" --split-mode "$SPLIT_MODE" --gap-ms "$GAP_MS" --log "$WORKDIR/fake_radio.log")
[ -n "$FORCE_SPLIT_AT" ] && RADIO_ARGS+=(--force-split-at "$FORCE_SPLIT_AT")
[ -n "$SEED" ] && RADIO_ARGS+=(--seed "$SEED")

python3 fake_radio.py "${RADIO_ARGS[@]}" > "$WORKDIR/radio_path.txt" 2>"$WORKDIR/radio_stderr.txt" &
RADIO_PID=$!

for _ in $(seq 1 50); do [ -s "$WORKDIR/radio_path.txt" ] && break; sleep 0.02; done
RADIO_DEV=$(cat "$WORKDIR/radio_path.txt")
if [ -z "$RADIO_DEV" ]; then
    echo "fake_radio never printed a device path; stderr:" >&2
    cat "$WORKDIR/radio_stderr.txt" >&2
    exit 2
fi

if [ "$DIRECT" -eq 1 ]; then
    TARGET_DEV="$RADIO_DEV"
    BRIDGE_PID=""
else
    VERBOSE_FLAG=""
    [ "$BRIDGE_VERBOSE" -eq 1 ] && VERBOSE_FLAG="--verbose"
    python3 ../bridge/serial_bridge.py --downstream "$RADIO_DEV" --idle-ms "$IDLE_MS" $VERBOSE_FLAG \
        --log "$WORKDIR/bridge.log" > "$WORKDIR/bridge_path.txt" 2>"$WORKDIR/bridge_stderr.txt" &
    BRIDGE_PID=$!
    for _ in $(seq 1 50); do [ -s "$WORKDIR/bridge_path.txt" ] && break; sleep 0.02; done
    TARGET_DEV=$(cat "$WORKDIR/bridge_path.txt")
    if [ -z "$TARGET_DEV" ]; then
        echo "bridge never printed a device path; stderr:" >&2
        cat "$WORKDIR/bridge_stderr.txt" >&2
        exit 2
    fi
fi

python3 test_client.py "$TARGET_DEV" --blocks "$BLOCKS" --mode "$MODE" 2>"$WORKDIR/client.log"
RC=$?

if [ "$QUIET" -eq 0 ]; then
    echo "--- logs kept in $WORKDIR ---"
    trap - EXIT   # leave the workdir behind for inspection on non-quiet runs
    kill $(jobs -p) 2>/dev/null
fi

exit $RC
