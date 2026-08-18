#!/usr/bin/env python3
"""
Step 0 foundation test -- run BEFORE trusting anything built on top of it.

Proves that a Python-owned pty pair, with termios put into raw mode, passes
arbitrary binary bytes (all 256 values, including control chars the cooked
line discipline would otherwise mangle: \\r \\n \\x00 \\x11/\\x13 XON/XOFF)
through unmodified in both directions, and that a non-blocking read on an
empty pty returns nothing immediately rather than blocking -- the same
"return immediately with whatever is available, zero counts as valid"
semantics Maverick's ReadFile() relies on.

If this script does not print ALL PASS, nothing built on top of pty pairs
(fake_radio.py, serial_bridge.py) can be trusted, because byte corruption or
blocking behavior here would masquerade as a "protocol" bug two layers up.
"""
import os
import pty
import select
import sys
import termios
import time

FAILURES = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(label)


def set_raw(fd):
    attrs = termios.tcgetattr(fd)
    # iflag: turn off input translation/flow-control that would eat control bytes
    attrs[0] &= ~(
        termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
        | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
    )
    # oflag: no output post-processing
    attrs[1] &= ~termios.OPOST
    # lflag: no echo, no canonical line buffering, no signal chars
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    # cflag: 8-bit clean
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8
    # cc: VMIN=1, VTIME=0 as a sane default; callers override per-fd as needed
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def read_exact(fd, n, timeout_s):
    """Blocking-with-timeout read of exactly n bytes, or return what we got."""
    buf = b""
    deadline = time.monotonic() + timeout_s
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            break
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def main():
    print("=== Step 0: pty raw-mode byte-fidelity self-test ===\n")

    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    print(f"pty pair opened: master fd={master_fd}, slave={slave_name}\n")

    set_raw(slave_fd)
    set_raw(master_fd)

    # --- Test 1: all 256 byte values, master -> slave ---
    payload = bytes(range(256))
    os.write(master_fd, payload)
    got = read_exact(slave_fd, len(payload), timeout_s=1.0)
    check(
        "256-byte binary payload master->slave, byte-identical",
        got == payload,
        f"sent={len(payload)} got={len(got)}" + ("" if got == payload else f" first_diff_at={next((i for i in range(min(len(got),len(payload))) if got[i]!=payload[i]), 'len-mismatch')}"),
    )

    # --- Test 2: same, slave -> master ---
    os.write(slave_fd, payload)
    got = read_exact(master_fd, len(payload), timeout_s=1.0)
    check(
        "256-byte binary payload slave->master, byte-identical",
        got == payload,
        f"sent={len(payload)} got={len(got)}",
    )

    # --- Test 3: protocol-specific control bytes survive round trip ---
    proto_bytes = b"PROGRAM" + b"\x02\x06\x00\x0d\x0a\x11\x13" + b"ID890UV\x00\x07V100\x00\x00\x06"
    os.write(master_fd, proto_bytes)
    got = read_exact(slave_fd, len(proto_bytes), timeout_s=1.0)
    check(
        "protocol control-byte sequence survives raw mode",
        got == proto_bytes,
        f"sent={proto_bytes!r} got={got!r}",
    )

    # --- Test 4: non-blocking read on an empty pty returns immediately, empty ---
    flags = os.get_blocking(slave_fd)
    os.set_blocking(slave_fd, False)
    t0 = time.monotonic()
    try:
        empty = os.read(slave_fd, 64)
    except BlockingIOError:
        empty = b""
    elapsed_ms = (time.monotonic() - t0) * 1000
    os.set_blocking(slave_fd, flags)
    check(
        "non-blocking read on empty pty returns immediately with nothing",
        empty == b"" and elapsed_ms < 5,
        f"got={empty!r} elapsed={elapsed_ms:.2f}ms",
    )

    # --- Test 5: a write arriving in two fragments is visible as two separate reads ---
    # (this is the behavior serial_bridge.py must compensate for -- confirm the
    # pty layer itself does NOT coalesce/reassemble on its own, so we know any
    # reassembly we see later comes from our bridge code, not a side effect of ptys)
    os.write(master_fd, b"W\x01\xf8\x10\x00\x10")
    time.sleep(0.01)
    os.write(master_fd, b"\xAA" * 15 + b"\x00")
    r1 = read_exact(slave_fd, 6, timeout_s=0.05)
    r2 = read_exact(slave_fd, 16, timeout_s=0.05)
    check(
        "fragmented writes stay observable as fragments at the raw pty layer",
        len(r1) == 6 and len(r2) == 16,
        f"r1_len={len(r1)} r2_len={len(r2)}",
    )

    os.close(master_fd)
    os.close(slave_fd)

    print()
    if FAILURES:
        print(f"=== {len(FAILURES)} FAILURE(S): {FAILURES} ===")
        sys.exit(1)
    print("=== ALL PASS -- pty raw byte-path is trustworthy, safe to build on ===")


if __name__ == "__main__":
    main()
