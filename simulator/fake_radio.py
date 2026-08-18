#!/usr/bin/env python3
"""
Protocol simulator ("Step 2" in README.md's Next Steps) -- stands in for the
real radio so Maverick+bridge (or, here, test_client+bridge) can be exercised
end-to-end with deliberate frame splitting, with no hardware attached and no
radio resets spent.

Opens a pty pair, prints the SLAVE path on stdout (this is the "downstream"
device a bridge or test client should open -- it plays the role /dev/ttyACM0
plays for the real radio), and replays the handshake/identity/block-read
sequence observed in the README's strace capture. Replies are, by default,
occasionally split across two writes with a short inter-write gap, the same
way cdc_acm was observed splitting a USB transfer mid-frame.

Usage:
    python3 fake_radio.py [--blocks N] [--split-mode {none,realistic,aggressive}]
                           [--gap-ms MS] [--force-split-at BLOCK_INDEX]
                           [--seed N] [--log FILE]

Exits after answering --blocks read requests (default 100, comfortably past
the 88 the real capture got through) or when the pty is closed.
"""
import argparse
import fcntl
import os
import pty
import random
import select
import struct
import sys
import termios
import time

import protocol


def pending_input(fd):
    """Bytes still sitting unread in this tty/pty's input queue (TIOCINQ)."""
    return struct.unpack("I", fcntl.ioctl(fd, termios.TIOCINQ, struct.pack("I", 0)))[0]


def drain_before_close(fd, log, timeout_s=1.0, poll_s=0.005):
    """Block until the downstream reader (the bridge, or test_client in
    --direct mode) has actually read everything we wrote, or timeout_s
    elapses.

    MEASURED: closing a pty's master fd discards any bytes still queued for
    the slave to read, unconditionally -- no delay-before-close avoids it,
    only actually waiting for TIOCINQ to hit 0 does. Without this, fake_radio
    closing its own pty right after the final (possibly split) reply
    silently drops the last fragment before the reader can drain it -- a
    fake_radio bug that showed up as a false test failure, not a bridge bug."""
    deadline = time.monotonic() + timeout_s
    checks = 0
    while time.monotonic() < deadline:
        checks += 1
        try:
            n = pending_input(fd)
        except OSError:
            log.log(f"drain_before_close: fd gone after {checks} check(s), nothing to drain", always=True)
            return True
        if n == 0:
            log.log(f"drain_before_close: queue empty after {checks} check(s)", always=True)
            return True
        time.sleep(poll_s)
    log.log(f"drain_before_close: {pending_input(fd)} byte(s) still unread after {timeout_s}s / {checks} checks, closing anyway", always=True)
    return False

