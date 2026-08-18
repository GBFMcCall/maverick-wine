#!/usr/bin/env python3
"""
Userspace serial bridge -- README.md "Next steps, 1. Userspace serial bridge".

    Maverick (Wine) --> COM4 -> pty --> bridge --> downstream device --> radio

Sits between the app's COM port and the real (or simulated) serial device.
Writes from the app are forwarded to the downstream device immediately,
unmodified. Bytes arriving FROM the downstream device are accumulated and
only released to the app side once the burst goes idle for --idle-ms
(default 4ms) with no further bytes -- so the app always sees one complete
frame per read, never a partial one, regardless of how cdc_acm split it
across USB transfers on the wire.

The upstream side (the app-side pty, and --upstream-symlink if given) is
created ONCE, immediately, and kept alive for the life of the process -- even
if --downstream doesn't exist yet at startup (e.g. launched before the radio
is powered on). Startup and a mid-session loss share the same wait/retry
logic, so there's no separate "crash if the device isn't there yet" path. If the downstream
device disappears -- MEASURED on real hardware: the AnyTone radio drops its
tty node ~3s after the app's end-of-session handshake, consistent with
leaving programming mode and re-enumerating over USB -- the bridge does NOT
exit. It closes the stale downstream fd, queues any app writes that arrive
in the meantime, and retries opening --downstream on an interval until it
reappears (a stable path, e.g. the udev-pinned /dev/anytone-cps, is what
makes this work -- a raw /dev/ttyACM* node is not stable across a
re-enumeration). Wine's COM4 mapping is therefore never invalidated by a
radio power-cycle or a Maverick session ending; start the bridge once and
leave it running. Only SIGINT/SIGTERM (or the upstream pty itself going
away, which shouldn't happen while the bridge holds its own copy of it)
stop it.

Default is 4ms, chosen empirically on real hardware, not derived from first
principles -- start conservative and only lower it with evidence in hand:
a full read (6547 blocks) was run against the real radio at idle-ms 15, then
8, then 4, each completing cleanly with zero short/corrupt frames and
getting progressively faster (109s -> 63s -> 37s) as the threshold dropped. None
of those three runs happened to hit a real split on the wire, so this is
NOT proof 4ms safely covers a genuine split's inter-fragment gap -- it's
proof the bridge's own timing holds up at 4ms and the user judged the
speed/risk trade worth it. If reads start failing after this, the first
thing to try is raising --idle-ms back up, not debugging the reassembly
logic -- see simulator/ dev notes: a 5ms threshold measured a ~20% failure
rate purely from select()/sleep() scheduling jitter (up to ~5.4ms overshoot
on a nominal 3ms gap) having near-zero margin, nothing to do with the radio.

The bridge is deliberately protocol-blind: it does not parse frames, does
not know command/reply lengths, and does not alter zero-byte-poll semantics
-- when nothing is buffered, the app-side pty simply has nothing to read,
same as it would from an idle real device.

Usage:
    python3 serial_bridge.py --downstream /path/to/device [--idle-ms 4]
                              [--upstream-symlink /path/to/link]
                              [--log FILE] [--baud 9600]

Prints the created app-side pty slave path on stdout (one line) as soon as
it's ready, so callers (test harness or a wrapper script) can point Wine's
COM4 symlink -- or a test client -- at it. If --upstream-symlink is given,
the bridge also maintains that symlink itself.
"""
import argparse
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time


