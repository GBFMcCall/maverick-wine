#!/usr/bin/env python3
"""
Mimics Maverick's own read behavior against a device path -- either
fake_radio.py directly (to reproduce the known short-read bug) or
serial_bridge.py sitting in front of it (to prove the bug is fixed).

Maverick's read behavior, per README.md's evidence (COMMTIMEOUTS VMIN=0
VTIME=0, no ClearCommError/overlapped I/O, no reassembly, no retry):
  - it issues one ReadFile() at a time, sized to expect a whole frame
  - a call returning 0 bytes ("empty poll") is normal and it just calls again
  - a call returning a NONZERO but SHORT count is treated as the answer --
    there is no code path that goes back for the rest

This client reproduces exactly that: a poll loop of individually-nonblocking
reads, and the FIRST nonzero read for each expected frame is taken as final,
whatever its length. If that length doesn't match the expected frame size,
or the block reply's checksum doesn't verify, the run is marked FAILED at
that point -- same as Maverick's state machine stopping dead.

Usage:
    python3 test_client.py DEVICE_PATH [--blocks N] [--poll-interval-ms MS]
                            [--poll-timeout-ms MS] [--log FILE]

Exit code 0 = every expected frame arrived whole and verified; 1 = a short
frame, corrupt data, or timeout was hit (reports where).
"""
import argparse
import os
import sys
import termios
import time

import protocol


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


class ShortFrame(Exception):
    pass


def poll_read(fd, max_len, poll_interval_s, timeout_s, log, label):
    """Reproduces Maverick's VMIN=0/VTIME=0 ReadFile loop: repeated
    non-blocking reads, each call's result taken as-is. Returns
    (payload_bytes, n_polls, elapsed_s). The first NONZERO read ends the
    loop -- regardless of whether it looks complete, exactly like the real
    app has no way to know it wasn't."""
    deadline = time.monotonic() + timeout_s
    t_start = time.monotonic()
    polls = 0
    while True:
        polls += 1
        try:
            chunk = os.read(fd, max_len)
        except BlockingIOError:
            chunk = b""
        if chunk:
            elapsed = time.monotonic() - t_start
            return chunk, polls, elapsed
        if time.monotonic() > deadline:
            elapsed = time.monotonic() - t_start
            return b"", polls, elapsed
        time.sleep(poll_interval_s)


def expect_frame(fd, expected_len, poll_interval_s, timeout_s, log, label):
    chunk, polls, elapsed = poll_read(fd, max(64, expected_len), poll_interval_s, timeout_s, log, label)
    log.log(f"{label}: read {len(chunk)}/{expected_len} bytes in {polls} poll(s), {elapsed*1000:.2f}ms: {chunk.hex()}")
    if len(chunk) != expected_len:
        raise ShortFrame(f"{label}: expected {expected_len} bytes, got {len(chunk)}: {chunk.hex()}")
    return chunk


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device")
    ap.add_argument("--blocks", type=int, default=100)
    ap.add_argument("--mode", choices=["read", "write"], default="read",
                     help="must match fake_radio's --mode on the other end")
    ap.add_argument("--poll-interval-ms", type=float, default=1.0)
    ap.add_argument("--poll-timeout-ms", type=float, default=500.0)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    log = Logger(args.log)
    poll_interval_s = args.poll_interval_ms / 1000.0
    poll_timeout_s = args.poll_timeout_ms / 1000.0

    fd = os.open(args.device, os.O_RDWR | os.O_NOCTTY)
    set_raw(fd)
    os.set_blocking(fd, False)

    blocks_ok = 0
    latencies_ms = []
    t_run_start = time.monotonic()

    try:
        os.write(fd, protocol.HANDSHAKE_REQ)
        log.log(f"sent handshake: {protocol.HANDSHAKE_REQ!r}")
        expect_frame(fd, len(protocol.HANDSHAKE_REPLY), poll_interval_s, poll_timeout_s, log, "handshake-reply")

        os.write(fd, protocol.IDENTITY_REQ)
        log.log(f"sent identity req: {protocol.IDENTITY_REQ!r}")
        expect_frame(fd, len(protocol.IDENTITY_REPLY), poll_interval_s, poll_timeout_s, log, "identity-reply")

        for i in range(args.blocks):
            addr = 0x000398 + i * protocol.BLOCK_DATA_LEN
            t_write = time.monotonic()

            if args.mode == "read":
                cmd = protocol.make_read_cmd(addr, protocol.BLOCK_DATA_LEN)
                os.write(fd, cmd)
                log.log(f"block[{i}]: sent read cmd addr=0x{addr:08x}")

                reply = expect_frame(fd, 8 + protocol.BLOCK_DATA_LEN, poll_interval_s, poll_timeout_s, log, f"block[{i}]-reply")
                latencies_ms.append((time.monotonic() - t_write) * 1000)

                r_addr, r_len, data, ok = protocol.parse_block_reply(reply)
                if not ok:
                    addr_str = f"0x{r_addr:08x}" if r_addr is not None else "?"
                    raise ShortFrame(f"block[{i}]: checksum/content verification FAILED addr={addr_str}")
            else:
                data = protocol.block_data_for(addr, protocol.BLOCK_DATA_LEN)
                cmd = protocol.make_write_cmd(addr, data)
                os.write(fd, cmd)
                log.log(f"block[{i}]: sent write cmd addr=0x{addr:08x} len={len(data)}: {cmd.hex()}")

                ack = expect_frame(fd, protocol.WRITE_ACK_LEN, poll_interval_s, poll_timeout_s, log, f"block[{i}]-ack")
                latencies_ms.append((time.monotonic() - t_write) * 1000)

                r_addr, ok = protocol.parse_write_ack(ack)
                if not ok or r_addr != addr:
                    addr_str = f"0x{r_addr:08x}" if r_addr is not None else "?"
                    raise ShortFrame(f"block[{i}]: ack verification FAILED addr={addr_str} (sent 0x{addr:08x})")
            blocks_ok += 1

        total_s = time.monotonic() - t_run_start
        verb = "read" if args.mode == "read" else "written and acked"
        print(f"\n=== PASS: {blocks_ok}/{args.blocks} blocks {verb} whole and verified ===")
        print(f"total time: {total_s*1000:.1f}ms  "
              f"avg block latency: {sum(latencies_ms)/len(latencies_ms):.2f}ms  "
              f"max: {max(latencies_ms):.2f}ms  min: {min(latencies_ms):.2f}ms")
        sys.exit(0)

    except ShortFrame as e:
        total_s = time.monotonic() - t_run_start
        print(f"\n=== FAIL after {blocks_ok}/{args.blocks} good blocks: {e} ===")
        print(f"elapsed before failure: {total_s*1000:.1f}ms")
        sys.exit(1)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
