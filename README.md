# maverick-setupapi-shim

A `setupapi.dll` proxy shim to make BridgeCom's Maverick CPS (`Maverick_1.05`,
a rebadged AnyTone AT-D868UV/D890UV programming tool) see its USB serial
cable under Wine on Linux, so the radio can be read/written without a
Windows VM.

Full background/diagnosis lives in `maverick_wine_troubleshooting.md` in the
separate `LLM-Markdown-Guides` repo — this repo is deliberately kept apart
from that documentation repo (own version control, own lifecycle).

**Status as of 2026-08-17: PAUSED. The shim builds and its logic is correct (validated
headlessly), but testing against the real `Maverick.exe` with the real radio physically
connected overturned the diagnosis this shim was built against — see "What actually happened
when tested for real" below. Not resuming without a new lead on the real mechanism.**

---

## The problem

`Maverick.exe` enumerates serial ports via the Windows PnP "Ports" device
class (`SetupDiGetClassDevsA` + friends) rather than probing `COM1..COMn` or
reading `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` directly (confirmed by an
exhaustive `strings` scan of the binary — see "Confirmed API surface"
below). Wine's real `setupapi.dll` only reports devices actually
instantiated through its internal virtual bus (`winebus.sys`); a
USB-serial/CDC-ACM device mapped by hand via a `dosdevices` symlink is
invisible to it no matter what static registry entries exist. Static
registry pre-population was already tried and confirmed not to work — see
the troubleshooting log.

## Confirmed API surface

An exhaustive `strings -a` scan of `Maverick.exe` for `SetupDi*` symbols
turned up exactly six functions (VB6 apps resolve Win32 calls at runtime via
`Declare` statements, not a static PE import table, so this scan — not
`winedump -j import` — is the authoritative source):

```
SetupDiClassGuidsFromNameA
SetupDiGetClassDevsA
SetupDiEnumDeviceInfo
SetupDiGetDeviceRegistryPropertyA
SetupDiOpenDevRegKey
SetupDiDestroyDeviceInfoList
```

Also present: `RegOpenKeyA`, `RegOpenKeyExA`, `RegQueryValueExA`. Combined
with `SetupDiOpenDevRegKey`, this is the textbook Microsoft
"enumerate-Ports-class-then-open-each-device's-registry-key-for-PortName"
idiom — a well-documented standard pattern, not something bespoke to
Maverick. That's why the shim can be built with confidence against a known
contract instead of guessing.

## Approach

`src/setupapi.c` implements those six functions to fabricate a single-device
"Ports" class enumeration result describing one known-good COM port,
configured via environment variables (nothing hardcoded at build time —
see "Configuration"). `SetupDiOpenDevRegKey` hands back a **real** registry
key (via real, unmodified `RegCreateKeyExA`/`RegSetValueExA`) pre-populated
with a `PortName` value, so the app's own subsequent `RegQueryValueExA` call
needs no interception at all — only one function actually touches genuine
persistent state.

### Why a full forwarding proxy, not a simple app-folder drop-in

