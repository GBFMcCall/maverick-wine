# maverick-wine

Getting the **BridgeCom Maverick CPS** (`Maverick_1.05` — a rebadged AnyTone
AT-D868UV/D890UV programming tool, 32-bit VB6) to talk to the radio under Wine
on Linux Mint, without a Windows VM.

> **Repo note.** This started life as `maverick-setupapi-shim`, built around a
> `setupapi.dll` forwarding proxy. **That shim turned out to be unnecessary** —
> the real bug was a Wine registry-matching bug, fixable with one registry
> value. The shim's source and the old README documenting it were removed
> once the real fix was confirmed; `git log` still has them if ever wanted.

**Status: everything works. COM port detection, reads, and writes are all
SOLVED and verified on the real radio — a zone was renamed, written back,
confirmed on-screen, and confirmed functionally (changed zone/channel,
transmitted successfully). See the short [README.md](README.md) for the
two-fix summary; this file is the full investigation and evidence trail.**

---

## Environment

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Zena), XFCE |
| Wine | WineHQ **wine-staging 11.15** (`/opt/wine-staging`) |
| Prefix | `~/.wine32-maverick` (`WINEARCH=win32`) |
| App | `~/.wine32-maverick/drive_c/Program Files/Maverick_1.05/Maverick.exe` |
| Binary | PE32 VB6; sole PE import is `MSVBVM60.DLL` — all Win32 calls are runtime `Declare` |
| Cable | STM32 Virtual ComPort, USB `0483:5740` → `/dev/ttyACM*` |

---

## 1. SOLVED — COM port list was empty

### Root cause: a Wine bug in `SetupDiClassGuidsFromName`

Maverick enumerates serial ports the standard Microsoft way, starting with
`SetupDiClassGuidsFromNameA("Ports")`. Under Wine that call **returns zero
GUIDs**, so the app gives up before ever reaching the enumeration.

Wine matches the class key's **`(Default)` description** (`"Ports (COM & LPT)"`)
instead of its **`Class` name** value (`"Ports"`). Measured directly:

```
SetupDiClassGuidsFromNameA("Ports")             -> ret=1, count=0     (wrong)
SetupDiClassGuidsFromNameA("Ports (COM & LPT)") -> ret=1, count=1     (the description matches)
SetupDiClassNameFromGuidA(GUID_DEVCLASS_PORTS)  -> "Ports"            (contradicts the above)
```

The name↔GUID round-trip does not close. Every class fails identically
(`Display`, `System`, `USB`, `DiskDrive`).

### The fix — one registry value, no DLL

```bash
WINEPREFIX=~/.wine32-maverick WINEARCH=win32 \
wine reg add 'HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E978-E325-11CE-BFC1-08002BE10318}' \
  /ve /t REG_SZ /d "Ports" /f
```

**Revert:** same command with `/d "Ports (COM & LPT)"`.

Forward-compatible: if Wine is ever fixed to match on `Class`, that key already
says `"Ports"`, so it keeps working either way.

### Why the shim is unnecessary

With Wine's **stock, unmodified** `setupapi.dll` (md5 `d45dda00…`, identical to
`/opt/wine-staging/lib/wine/i386-windows/setupapi.dll`), the app's full
six-call sequence now succeeds:

```
SetupDiClassGuidsFromNameA("Ports")  -> 1 GUID {4D36E978-…}
SetupDiGetClassDevsA(PORTS, PRESENT) -> 1 device
  friendly = "STM32 Virtual ComPort (COM4)"
  PortName = "COM4"
```

Wine 11.15 already enumerates the radio correctly. There was never anything to
fabricate. **Do not install `src/setupapi.dll`.**

---

## 2. SOLVED — device node moved on every power-cycle

The radio re-enumerates on power-cycle and when entering PC mode, so the kernel
node hops (`ttyACM0` → `ttyACM1` → …) and Wine's static `dosdevices/com4`
symlink goes dangling. This is almost certainly why a udev rule was mentioned
as part of the original tip.

[`udev/99-anytone-cps.rules`](udev/99-anytone-cps.rules) pins a stable name:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", \
    SYMLINK+="anytone-cps", MODE="0660", GROUP="dialout"
