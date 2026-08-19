# maverick-wine

I wanted to get the **BridgeCom Maverick CPS** (`Maverick_1.05` — a rebadged AnyTone
AT-D868UV/D890UV programming tool, 32-bit VB6) to work on Linux, without a Windows VM. I absolutely leaned heavily on Agentic AI, in this case, Claude Code, to figure out that Wine was the appropriate solution. However, a basic install of Wine wouldn't work. We ran into two issues.

## Fix 1 — COM port list was empty

Wine's `SetupDiClassGuidsFromNameA("Ports")` returns zero GUIDs because Wine
matches the class key's *description* string instead of its *Class* name
value. One registry value fixes it:

```bash
WINEPREFIX=~/.wine32-maverick WINEARCH=win32 \
wine reg add 'HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E978-E325-11CE-BFC1-08002BE10318}' \
  /ve /t REG_SZ /d "Ports" /f
```

Also, installed a udev rule so the radio gets a stable device name that
survives a power-cycle or re-enumeration (`/dev/ttyACM*` does not):

```bash
sudo cp udev/99-anytone-cps.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

That fix allowed the CPS to see the COM port successfully.

## Fix 2 — reads/writes stalled on a split serial frame

The CPS reads the port from the radio the way Windows allows but Linux doesn't guarantee:
one `ReadFile` per frame, no reassembly, no retry. On Linux, `cdc_acm` can
split a USB transfer mid-frame, and when that happens The CPS's state
machine just stops. A bridge was created. [`bridge/serial_bridge.py`](bridge/serial_bridge.py) sits
between Wine's COM4 and the radio: writes pass straight through, and reads
get buffered and released only once the line's gone quiet for a few
milliseconds — so the CPS always sees a complete frame.

**You don't run the bridge or the CPS directly — use the launcher**, which
starts the bridge if it isn't already running (reusing one across sessions;
it survives the radio being unplugged/replugged) and then launches the CPS:

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
