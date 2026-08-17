#!/usr/bin/env python3
"""Generate a full forwarding .def for the setupapi.dll proxy.

Every real export of Wine's builtin setupapi.dll is forwarded, unmodified,
to a renamed copy of the original file (setupapi_wine_orig.dll) -- except
the handful of functions we actually override, which point at our own
implementation in this DLL instead.

Regenerate this whenever the underlying Wine version's setupapi.dll export
list might have changed:
    objdump -p /path/to/real/setupapi.dll | ... > exports.txt
    python3 setupapi_gen_forward.py exports.txt > setupapi_proxy.def
"""
import sys

OVERRIDDEN = {
    "SetupDiClassGuidsFromNameA",
    "SetupDiGetClassDevsA",
    "SetupDiEnumDeviceInfo",
    "SetupDiGetDeviceRegistryPropertyA",
    "SetupDiOpenDevRegKey",
    "SetupDiDestroyDeviceInfoList",
}
FORWARD_TARGET_MODULE = "setupapi_wine_orig"

def main():
    exports_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/setupapi_exports_clean.txt"
    with open(exports_file) as f:
        names = [line.strip() for line in f if line.strip()]

    print("LIBRARY setupapi.dll")
    print("EXPORTS")
    for name in names:
        if name in OVERRIDDEN:
            print(f"    {name}")
        else:
            print(f"    {name} = {FORWARD_TARGET_MODULE}.{name}")

if __name__ == "__main__":
    main()