def set_raw(fd, vmin=1, vtime=0):
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(
        termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
        | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8
    attrs[6][termios.VMIN] = vmin
    attrs[6][termios.VTIME] = vtime
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def pending_input(fd):
    """Bytes still sitting unread in this tty/pty's input queue (TIOCINQ)."""
    return struct.unpack("I", fcntl.ioctl(fd, termios.TIOCINQ, struct.pack("I", 0)))[0]


def drain_before_close(fd, log, timeout_s=2.0, poll_s=0.005):
    """Block until the app side has actually read everything we wrote to it,
    or timeout_s elapses.

    MEASURED, not assumed: closing a pty's master fd discards any bytes still
    queued for the slave to read, unconditionally, no matter how long you
    wait beforehand -- confirmed empirically (see dev notes) by writing to a
    fresh pty pair, closing the master after delays up to 100ms with the
    reader never touching it, and observing the independently-opened slave
    fd get nothing every time. A fixed sleep-before-close cannot fix this;
    only actually waiting for TIOCINQ to reach 0 does, because it reflects
    real drainage instead of guessing at a "long enough" delay.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if pending_input(fd) == 0:
                return True
        except OSError:
            return True  # fd already gone/invalid -- nothing to drain
        time.sleep(poll_s)
    log.log(f"drain_before_close: {pending_input(fd)} byte(s) still unread after {timeout_s}s, closing anyway", always=True)
    return False


BAUD_MAP = {
    9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
    57600: termios.B57600, 115200: termios.B115200,
}


def set_baud(fd, baud):
    if baud not in BAUD_MAP:
        raise ValueError(f"unsupported baud {baud}")
    attrs = termios.tcgetattr(fd)
    attrs[4] = BAUD_MAP[baud]
    attrs[5] = BAUD_MAP[baud]
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


class Logger:
    def __init__(self, path, verbose):
        self.f = open(path, "a") if path else None
        self.t0 = time.monotonic()
        self.verbose = verbose

    def log(self, msg, always=False):
        if not self.verbose and not always:
            return
        line = f"[{time.time():.6f}] {msg}"
        print(line, file=sys.stderr)
        if self.f:
            print(line, file=self.f, flush=True)


class Stats:
    """Evidence counters -- printed on exit so every run leaves a measured
    record of what the bridge actually did, not just what it was asked to do."""
    def __init__(self):
        self.down_reads = 0
        self.down_bytes = 0
        self.flushes = 0
        self.flushed_bytes = 0
        self.split_flushes = 0  # flushes assembled from >1 downstream read
        self.up_writes = 0
        self.up_bytes = 0
        self.flush_latencies_ms = []
        self.disconnects = 0
        self.reconnects = 0
        self.queued_writes_during_outage = 0

    def report(self):
        print("\n=== serial_bridge stats ===", file=sys.stderr)
        print(f"downstream reads: {self.down_reads}  bytes: {self.down_bytes}", file=sys.stderr)
        print(f"flushes to app:   {self.flushes}  bytes: {self.flushed_bytes}  "
              f"assembled-from-multiple-reads: {self.split_flushes}", file=sys.stderr)
        print(f"app->downstream writes: {self.up_writes}  bytes: {self.up_bytes}", file=sys.stderr)
        if self.flush_latencies_ms:
            lat = self.flush_latencies_ms
            print(f"flush latency (first-byte to release) ms: "
                  f"avg={sum(lat)/len(lat):.2f} min={min(lat):.2f} max={max(lat):.2f}", file=sys.stderr)
        print(f"downstream disconnects: {self.disconnects}  reconnects: {self.reconnects}  "
              f"app writes queued during an outage: {self.queued_writes_during_outage}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--downstream", required=True, help="device to bridge to (real port or fake_radio's pty slave)")
    ap.add_argument("--idle-ms", type=float, default=4.0, help="quiet period before releasing a buffered frame")
    ap.add_argument("--upstream-symlink", default=None, help="maintain this path as a symlink to the app-side pty slave")
    ap.add_argument("--baud", type=int, default=None, help="set this baud rate on the downstream device (ignored for ptys)")
    ap.add_argument("--log", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--read-chunk", type=int, default=4096)
    ap.add_argument("--reconnect-interval-ms", type=float, default=200.0,
                     help="how often to retry opening --downstream after it disappears")
    ap.add_argument("--reconnect-heartbeat-s", type=float, default=5.0,
                     help="log a still-waiting line at most this often during a long outage (0 disables)")
    args = ap.parse_args()

    log = Logger(args.log, args.verbose)
    stats = Stats()

    def open_downstream():
        fd = os.open(args.downstream, os.O_RDWR | os.O_NOCTTY)
        try:
            set_raw(fd)
        except termios.error:
            pass  # not a tty (e.g. a plain file in some test) -- proceed anyway
        if args.baud:
            try:
                set_baud(fd, args.baud)
            except (termios.error, ValueError) as e:
                log.log(f"warning: could not set baud {args.baud}: {e}", always=True)
        return fd

    # Don't hard-fail if --downstream isn't there yet (e.g. the radio isn't
    # powered on when this is launched from a shortcut) -- start in the same
    # "disconnected, retrying" state the main loop already handles for a
    # mid-session loss, so there's one code path for both instead of a
    # separate startup crash. The upstream pty/symlink still comes up right
    # away, so COM4 is valid immediately even before the radio is.
    down_fd = None
    try:
        down_fd = open_downstream()
    except OSError as e:
        log.log(f"downstream not available yet ({e}), will wait for it -- upstream still comes up now", always=True)

    up_master_fd, up_slave_fd = pty.openpty()
    set_raw(up_slave_fd)
    set_raw(up_master_fd)
    up_slave_name = os.ttyname(up_slave_fd)

    if args.upstream_symlink:
        link = args.upstream_symlink
        try:
            if os.path.islink(link) or os.path.exists(link):
                os.remove(link)
        except FileNotFoundError:
            pass
        os.symlink(up_slave_name, link)
        log.log(f"symlinked {link} -> {up_slave_name}", always=True)

    print(up_slave_name, flush=True)
    log.log(f"bridge up: downstream={args.downstream} upstream={up_slave_name} idle_ms={args.idle_ms}", always=True)

    running = True

    def handle_sig(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    inbound = bytearray()
    last_down_rx = None
    first_byte_of_burst_t = None
    reads_in_burst = 0
    outbound_queue = bytearray()   # app writes queued while downstream is disconnected
    # None means down_fd is currently open; if startup couldn't open it yet,
    # start the retry clock now instead of waiting for the first disconnect().
    next_reconnect_attempt = None if down_fd is not None else time.monotonic()
    last_heartbeat = 0.0

    def disconnect(reason):
        nonlocal down_fd, next_reconnect_attempt
        log.log(f"downstream disconnected ({reason}), will keep retrying -- upstream stays up", always=True)
        try:
            os.close(down_fd)
        except OSError:
            pass
        down_fd = None
        stats.disconnects += 1
        next_reconnect_attempt = time.monotonic()  # try immediately, then back off on failure

    try:
        while running:
            timeouts = [1.0]  # default: wake periodically to notice signals even when fully idle
            if inbound:
                timeouts.append(max(0.0, (args.idle_ms / 1000.0) - (time.monotonic() - last_down_rx)))
            if next_reconnect_attempt is not None:
                timeouts.append(max(0.0, next_reconnect_attempt - time.monotonic()))
            timeout = min(timeouts)

            watch = [up_master_fd] + ([down_fd] if down_fd is not None else [])
            try:
                r, _, _ = select.select(watch, [], [], timeout)
            except InterruptedError:
                continue

            if down_fd is not None and down_fd in r:
                try:
                    chunk = os.read(down_fd, args.read_chunk)
                except OSError as e:
                    disconnect(str(e))
                    chunk = None
                if chunk is not None:
                    if not chunk:
                        disconnect("EOF")
                    else:
                        stats.down_reads += 1
                        stats.down_bytes += len(chunk)
                        reads_in_burst += 1
                        if not inbound:
                            first_byte_of_burst_t = time.monotonic()
                        inbound += chunk
                        last_down_rx = time.monotonic()
                        log.log(f"downstream->buffer: +{len(chunk)}B (buffer now {len(inbound)}B): {chunk.hex()}")

            if up_master_fd in r:
                try:
                    chunk = os.read(up_master_fd, args.read_chunk)
                except OSError as e:
                    log.log(f"upstream read error: {e}", always=True)
                    break
                if not chunk:
                    log.log("upstream EOF, shutting down", always=True)
                    break
                stats.up_writes += 1
                stats.up_bytes += len(chunk)
                if down_fd is None:
                    outbound_queue += chunk
                    stats.queued_writes_during_outage += 1
                    log.log(f"app->downstream: {len(chunk)}B queued (downstream still reconnecting): {chunk.hex()}", always=True)
                else:
                    os.write(down_fd, chunk)
                    log.log(f"app->downstream: {len(chunk)}B passed straight through: {chunk.hex()}")

            if next_reconnect_attempt is not None and time.monotonic() >= next_reconnect_attempt:
                try:
                    down_fd = open_downstream()
                    next_reconnect_attempt = None
                    stats.reconnects += 1
                    log.log("downstream reconnected", always=True)
                    if outbound_queue:
                        os.write(down_fd, bytes(outbound_queue))
                        log.log(f"flushed {len(outbound_queue)}B queued during the outage to downstream: {bytes(outbound_queue).hex()}", always=True)
                        outbound_queue.clear()
                except OSError:
                    next_reconnect_attempt = time.monotonic() + (args.reconnect_interval_ms / 1000.0)
                    if args.reconnect_heartbeat_s and (time.monotonic() - last_heartbeat) >= args.reconnect_heartbeat_s:
                        log.log(f"still waiting for {args.downstream} to reappear...", always=True)
                        last_heartbeat = time.monotonic()

            if inbound and last_down_rx is not None and (time.monotonic() - last_down_rx) >= (args.idle_ms / 1000.0):
                os.write(up_master_fd, bytes(inbound))
                stats.flushes += 1
                stats.flushed_bytes += len(inbound)
                if reads_in_burst > 1:
                    stats.split_flushes += 1
                stats.flush_latencies_ms.append((time.monotonic() - first_byte_of_burst_t) * 1000)
                log.log(f"buffer->app: FLUSH {len(inbound)}B after {reads_in_burst} read(s): {bytes(inbound).hex()}", always=(reads_in_burst > 1))
                inbound.clear()
                reads_in_burst = 0
                last_down_rx = None

    finally:
        if inbound:
            try:
                os.write(up_master_fd, bytes(inbound))
                stats.flushes += 1
                stats.flushed_bytes += len(inbound)
                if reads_in_burst > 1:
                    stats.split_flushes += 1
                log.log(f"buffer->app: FINAL FLUSH {len(inbound)}B on shutdown (not left stranded): {bytes(inbound).hex()}", always=True)
            except OSError as e:
                log.log(f"final flush failed: {e}", always=True)
        drain_before_close(up_slave_fd, log)
        if down_fd is not None:
            os.close(down_fd)
        os.close(up_master_fd)
        os.close(up_slave_fd)
        if args.upstream_symlink:
            try:
                os.remove(args.upstream_symlink)
            except FileNotFoundError:
                pass
        stats.report()


if __name__ == "__main__":
    main()
