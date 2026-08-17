/*
 * setupapi.c — minimal drop-in setupapi.dll shim for BridgeCom Maverick CPS
 *   (Maverick_1.05, a rebadged AnyTone AT-D868UV/D890UV VB6 programming tool)
 *   running under Wine.
 *
 * PROBLEM:
 *   Maverick.exe enumerates serial ports via the Windows PnP "Ports" device
 *   class (SetupDiGetClassDevsA + friends) instead of probing COM1..COMn or
 *   reading HKLM\HARDWARE\DEVICEMAP\SERIALCOMM directly. Wine's real
 *   setupapi.dll only reports devices that were actually instantiated
 *   through its internal virtual bus (winebus.sys); a USB-serial/CDC-ACM
 *   device mapped by hand via a dosdevices symlink is invisible to it no
 *   matter what static registry entries exist. See maverick_wine_troubleshooting.md
 *   in the LLM-Markdown-Guides repo for the full diagnosis.
 *
 * APPROACH:
 *   Replace setupapi.dll for this one process (drop-in copy in the app's
 *   own install folder — same trick already used for VERSION.DLL and
 *   msimg32.dll — Wine's DLL search order picks the local copy over the
 *   system one). This shim only implements the exact six SetupDi* entry
 *   points Maverick.exe actually calls (confirmed via exhaustive `strings`
 *   scan of Maverick.exe — see README.md). It fabricates a single-device
 *   "Ports" class enumeration result describing one known-good COM port,
 *   configured via environment variables so nothing is hardcoded at build
 *   time. SetupDiOpenDevRegKey hands back a REAL registry key (via the
 *   real, unmodified advapi32 RegCreateKeyExA/RegSetValueExA) pre-populated
 *   with a PortName value — so the app's own subsequent RegQueryValueExA
 *   call needs no interception at all.
 *
 * PRECEDENT:
 *   Same technique used by radiomanV/TL866's wine32 setupapi.dll wrapper
 *   (https://github.com/radiomanV/TL866/tree/master/wine32) — a custom
 *   winelib setupapi.dll dropped into the app folder, built from a .spec
 *   exporting only the functions the target app needs.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <windows.h>
#include <setupapi.h>

#include <initguid.h> /* #defines INITGUID and re-includes guiddef.h so devguid.h below
                          actually *defines* the GUIDs instead of just declaring them extern */
#include <devguid.h>

/* ---- debug logging, gated by env var so it's silent by default ---- */
static int debug_enabled(void)
{
    static int checked = 0, enabled = 0;
    if (!checked) { enabled = getenv("MAVERICK_SHIM_DEBUG") != NULL; checked = 1; }
    return enabled;
}
#define SHIM_TRACE(...) do { if (debug_enabled()) { fprintf(stderr, "[maverick-shim] " __VA_ARGS__); fflush(stderr); } } while (0)

/* ---- configuration, read once from environment ---- */
typedef struct {
    char com_port[16];      /* e.g. "COM4" */
    char friendly_name[128];/* e.g. "STM32 Virtual ComPort (COM4)" */
} shim_config_t;

static void get_config(shim_config_t *cfg)
{
    const char *com = getenv("MAVERICK_SHIM_COM");
    const char *name = getenv("MAVERICK_SHIM_FRIENDLYNAME");

    strncpy(cfg->com_port, com ? com : "COM4", sizeof(cfg->com_port) - 1);
    cfg->com_port[sizeof(cfg->com_port) - 1] = '\0';

    if (name) {
        strncpy(cfg->friendly_name, name, sizeof(cfg->friendly_name) - 1);
        cfg->friendly_name[sizeof(cfg->friendly_name) - 1] = '\0';
    } else {
        snprintf(cfg->friendly_name, sizeof(cfg->friendly_name),
                  "STM32 Virtual ComPort (%s)", cfg->com_port);
    }
}

/* ---- our fake device-info-set handle ---- */
#define FAKE_HANDLE_MAGIC 0x4D415645u /* 'MAVE' */
typedef struct {
    DWORD magic;
    BOOL  device_valid; /* we only ever expose exactly one device */
} fake_devinfo_set_t;

static BOOL is_our_handle(HDEVINFO h)
{
    fake_devinfo_set_t *p = (fake_devinfo_set_t *)h;
    return p != NULL && p != (fake_devinfo_set_t *)INVALID_HANDLE_VALUE
           && p->magic == FAKE_HANDLE_MAGIC;
}

/* ---------------------------------------------------------------------- */

BOOL WINAPI SetupDiClassGuidsFromNameA(LPCSTR ClassName, LPGUID ClassGuidList,
                                        DWORD ClassGuidListSize, PDWORD RequiredSize)
{
    SHIM_TRACE("SetupDiClassGuidsFromNameA(\"%s\")\n", ClassName ? ClassName : "(null)");

    if (!ClassName || lstrcmpiA(ClassName, "Ports") != 0) {
        if (RequiredSize) *RequiredSize = 0;
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }

    if (RequiredSize) *RequiredSize = 1;

    if (ClassGuidListSize < 1) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }

    ClassGuidList[0] = GUID_DEVCLASS_PORTS;
    return TRUE;
}