```

```bash
sudo cp udev/99-anytone-cps.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
ln -sfn /dev/anytone-cps ~/.wine32-maverick/dosdevices/com4
```

Verified: after a power-cycle the node moved `ttyACM1` → `ttyACM0` and the
symlink followed automatically, with COM4 still enumerating.

---

## 3. OPEN — read stalls on a partial serial frame

### What actually happens on the wire

Captured with `strace -f -y -e trace=read,write` against the real radio. The
protocol works — this is far past where the original investigation believed
things failed:

```
write(22</dev/ttyACM0>, "PROGRAM", 7)                = 7    handshake
read (22</dev/ttyACM0>, "", 3)                       = 0    empty poll (normal)
read (22</dev/ttyACM0>, "QX\6", 3)                   = 3    radio answers
write(22</dev/ttyACM0>, "\2", 1)                     = 1    request identity
read (22</dev/ttyACM0>, "ID890UV\0\7V100\0\0\6", 16) = 16   radio identifies
write(22</dev/ttyACM0>, "R\3\230\0\0\20", 6)         = 6    block read: 'R' + addr + len
read (22</dev/ttyACM0>, "W…", 24)                    = 24   block reply: 'W' + addr + 16 data + csum
        … 88 successful 24-byte blocks …
write(22</dev/ttyACM0>, "R\1\370\20\0\20", 6)        = 6
read (22</dev/ttyACM0>, "W\1\370\20\0\20…", 24)      = 21   <-- SHORT: 21 of 24
        … no further serial I/O, ever …
