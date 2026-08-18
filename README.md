# maverick-wine

Getting the **BridgeCom Maverick CPS** (`Maverick_1.05` — a rebadged AnyTone
AT-D868UV/D890UV programming tool, 32-bit VB6) to talk to the radio under
Wine on Linux, without a Windows VM. **Confirmed working: reads and writes
both verified on real hardware** — a zone was renamed, written to the radio,
and confirmed both on-screen and functionally (changed zone/channel,
transmitted successfully).

Two unrelated bugs had to be fixed. Neither needs a DLL or a VM.

## Fix 1 — COM port list was empty

Wine's `SetupDiClassGuidsFromNameA("Ports")` returns zero GUIDs because Wine
matches the class key's *description* string instead of its *Class* name
value. One registry value fixes it:

```bash
WINEPREFIX=~/.wine32-maverick WINEARCH=win32 \
wine reg add 'HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E978-E325-11CE-BFC1-08002BE10318}' \
  /ve /t REG_SZ /d "Ports" /f
```

Also install the udev rule so the radio gets a stable device name that
survives a power-cycle or re-enumeration (`/dev/ttyACM*` does not):

```bash
sudo cp udev/99-anytone-cps.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

## Fix 2 — reads/writes stalled on a split serial frame

Maverick reads the port the way Windows allows but Linux doesn't guarantee:
one `ReadFile` per frame, no reassembly, no retry. On Linux, `cdc_acm` can
split a USB transfer mid-frame, and when that happens Maverick's state
machine just stops. [`bridge/serial_bridge.py`](bridge/serial_bridge.py) sits
between Wine's COM4 and the radio: writes pass straight through, and reads
get buffered and released only once the line's gone quiet for a few
milliseconds — so Maverick always sees a complete frame.

**You don't run the bridge or Maverick directly — use the launcher**, which
starts the bridge if it isn't already running (reusing one across sessions;
it survives the radio being unplugged/replugged) and then launches Maverick:

```bash
bin/maverick-cps.sh
```

To install a Favorites/Desktop shortcut that does the same thing:

```bash
sed "s|/path/to/maverick-wine|$(pwd)|" desktop/maverick-cps.desktop \
    > ~/.local/share/applications/maverick-cps.desktop
chmod +x ~/.local/share/applications/maverick-cps.desktop
cp ~/.local/share/applications/maverick-cps.desktop ~/Desktop/
```
(Icon is optional — extract one from `Maverick.exe` with `icoutils`
(`wrestool`/`icotool`) into `~/.local/share/icons/hicolor/{16,32}x32/apps/
maverick-cps.png`, or drop the `Icon=` line to use a generic one.)

To stop the bridge: `bin/maverick-cps.sh --stop-bridge`.

## Environment this was built and verified on

| Item | Value |
|---|---|
| OS | Linux Mint 22.3 (Zena), XFCE |
| Wine | WineHQ wine-staging 11.15 |
| Prefix | `~/.wine32-maverick` (`WINEARCH=win32`) |
| Cable | STM32 Virtual ComPort, USB `0483:5740` |

## More

**[README.detailed.md](README.detailed.md)** has the full investigation:
exact captures and measurements behind both fixes, disproven theories (so
they don't get re-tried), the bridge's design tradeoffs, every stress-test
result, and the real-hardware read and write evidence.
