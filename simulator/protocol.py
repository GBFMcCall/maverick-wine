"""
Shared, self-consistent protocol definitions for fake_radio.py and
test_client.py.

This is NOT a reverse-engineered copy of the real AnyTone/Maverick wire
protocol -- we don't have that. It reproduces the *shape* observed in the
README's strace capture (command/reply sizes, the handshake sequence, the
6-byte read command / 24-byte block reply framing) closely enough to exercise
serial_bridge.py's reassembly under the same splitting failure mode, while
being fully self-checking: fake_radio fills each block deterministically from
its address and appends a checksum, so test_client can detect corruption, not
just short reads.

Frame formats (both ends agree on these; real radio's actual byte meanings
are irrelevant to what's being tested here):

    handshake:  app -> "PROGRAM"                (7 bytes)
                radio -> "QX\\x06"                (3 bytes)
    identity:   app -> "\\x02"                    (1 byte)
                radio -> "ID890UV\\x00\\x07V100\\x00\\x00\\x06"  (16 bytes)
    block read: app -> "R" + addr(4, BE) + length(1)         (6 bytes)
                radio -> "W" + addr(4, BE) + length(1) + data(length) + csum(2, BE)
                       total = 8 + length; length=0x10 -> 24 bytes (matches capture)

Block WRITE is invented, not captured -- there is no real trace of a write to
mirror, per README's own caveat that writes may use different framing. It
models the plausible symmetric case (large payload now travels app->radio,
a short ack comes back) purely to exercise the bridge's mechanism -- byte-
perfect outbound pass-through under volume, and reassembly of a split *ack*
-- not to claim it matches the real write protocol's actual bytes:

    block write: app -> "W" + addr(4, BE) + length(1) + data(length) + csum(2, BE)  (8+length)
                 radio -> "A" + addr(4, BE) + status(1)                              (6 bytes)
"""
import struct

HANDSHAKE_REQ = b"PROGRAM"
HANDSHAKE_REPLY = b"QX\x06"

IDENTITY_REQ = b"\x02"
IDENTITY_REPLY = b"ID890UV\x00\x07V100\x00\x00\x06"

READ_CMD_LEN = 6
BLOCK_DATA_LEN = 0x10  # 16, matches every block length seen in the capture


def make_read_cmd(addr: int, length: int = BLOCK_DATA_LEN) -> bytes:
    return b"R" + struct.pack(">I", addr) + bytes([length])


def parse_read_cmd(cmd: bytes):
    if len(cmd) != READ_CMD_LEN or cmd[0:1] != b"R":
        raise ValueError(f"not a read command: {cmd!r}")
    addr = struct.unpack(">I", cmd[1:5])[0]
    length = cmd[5]
    return addr, length


def block_data_for(addr: int, length: int) -> bytes:
    """Deterministic, address-derived payload -- lets test_client verify
    content integrity, not just framing, with zero shared state."""
    return bytes((addr + i) & 0xFF for i in range(length))


def checksum(payload: bytes) -> int:
    """Simple additive checksum over everything before it, mod 65536."""
    return sum(payload) & 0xFFFF


def make_block_reply(addr: int, length: int) -> bytes:
    data = block_data_for(addr, length)
    head = b"W" + struct.pack(">I", addr) + bytes([length])
    body = head + data
    csum = checksum(body)
    return body + struct.pack(">H", csum)


def parse_block_reply(reply: bytes):
    """Returns (addr, length, data, ok) -- ok is False if truncated or the
    checksum doesn't match (corruption), never raises on short input."""
    if len(reply) < 6 or reply[0:1] != b"W":
        return None, None, None, False
    addr = struct.unpack(">I", reply[1:5])[0]
    length = reply[5]
    expected_total = 8 + length
    if len(reply) != expected_total:
        return addr, length, reply[6:], False  # short/long frame
    data = reply[6:6 + length]
    csum_recv = struct.unpack(">H", reply[6 + length:8 + length])[0]
    csum_calc = checksum(reply[:6 + length])
    return addr, length, data, (csum_recv == csum_calc) and (data == block_data_for(addr, length))


WRITE_ACK_LEN = 6


def make_write_cmd(addr: int, data: bytes) -> bytes:
    head = b"W" + struct.pack(">I", addr) + bytes([len(data)])
    body = head + data
    csum = checksum(body)
    return body + struct.pack(">H", csum)


def parse_write_cmd(cmd: bytes):
    """Returns (addr, length, data, ok) -- mirrors parse_block_reply's shape.
    ok is False on truncation or a checksum mismatch, never raises."""
    if len(cmd) < 6 or cmd[0:1] != b"W":
        return None, None, None, False
    addr = struct.unpack(">I", cmd[1:5])[0]
    length = cmd[5]
    expected_total = 8 + length
    if len(cmd) != expected_total:
        return addr, length, cmd[6:], False
    data = cmd[6:6 + length]
    csum_recv = struct.unpack(">H", cmd[6 + length:8 + length])[0]
    csum_calc = checksum(cmd[:6 + length])
    return addr, length, data, (csum_recv == csum_calc)


def make_write_ack(addr: int, ok: bool = True) -> bytes:
    return b"A" + struct.pack(">I", addr) + bytes([0x00 if ok else 0xFF])


def parse_write_ack(ack: bytes):
    """Returns (addr, ok) -- ok False on truncation, wrong tag, or a
    negative-status byte from the simulated radio (e.g. checksum it rejected)."""
    if len(ack) != WRITE_ACK_LEN or ack[0:1] != b"A":
        return None, False
    addr = struct.unpack(">I", ack[1:5])[0]
    return addr, ack[5] == 0x00