```

Read-size distribution across the whole capture:

| bytes | count | meaning |
|---|---|---|
| 24 | **88** | successful blocks |
| 0 | 7 | empty polls — handled fine by the app |
| 3, 16 | 1 each | handshake replies |
| **21** | **1** | the short frame — everything stops here |

### Why

Maverick sets `COMMTIMEOUTS` to *return immediately* (`VMIN=0, VTIME=0`,
confirmed via `stty` while the app held the port). On Windows that is safe:
`usbser.sys` hands up a whole frame per read. On Linux, `cdc_acm` can split a
frame across USB transfers, so an immediate read returns a partial frame. The
app has **no reassembly and no retry** — its state machine simply stops.

Its entire serial API surface is small and confirms this:

```
CreateFileA  SetupComm  GetCommState  SetCommState
SetCommTimeouts  PurgeComm  WriteFile  ReadFile  CloseHandle  Sleep
```

No `ClearCommError`, no overlapped I/O, no `WaitCommEvent`.

---

## Theories that were tested and DISPROVEN

Recorded so nobody re-treads them. Each was killed by measurement, not opinion.

| Theory | Verdict | Evidence |
|---|---|---|
| App enumerates via `SERIALCOMM` / `strings` says no `setupapi` | **Wrong** | ASCII-only `strings` missed UTF-16 literals; VB6 stores literals as UTF-16 (`strings -e l`) |
| App reads `PortSpeed=0` from the 8-NUL-byte `Setting.ini` | **Wrong** | Wine's `GetCommState` returns `BaudRate=9600` on a fresh open, not 0 |
| `speed 0` drops DTR and kills the link | **Wrong** | Measured `DTR=1` via `TIOCMGET` while Wine held the port |
| Wine's serial `ReadFile` is broken | **Wrong** | Reads 8/8 bytes correctly through a raw socat pty pair |
| Wine ignores `ReadIntervalTimeout` | **Wrong** | Returns full 24 bytes on a deliberately split frame with `RI=100ms` |
| App blocks forever inside `ReadFile` | **Wrong** | `/proc/<pid>/task/*/` shows no blocked read; it sits in Wine IPC, then spins |

### Failed workaround — do not repeat

Forcing `stty -F /dev/anytone-cps min 24 time 2` **made things worse**. It
wedged the app at 100% CPU *before any serial I/O at all* — earlier than the
original failure. Cause: with `VMIN>0`, a read with no data yet blocks
indefinitely, because `VTIME`'s inter-byte timer only starts after the first
byte — and the app legitimately does zero-byte poll reads. Reverting the
setting did not recover the wedged process.

The lesson: the fix must distinguish *"wait for the rest of this frame"* from
*"there is no frame coming"*. A kernel tty setting cannot.

---

## Next steps

### 1. Userspace serial bridge — BUILT, verified offline, not yet run against real hardware

```
Maverick (Wine) ──► COM4 → pty ──► bridge ──► /dev/ttyACM0 ──► radio
```

[`bridge/serial_bridge.py`](bridge/serial_bridge.py). Forwards app→radio
writes straight through; on the inbound side, accumulates bytes and releases
them to the app-side pty only once the burst goes idle for `--idle-ms`
(default **15ms**, not the originally-proposed 5ms — see below), so the app
always finds a **complete frame**. Protocol-blind by design: it never parses
command/reply structure, so zero-byte polls on an idle line still return
zero untouched.

### 2. Protocol simulator — BUILT, this is what validated the bridge

[`simulator/`](simulator/) stands in for the radio with no hardware attached:

| File | Role |
|---|---|
| `pty_selftest.py` | Foundation check: raw-mode pty pairs pass all 256 byte values and protocol control bytes unmodified, non-blocking reads on an empty pty return immediately, fragmented writes stay observably fragmented at the raw layer. Run this first if anything above it looks broken. |
| `protocol.py` | Shared frame definitions both ends agree on — handshake/identity sizes match the capture; block replies are self-checking (address-derived data + checksum), so corruption is caught, not just short reads. |
| `fake_radio.py` | Replays the captured handshake/identity/block-read sequence, splitting replies across two writes with a configurable gap (`--split-mode none\|realistic\|aggressive`, `--force-split-at N` to reproduce the exact 88-blocks-then-short-frame capture deterministically). |
| `test_client.py` | Reproduces Maverick's own read behavior: a poll loop of individually non-blocking reads, first nonzero read taken as final. A short or corrupt frame fails the run at that point — exactly how the real app's state machine stops dead. |
| `run_test.sh` | Orchestrates one trial; `--direct` bypasses the bridge as a negative control. |
| `run_reconnect_test.sh` | Exercises the bridge's auto-reconnect (below): downstream disappears mid-test, reappears at the same stable path as a brand-new device, one long-lived bridge survives it. |

**Evidence gathered:**
- `run_test.sh --force-split-at 88 --split-mode none --direct` reproduces the
  README's exact capture: 88 good blocks, then a short 15-of-24 read, client
  stops. Confirms the simulator's failure shape matches the real one.
- The same scenario **through the bridge**: all frames arrive whole,
  including the deliberately-split one.
- Stress batteries through the bridge, bridge default `--idle-ms 15`: **60/60**
  trials with every single reply split (`--split-mode aggressive`) across 60
  blocks each, and **30/30** trials at ~1%-split realistic rate across 300
  blocks each — zero short or corrupt frames.
- Negative control: the same aggressive-split load with `--direct` (no
  bridge) fails every time (15/15) — confirms the test isn't vacuously
  passing.

**Two real bugs found and fixed by this testing, both the same root cause:**
closing a pty's master fd **unconditionally discards any bytes still queued
for the slave to read**, no matter how long you wait first — measured
directly (writing a payload, delaying the close by up to 100ms with the
reader untouched, then finding the reader gets nothing every time). The fix
is polling `TIOCINQ` down to 0 before closing, not a fixed delay:
1. `serial_bridge.py` was dropping its last buffered frame if the downstream
   device disconnected right after sending it.
2. `fake_radio.py` had the identical bug on its own pty when exiting after
   its last configured block — this was surfacing as false bridge failures
   that were actually a test-harness bug, not a bridge bug.

**Tuning note:** the first stress run at `--idle-ms 5` (the README's original
proposal) showed a ~20% spurious failure rate. Root cause was measured, not
guessed: Python's `time.sleep()` for a nominal 3ms gap overshot to 5.4ms
under load in a standalone benchmark, leaving ~zero margin against a 5ms
idle threshold. Raising to 15ms eliminated it across 100+ subsequent trials.
This says nothing yet about the right value against the *real* radio's
actual inter-transfer gaps — that needs measuring on hardware, see next.

### 3. Real hardware — READ CONFIRMED WORKING

```bash
python3 bridge/serial_bridge.py --downstream /dev/anytone-cps \
    --upstream-symlink ~/.wine32-maverick/dosdevices/com4 \
    --idle-ms 15 --verbose --log ~/maverick-bridge-realtest.log
```
...with Maverick launched normally afterward (`WINEPREFIX=~/.wine32-maverick
WINEARCH=win32 wine Maverick.exe`) and its Read operation run manually.

**Result:** full read completed, all data displayed correctly in the CPS —
no stall, no short frame, no crash. First time this has gotten past the ~88
block mark.

**What the log actually shows** (6547 block exchanges, 157076 bytes in from
the radio) — read this precisely, not as "the split bug got hit and fixed
again on hardware":
- Every one of the 6547 replies arrived in a single downstream read
  (`assembled-from-multiple-reads: 0`). No split was observed on the wire in
  this run. The original captured split may simply be rare/timing-dependent
  enough not to recur every run — this run doesn't confirm or rule that out,
  it just confirms nothing broke.
- The real protocol structurally matches what `simulator/protocol.py`
  guessed from the original capture (`PROGRAM`→`QX\x06`, `\x02`→`ID890UV…`,
  `R`+4-byte-addr+len → `W`+addr+len+data+checksum) — good independent
  confirmation the simulator was testing the right shape.
- **The read was noticeably slower than on Windows, and the log explains
  why, precisely:** average flush latency was 15.23ms — i.e. almost exactly
  the configured `--idle-ms 15`, on nearly every single block, whether or
  not that block was ever split. Total read time was 109.4s over 6547
  exchanges = 16.70ms/exchange average, meaning the artificial idle wait
  accounts for essentially all of the per-block time, not radio/USB latency.
  Since this run saw zero real splits, most of that 15ms tax bought nothing
  this time — it's the cost of the bridge simply not knowing a frame is
  complete without waiting out the idle window. Lowering `--idle-ms` would
  likely speed this up substantially; how far it can safely go needs
  measuring against a real split's actual inter-fragment gap, which this run
  didn't capture.
- The bridge exited itself ~3s after the app sent `END`/got the `\x06` ack,
  logging `downstream EOF` — consistent with the radio leaving programming
  mode and re-enumerating (README section 2), not an error.

**Fixed:** the bridge no longer exits on a downstream disconnect. It closes
the stale fd, queues any app writes that arrive in the meantime, and retries
opening `--downstream` on an interval (`--reconnect-interval-ms`, default
200ms) until it reappears — the upstream pty and `--upstream-symlink` (i.e.
Wine's COM4 mapping) are created once and never touched again. Verified
offline with `simulator/run_reconnect_test.sh` (5/5 clean runs): one bridge
process carries two full client sessions across a simulated
disconnect/reconnect, with the app-side device path unchanged throughout.
This depends on `--downstream` being a *stable* path that gets re-pointed at
the new device node underneath (which is exactly what the udev rule in
section 2 gives `/dev/anytone-cps`) — a raw `/dev/ttyACM*` path would not
survive a re-enumeration. Start the bridge once per boot/session; only
Ctrl-C stops it now. **This was then confirmed live**, not just offline: the
same bridge process that answered one real read survived the radio dropping
off ~16s later (`downstream disconnected (EOF)`, heartbeat logs every 5s)
and reconnected on its own with no restart, ready for the next session.

**`--idle-ms` tuning — settled on 4ms, by measurement, not by guessing:**
three full reads (6547 blocks / 157076 bytes each) run back-to-back on the
real radio at decreasing thresholds:

| `--idle-ms` | total time | avg / exchange | real splits seen |
|---|---|---|---|
| 15 | 109.4s | 16.70ms | 0 |
| 8 | 63.5s | 9.69ms | 0 |
| 4 | 37.0s | 5.65ms | 0 |

Time tracks the idle threshold almost exactly (a constant ~1.5-1.7ms/block
on top of it), confirming again the wait itself is nearly the entire
per-block cost. **None of the three runs hit a real split** — so this is not
proof 4ms safely covers a genuine split's inter-fragment gap on this
hardware, only proof the bridge holds up at 4ms and reads complete reliably
at that setting. Settled here by explicit choice (diminishing returns below
this, and the simulator's own dev notes put a 5ms threshold's failure floor
around scheduling jitter alone) — if a future read ever fails, raise
`--idle-ms` back up first, before suspecting the reassembly logic itself.
Bridge default is now `--idle-ms 4`.

### 4. Why not a shim DLL for this part

The technique works — the old `setupapi` forwarding proxy did load and forward
all 617 exports. But the serial calls resolve through `Declare Lib "kernel32"`,
so intercepting `ReadFile` means replacing **`kernel32.dll`**: loaded before
almost everything, depended on by Wine's own `services.exe`/`wineboot`, mostly
forwarders into `kernelbase`/`ntdll`, and it would intercept `ReadFile` for the
app's `.rdt` and DAO/Jet file I/O too. Far more blast radius than the bridge,
for the same interception.

### 4. Still unexplained (neither blocks progress)

- Wine leaves the CDC-ACM tty at `speed 0` while setting a pty's speed
  correctly. Appears **cosmetic** — CDC-ACM firmware ignores line coding and
  DTR stays asserted — but the asymmetry is not understood.
- Intermittent `STATUS_FLOAT_INEXACT_RESULT` (`0xc000008f`) crash at startup —
  an unmasked x87 exception, a known VB6-under-Wine failure mode. Occurred once
  in ~6 launches.

### 5. Caveat before trusting a write — offline write-path testing done, real write not yet attempted

The bridge is a workaround for an app assuming Windows USB-serial buffering.
Reads use a consistent 24-byte frame, but **writes to the radio may use
different framing**. A read now completes cleanly (section 3) — before
deliberately testing a real write, the simulator was extended to a `--mode
write` (`fake_radio.py`, `test_client.py`, `protocol.py`): the app now sends
the large frame and a short 6-byte ack comes back, the reverse size ratio
from reads. This framing is **invented, not captured** — there is no real
write trace to mirror — so it proves the bridge's mechanism (byte-perfect
outbound pass-through under volume, correct reassembly of a split *ack*),
not the real protocol's actual bytes. It also cannot simulate genuine
outbound USB-transfer splitting (a pty has no packet boundaries at all,
confirmed in `pty_selftest.py`'s test 5) — that risk, if it exists on the
real hardware, is simply unmeasured.

**Finding that changes the write-day plan:** the current bridge default
(`--idle-ms 4`, tuned against reads' 24-byte blocks) is **not** safe margin
for a short 6-byte ack under worst-case conditions:

| `--idle-ms` | aggressive split (every ack split) | realistic split (~1%) |
|---|---|---|
| 4 (current live default) | **36/60 FAILED (40%)** | 30/30 passed |
| 8 | 60/60 passed | (not separately run — 8 already covers 4's failure cases) |
| 15 | 60/60 passed | 30/30 passed |

A 6-byte ack split into fragments has even less absolute margin than a
24-byte block reply does, so the same jitter that reads tolerated at 4ms
isn't safe here. **Recommendation: use `--idle-ms 15` for the first real
write**, not the bridge's current live 4ms — correctness matters more than
speed when the failure mode is corrupted radio memory instead of a stalled
read. Revisit lowering it only after a write has succeeded and been verified.

### 6. Real write — CONFIRMED WORKING, twice

First real write, `--idle-ms 15` (the conservative recommendation above): a
Zone renamed from "Depew" to "Testing", written back to the radio. **9642
write blocks, 9646 total exchanges, zero split or corrupt acks, three
downstream disconnects along the way (moving between write/zone-change/
transmit operations) — the bridge survived every one automatically, no
restart needed.** 157.4s total, 16.32ms/exchange, tracking `--idle-ms`
exactly, same pattern as the reads. Verified two ways, not just by
Maverick's own success message: the renamed zone displayed correctly, and a
live functional test (changed zone, changed channel, transmitted
successfully to another radio) confirmed the radio operates correctly
afterward.

**Then confirmed the real ack is smaller and safer than the offline model
assumed:** the log showed the actual write frame is 24 bytes (`W`+addr+len+
data+checksum, matching what the simulator guessed) with a **single-byte**
`0x06` ack — not the invented 6-byte ack the offline stress testing used. A
single byte cannot arrive partially split, so the specific failure mode that
made `--idle-ms 4` unsafe in section 5's testing doesn't apply to the real
protocol. Re-ran the same rename-and-write at `--idle-ms 4`: **4525+ blocks
in, zero split acks, meaningfully faster**, before the zone was renamed back
to "Depew" to confirm repeatability. Settled: `--idle-ms 4` for both reads
and writes going forward.

Startup was also hardened after this: originally the bridge hard-crashed if
`--downstream` didn't exist yet (hit for real when the radio was found
powered off mid-session). Fixed to share the same wait/retry logic a
mid-session disconnect already used — the upstream pty/COM4 symlink comes up
immediately either way, so a shortcut-launched bridge no longer needs the
radio to already be on. See [`bin/maverick-cps.sh`](bin/maverick-cps.sh) and
the short README for the launcher this enabled.

---

## Changes made to this machine, and how to revert

| Change | Revert |
|---|---|
| Registry `Class` default → `"Ports"` in `~/.wine32-maverick` | `wine reg add …{4D36E978-…} /ve /t REG_SZ /d "Ports (COM & LPT)" /f` |
| `/etc/udev/rules.d/99-anytone-cps.rules` | `sudo rm /etc/udev/rules.d/99-anytone-cps.rules && sudo udevadm control --reload-rules` |
| `dosdevices/com4` → `/dev/anytone-cps` (was a dangling `ttyACM0`) | repoint or delete the symlink |
| Removed stale `dosdevices/com33`, `com5`, `com6` | recreate if wanted (they were duplicates/test artifacts) |
| `HKCU\Software\Wine\Debug` `RelayInclude` set for tracing | `wine reg delete 'HKCU\Software\Wine\Debug' /v RelayInclude /f` |
| `Bash(stty -F /dev/anytone-cps:*)` allow-rule in `~/.claude/settings.json` | remove the entry (backup: `~/.claude/settings.json.bak`) |
| Disposable prefixes `~/.wine32-shim-test`, `~/.wine32-maverick-shimtest` | `rm -rf` either, no impact elsewhere |
| Passwordless sudo `/etc/sudoers.d/claude-full` (from an earlier session) | `sudo rm /etc/sudoers.d/claude-full` when no longer wanted |
| `icoutils` package installed (to extract Maverick's icon) | `sudo apt-get remove icoutils` |
| Icon at `~/.local/share/icons/hicolor/{16x16,32x32}/apps/maverick-cps.png` | `rm` both files |
| Desktop entry `~/.local/share/applications/maverick-cps.desktop` and `~/Desktop/maverick-cps.desktop` | `rm` both files |
| `src/`, `test/`, `reference/`, `bin/maverick-run.sh`, old superseded README removed (shim-era detritus) | `git checkout` an earlier commit if any of it is wanted back |

---

## Repo layout

```
README.md                              short two-fix overview, start here
README.detailed.md                     this file -- full investigation and evidence trail
udev/99-anytone-cps.rules              stable device node                    [IN USE]
bin/maverick-cps.sh                    launcher: re-applies fix 1 if a Wine
                                        update reset it, starts the bridge if
                                        needed, then launches Maverick       [IN USE]
tools/probe.c                          setupapi COM-port enumeration probe
bridge/serial_bridge.py                userspace serial bridge               [IN USE]
desktop/maverick-cps.desktop           shortcut template (see README.md to install)
simulator/                             protocol simulator + test harness, no hardware needed
  pty_selftest.py                        foundation: raw pty byte-fidelity check
  protocol.py                            shared frame definitions (fake_radio + test_client)
  fake_radio.py                          stands in for the radio, deliberately splits frames
  test_client.py                         reproduces Maverick's own short-read-prone behavior
  run_test.sh                            orchestrates one trial; --direct = negative control
  run_reconnect_test.sh                  exercises the bridge's auto-reconnect
```

## Useful commands

```bash
# what does Wine's setupapi actually report? (exit 1 = fix 1 missing)
i686-w64-mingw32-gcc -O2 -o probe.exe tools/probe.c -lsetupapi
WINEPREFIX=~/.wine32-maverick WINEARCH=win32 wine probe.exe

# watch the serial layer
WINEDEBUG=+comm wine Maverick.exe

# capture the actual wire traffic (the decisive tool)
strace -f -y -s 256 -e trace=read,write,openat -o trace.log wine Maverick.exe
grep 'ttyACM' trace.log

# line settings while the app holds the port
stty -F /dev/anytone-cps -a
```