The original plan (same trick already used in the working install for
`VERSION.DLL`/`msimg32.dll`: drop a same-named file next to `Maverick.exe`
and rely on Wine's DLL search order preferring the app directory) **does
not work for `setupapi.dll` on this Wine version (wine-staging 11.15)**.
Empirically confirmed:

- A same-directory drop-in is silently ignored — Wine always loads its own
  registered builtin from `system32` for this DLL, regardless of
  `WINEDLLOVERRIDES` (tried env var *and* the `HKCU\Software\Wine\DllOverrides`
  registry key directly, both `native` and `native,builtin` modes, with and
  without `WINEDLLPATH`). `setupapi` appears to be one of a set of "core"
  DLL names Wine's loader resolves straight from its internal module table,
  bypassing the normal app-directory search Windows itself would do.
- Replacing the **system32** copy directly *does* get picked up — but
  naively replacing it wholesale breaks Wine's own internals: `wineboot`'s
  `services.exe` also calls into `setupapi.dll` (for
  `SetupDiGetClassDevsW`, which our shim didn't implement) and crashed on
  startup, hanging on Wine's built-in unhandled-exception debugger prompt.
- Fix: a full **forwarding proxy**. Rename the real `system32\setupapi.dll`
  to `setupapi_wine_orig.dll`, and build our replacement as a `.def`-driven
  DLL that forwards **every one of the real DLL's 617 exports** straight
  through to that renamed original via PE export forwarding (zero glue code
  — the PE loader does it) — except the six functions above, which get our
  own implementation instead. Confirmed empirically that a renamed copy of
  Wine's builtin still resolves and loads correctly by name.

This is a standard DLL-proxy pattern (same idea used by many game-mod
loaders), just applied at the Wine builtin level. It's a **bigger blast
radius than originally planned** — system32 replacement inside the prefix,
not just an app-folder file — see "Backout plan" below, which is exactly
why this got scoped that way.

### Why MinGW, not winegcc

The first build attempt used `winegcc` (Wine's own Winelib toolchain),
matching the technique documented by
[radiomanV/TL866's `wine32` setupapi.dll wrapper](https://github.com/radiomanV/TL866/tree/master/wine32)
— the real-world precedent this whole approach is modeled on. `winegcc`
produces a Linux ELF `.so` that gets renamed to `.dll` (`mv setupapi.dll.so
setupapi.dll`, literally what TL866's own Makefile does). That format loads
fine when placed directly in `system32`, but modern Wine's loader would not
pick it up via the app-directory search that TL866's README assumes — likely
why TL866's own `wine32` wrapper now carries a "no longer maintained, see
the actively-maintained [wine64 broker-based] replacement" notice; something
changed in how modern Wine's builtin loader handles this on-disk format.
Switching to the **MinGW-w64 `i686-w64-mingw32-gcc`** cross-compiler produces
a genuine PE32 DLL, indistinguishable on disk from a real Windows DLL, which
sidesteps the question entirely — this is the same reason many DLL-proxy
projects for Windows software (cracks, mods, wrappers) use MinGW rather than
any Wine-specific toolchain.

## Configuration

Set before launching the app (or bake into a wrapper script/`.desktop`
launcher's `Exec=` line):

| Variable | Default | Purpose |
|---|---|---|
| `MAVERICK_SHIM_COM` | `COM4` | The COM port name to report (must match a real, working `dosdevices` mapping) |
| `MAVERICK_SHIM_FRIENDLYNAME` | `STM32 Virtual ComPort (<COM>)` | The device description string returned for `SPDRP_FRIENDLYNAME`/`SPDRP_DEVICEDESC` |
| `MAVERICK_SHIM_DEBUG` | unset | If set to anything, trace every shim call to stderr |

Currently hardcoded to Wine's `COM4` default, matching the existing
`dosdevices/com4 → /dev/ttyACM0` mapping already set up in
`~/.wine32-maverick` (see the troubleshooting log). A more thorough version
could read `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` at runtime instead of
relying on an env var, but that's unnecessary complexity for a prototype we
haven't even confirmed against the real app yet.

## Building

```bash
sudo apt install gcc-mingw-w64-i686   # one-time; genuine-PE cross toolchain
cd src && make
```

Produces `src/setupapi.dll`.

## Testing (headless, no hardware, no GUI needed)

`test/test_harness.c` is a console-only program that calls the exact same
six functions, in the exact same order, that `Maverick.exe` is confirmed to
use, and prints what comes back — no display, no radio, safe to run over
SSH.

```bash
cd test
i686-w64-mingw32-gcc -o test_harness_mingw.exe test_harness.c -lsetupapi -ladvapi32

# Use a disposable prefix -- NEVER the real ~/.wine32-maverick -- for this:
WINEPREFIX=~/.wine32-shim-test WINEARCH=win32 \
  WINEDLLOVERRIDES="mscoree=d;mshtml=d" wine wineboot -u   # first time only

SYS=~/.wine32-shim-test/drive_c/windows/system32
cp "$SYS/setupapi.dll" "$SYS/setupapi_wine_orig.dll"   # only if not done already
cp ../src/setupapi.dll "$SYS/setupapi.dll"

MAVERICK_SHIM_DEBUG=1 wine test_harness_mingw.exe
```

Expected: `ALL CHECKS PASSED`, with a trace showing the fabricated Ports
class GUID, one enumerated device, the friendly name string, and
`PortName = "COM4"` read back through a completely real, unmodified
`RegQueryValueExA` call.

**This has been run and passes** (2026-08-17) in a throwaway
`~/.wine32-shim-test` prefix — confirmed Wine's own `wineboot`/`services.exe`
still start cleanly with the forwarding proxy installed (the failure mode
from the naive non-forwarding attempt does not recur).

## What actually happened when tested for real

Tested against the real `Maverick.exe` (in a disposable **copy** of
`~/.wine32-maverick`, never the real prefix — see "Backout plan"), with the
user actively present and the real radio physically connected. Three
independent tests, all negative:

1. **Shim installed, device already connected before launch.** `Set → Set
   COM` opens, Comm Port list is empty — same symptom as the original bug.
   A Wine relay trace scoped to `setupapi.dll`, captured across the exact
   click that opens the dialog, shows **zero calls into setupapi.dll at
   all**. Not "returns empty" — never called. An unscoped relay trace of the
   same click (865,000 lines) confirms it: 100% VB6/OLE string-and-heap
   management noise, zero `CreateFile`, zero serial-API calls of any kind.
2. **Real, unmodified Wine builtin (no shim), device already connected
   before launch.** Same empty list.
3. **Real, unmodified Wine builtin, live physical unplug/replug of the
   cable while the dialog was already open** (matching the dialog's own
   hint text: "plug in the cable... select the new COM port that
   appears") — testing whether this is a live hot-plug-notification driven
   list rather than a static enumeration. `dmesg` confirms the real USB
   re-enumeration happened. A relay trace on both `setupapi.dll` and
   `user32.dll`, running throughout, shows **zero activity in response**.

**Conclusion: whatever actually drives this dialog's Comm Port list is not
`setupapi.dll`, at least not in any code path this dialog exercises.** The
`SetupDi*`/`SetupDiOpenDevRegKey` symbols really are in the binary
(confirmed via `strings`), and the shim built against them is logically
correct and fully working on its own terms — but that's very likely not the
actual mechanism this specific dialog depends on. Paused here rather than
keep guessing at the real mechanism blind; see "Next steps" below.

## Next steps (if picked back up)

- **Jeremiah Mulford's own file/description**, if it ever arrives — the
  most direct way to shortcut this, since he's apparently gotten it working.
- **A full, unscoped relay trace across the entire dialog lifecycle**
  (not just the open-click) — expensive (~15MB/sec of trace output for this
  app) but would show definitively what API surface, if any, this dialog
  actually touches. Not attempted yet because it's a much bigger,
  more open-ended investment than anything tried so far.
- **Ask BridgeCom support directly** (option 2 from the original
  troubleshooting log) whether this is a known issue with a documented
  Linux workaround, given the dialog's own hint text implies specific
  expected behavior (list should update live as you plug in the cable) that
  doesn't seem to be implemented via any observable Windows API on this
  Wine version.

### Why no unattended UI automation (earlier in this session)

Before the user was actively present and the radio was connected, I held
off on driving `xdotool` clicks through Maverick's menus unattended — this
machine's `DISPLAY` was the user's actual live foreground desktop (VS Code,
browser tabs, terminal all visibly in use). A stray click landing on the
wrong window is exactly the kind of outward-facing, hard-to-reverse action
worth asking about first. All UI automation actually performed happened
later, with the user actively watching and having explicitly OK'd it.

---

## Backout plan

Three layers, cheapest-to-revert first:

### 1. Just stop / delete this repo
Nothing in `~/.wine32-maverick` (the real, working prefix) has been touched
by any of this. Nothing needs undoing if you decide not to proceed —
`rm -rf ~/Documents/maverick-setupapi-shim` and it's as if this never
happened.

### 2. Disposable test prefixes
Two, both throwaway, both safe to `rm -rf` any time with no impact on
anything else:
- `~/.wine32-shim-test` — minimal prefix, used for the headless test harness
  runs. Has the proxy DLL installed (`system32/setupapi.dll` replaced,
  original renamed to `setupapi_wine_orig.dll`).
- `~/.wine32-maverick-shimtest` — a full `cp -a` copy of the real
  `~/.wine32-maverick`, used for testing against the actual `Maverick.exe`
  with real registry/settings state. Also has the proxy installed the same
  way. This is the one the real-app testing in "What actually happened
  when tested for real" used.

### 3. If deployed to the real `~/.wine32-maverick` prefix
Deployment (not yet done) would mean, inside that prefix's `system32`:
```bash
SYS=~/.wine32-maverick/drive_c/windows/system32
cp "$SYS/setupapi.dll" "$SYS/setupapi_wine_orig.dll"   # back up the real one first
cp src/setupapi.dll "$SYS/setupapi.dll"                # install the proxy
```
**To revert:** exactly one command, no reinstall, no Wine package changes:
```bash
cp "$SYS/setupapi_wine_orig.dll" "$SYS/setupapi.dll"
rm "$SYS/setupapi_wine_orig.dll"   # optional cleanup
```
That's it — the same fix used earlier this session when `winehq-devel`
accidentally got installed over `winehq-staging`: identify what got
swapped, swap it back, verify with a smoke-test launch of `Maverick.exe`
before considering it done.

### If Jeremiah's DLL/documentation arrives first
Nothing here is wasted regardless: the confirmed six-function API surface
and the "why app-folder drop-in doesn't work on modern Wine, use a
forwarding proxy" finding both apply no matter whose implementation ends up
installed. If his file is simpler (e.g. he's on an older Wine version where
the app-folder trick still works), that's a strictly easier deployment than
ours — worth comparing before committing to the system32 proxy approach for
real. Either way, swapping his file in means the same single-file revert as
above, nothing more.

---

## Repo layout

```
src/
  setupapi.c              the shim implementation
  setupapi.def             generated forwarding table (regenerate via `make regen-def`)
  setupapi_gen_forward.py  generates setupapi.def from a captured export list
  Makefile
test/
  test_harness.c           headless console exerciser, no GUI/hardware needed
reference/
  setupapi_exports_wine-11.15-staging_win32.txt   captured export list this .def was generated from
udev/
  (reserved for a udev rule if/when one proves necessary -- current testing
  hasn't needed one since ~/.wine32-maverick's dosdevices/com4 mapping and
  dialout-group permissions already work; Jeremiah's mention of a udev rule
  was likely just for a stable device node, same idea)
```