HDEVINFO WINAPI SetupDiGetClassDevsA(const GUID *ClassGuid, LPCSTR Enumerator,
                                      HWND hwndParent, DWORD Flags)
{
    fake_devinfo_set_t *set;

    SHIM_TRACE("SetupDiGetClassDevsA(flags=0x%lx)\n", (unsigned long)Flags);

    if (!ClassGuid || !IsEqualGUID(ClassGuid, &GUID_DEVCLASS_PORTS)) {
        /* Not a request for the Ports class — we don't fake anything else. */
        SetLastError(ERROR_INVALID_CLASS);
        return (HDEVINFO)INVALID_HANDLE_VALUE;
    }

    set = HeapAlloc(GetProcessHeap(), 0, sizeof(*set));
    if (!set) {
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return (HDEVINFO)INVALID_HANDLE_VALUE;
    }
    set->magic = FAKE_HANDLE_MAGIC;
    set->device_valid = TRUE;

    return (HDEVINFO)set;
}

BOOL WINAPI SetupDiEnumDeviceInfo(HDEVINFO DeviceInfoSet, DWORD MemberIndex,
                                   PSP_DEVINFO_DATA DeviceInfoData)
{
    SHIM_TRACE("SetupDiEnumDeviceInfo(index=%lu)\n", (unsigned long)MemberIndex);

    if (!is_our_handle(DeviceInfoSet) || !DeviceInfoData) {
        SetLastError(ERROR_INVALID_HANDLE);
        return FALSE;
    }

    if (MemberIndex != 0) {
        SetLastError(ERROR_NO_MORE_ITEMS);
        return FALSE;
    }

    if (DeviceInfoData->cbSize != sizeof(SP_DEVINFO_DATA)) {
        SetLastError(ERROR_INVALID_USER_BUFFER);
        return FALSE;
    }

    DeviceInfoData->ClassGuid = GUID_DEVCLASS_PORTS;
    DeviceInfoData->DevInst = 1;
    DeviceInfoData->Reserved = 0;
    return TRUE;
}

BOOL WINAPI SetupDiGetDeviceRegistryPropertyA(HDEVINFO DeviceInfoSet, PSP_DEVINFO_DATA DeviceInfoData,
                                               DWORD Property, PDWORD PropertyRegDataType,
                                               PBYTE PropertyBuffer, DWORD PropertyBufferSize,
                                               PDWORD RequiredSize)
{
    shim_config_t cfg;
    DWORD needed;

    SHIM_TRACE("SetupDiGetDeviceRegistryPropertyA(property=0x%lx)\n", (unsigned long)Property);

    if (!is_our_handle(DeviceInfoSet) || !DeviceInfoData) {
        SetLastError(ERROR_INVALID_HANDLE);
        return FALSE;
    }

    if (Property != SPDRP_DEVICEDESC && Property != SPDRP_FRIENDLYNAME) {
        SetLastError(ERROR_INVALID_DATA);
        return FALSE;
    }

    get_config(&cfg);
    needed = (DWORD)strlen(cfg.friendly_name) + 1;

    if (RequiredSize) *RequiredSize = needed;
    if (PropertyRegDataType) *PropertyRegDataType = REG_SZ;

    if (!PropertyBuffer || PropertyBufferSize < needed) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }

    memcpy(PropertyBuffer, cfg.friendly_name, needed);
    return TRUE;
}

HKEY WINAPI SetupDiOpenDevRegKey(HDEVINFO DeviceInfoSet, PSP_DEVINFO_DATA DeviceInfoData,
                                  DWORD Scope, DWORD HwProfile, DWORD KeyType, REGSAM samDesired)
{
    shim_config_t cfg;
    HKEY hKey = NULL;
    LONG rc;

    SHIM_TRACE("SetupDiOpenDevRegKey(scope=%lu, keytype=%lu)\n",
               (unsigned long)Scope, (unsigned long)KeyType);

    if (!is_our_handle(DeviceInfoSet) || !DeviceInfoData) {
        SetLastError(ERROR_INVALID_HANDLE);
        return INVALID_HANDLE_VALUE;
    }

    get_config(&cfg);

    /* Real registry key, via the real (unmodified) advapi32 — this is the
     * one and only piece of actual persistent state the shim creates.
     * Everything the app does with the returned HKEY afterwards (its own
     * RegQueryValueExA("PortName", ...) call) is completely untouched,
     * genuine Win32 registry code. */
    rc = RegCreateKeyExA(HKEY_CURRENT_USER,
                          "Software\\MaverickWineShim\\Port0\\Device Parameters",
                          0, NULL, REG_OPTION_NON_VOLATILE, KEY_ALL_ACCESS, NULL,
                          &hKey, NULL);
    if (rc != ERROR_SUCCESS) {
        SetLastError((DWORD)rc);
        return INVALID_HANDLE_VALUE;
    }

    rc = RegSetValueExA(hKey, "PortName", 0, REG_SZ,
                         (const BYTE *)cfg.com_port, (DWORD)strlen(cfg.com_port) + 1);
    if (rc != ERROR_SUCCESS) {
        RegCloseKey(hKey);
        SetLastError((DWORD)rc);
        return INVALID_HANDLE_VALUE;
    }

    SHIM_TRACE("  -> PortName=%s (HKCU\\Software\\MaverickWineShim\\Port0\\Device Parameters)\n",
               cfg.com_port);
    return hKey;
}

BOOL WINAPI SetupDiDestroyDeviceInfoList(HDEVINFO DeviceInfoSet)
{
    SHIM_TRACE("SetupDiDestroyDeviceInfoList()\n");

    if (!is_our_handle(DeviceInfoSet)) {
        SetLastError(ERROR_INVALID_HANDLE);
        return FALSE;
    }

    HeapFree(GetProcessHeap(), 0, (LPVOID)DeviceInfoSet);
    return TRUE;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    return TRUE;
}