def set_raw(fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(
        termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
        | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


class Logger:
    def __init__(self, path):
        self.f = open(path, "a") if path else None
        self.t0 = time.monotonic()

    def log(self, msg):
        line = f"[{time.time():.6f}] {msg}"
        print(line, file=sys.stderr)
        if self.f:
            print(line, file=self.f, flush=True)


def read_exact(fd, n, timeout_s=5.0):
    buf = b""
    deadline = time.monotonic() + timeout_s
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"expected {n} bytes, got {len(buf)}: {buf!r}")
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            raise TimeoutError(f"expected {n} bytes, got {len(buf)}: {buf!r}")
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            raise EOFError("pty closed")
        buf += chunk
    return buf


def send(fd, payload, log, split_mode, gap_ms, rng, force_split, label):
    """Write `payload` to fd, optionally splitting it into two writes with a
    gap, to simulate a USB transfer boundary landing mid-frame."""
    do_split = False
    if force_split:
        do_split = True
    elif split_mode == "aggressive":
        do_split = len(payload) > 1
    elif split_mode == "realistic":
        do_split = len(payload) > 1 and rng.random() < 0.01  # ~1-in-97, matches capture

    if not do_split:
        os.write(fd, payload)
        log.log(f"{label}: sent {len(payload)} bytes whole: {payload.hex()}")
        return

    split_at = rng.randint(1, len(payload) - 1)
    first, second = payload[:split_at], payload[split_at:]
    os.write(fd, first)
    log.log(f"{label}: SPLIT sent {len(first)}/{len(payload)} bytes: {first.hex()}")
    time.sleep(gap_ms / 1000.0)
    os.write(fd, second)
    log.log(f"{label}: SPLIT sent remaining {len(second)}/{len(payload)} bytes: {second.hex()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--blocks", type=int, default=100, help="number of block-read requests to answer")
    ap.add_argument("--mode", choices=["read", "write"], default="read",
                     help="read: app requests blocks, we send data (matches the real capture). "
                          "write: app sends blocks, we send short acks (invented, see protocol.py docstring)")
    ap.add_argument("--split-mode", choices=["none", "realistic", "aggressive"], default="realistic")
    ap.add_argument("--gap-ms", type=float, default=3.0, help="inter-write gap when a reply is split")
    ap.add_argument("--force-split-at", type=int, default=None,
                     help="deterministically split the reply to this block index (0-based), reproducing the README's 88-blocks-then-short-frame capture")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    log = Logger(args.log)

    master_fd, slave_fd = pty.openpty()
    set_raw(slave_fd)
    set_raw(master_fd)
    slave_name = os.ttyname(slave_fd)

    # Print the path a downstream consumer (bridge or test client) should
    # open. Flush explicitly -- a shell script will be reading this line.
    print(slave_name, flush=True)
    log.log(f"fake_radio listening on {slave_name} (blocks={args.blocks} split_mode={args.split_mode})")

    try:
        # --- handshake ---
        req = read_exact(master_fd, len(protocol.HANDSHAKE_REQ))
        assert req == protocol.HANDSHAKE_REQ, f"bad handshake: {req!r}"
        log.log(f"recv handshake: {req!r}")
        send(master_fd, protocol.HANDSHAKE_REPLY, log, args.split_mode, args.gap_ms, rng, False, "handshake-reply")

        # --- identity ---
        req = read_exact(master_fd, len(protocol.IDENTITY_REQ))
        assert req == protocol.IDENTITY_REQ, f"bad identity req: {req!r}"
        log.log(f"recv identity req: {req!r}")
        send(master_fd, protocol.IDENTITY_REPLY, log, args.split_mode, args.gap_ms, rng, False, "identity-reply")

        if args.mode == "read":
            # --- block reads: app asks, we send data ---
            for i in range(args.blocks):
                cmd = read_exact(master_fd, protocol.READ_CMD_LEN)
                addr, length = protocol.parse_read_cmd(cmd)
                log.log(f"recv block[{i}] read cmd: addr=0x{addr:08x} len={length}")
                reply = protocol.make_block_reply(addr, length)
                force = (args.force_split_at is not None and i == args.force_split_at)
                send(master_fd, reply, log, args.split_mode, args.gap_ms, rng, force, f"block[{i}]-reply")
        else:
            # --- block writes: app sends data (the LARGE frame is now outbound),
            # we ack with a short reply (that's what can get split here) ---
            write_cmd_len = 8 + protocol.BLOCK_DATA_LEN
            for i in range(args.blocks):
                cmd = read_exact(master_fd, write_cmd_len)
                addr, length, data, ok = protocol.parse_write_cmd(cmd)
                addr_str = f"0x{addr:08x}" if addr is not None else "?"
                log.log(f"recv block[{i}] write cmd: addr={addr_str} len={length} checksum_ok={ok} data={data.hex() if data else ''}")
                ack = protocol.make_write_ack(addr, ok=ok)
                force = (args.force_split_at is not None and i == args.force_split_at)
                send(master_fd, ack, log, args.split_mode, args.gap_ms, rng, force, f"block[{i}]-ack")

        log.log(f"answered all {args.blocks} block requests, done")

    except (TimeoutError, EOFError, AssertionError) as e:
        log.log(f"stopped: {e}")
        sys.exit(1)
    finally:
        drain_before_close(slave_fd, log)
        os.close(master_fd)
        os.close(slave_fd)


if __name__ == "__main__":
    main()
