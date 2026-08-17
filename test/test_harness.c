/*
 * test_harness.c — headless console exerciser for the Maverick setupapi.dll
 * shim. Calls the exact same six SetupDi* entry points, in the exact same
 * order, that Maverick.exe is confirmed (via `strings` on the binary) to
 * use, and prints what comes back. No GUI, no window, safe to run
 * unattended over SSH/CI with no display at all.
 *
 * Expected output on success:
 *   class guid resolved for "Ports"
 *   one device enumerated
 *   friendly name = "STM32 Virtual ComPort (COM4)" (or whatever
 *     MAVERICK_SHIM_* env vars say)
 *   PortName registry value = "COM4"
 */

#include <windows.h>
#include <setupapi.h>
#include <initguid.h>
#include <devguid.h>
#include <stdio.h>

static void fail(const char *step)
{
    fprintf(stderr, "FAIL at %s: GetLastError=%lu\n", step, (unsigned long)GetLastError());
    exit(1);
}

int main(void)
{
    GUID guids[4];
    DWORD required = 0;
    HDEVINFO set;
    SP_DEVINFO_DATA devInfo;
    char nameBuf[256];
    DWORD nameLen = 0, regType = 0;
    HKEY hKey;
    char portName[64];
    DWORD portNameLen = sizeof(portName);
    DWORD portNameType = 0;
    LONG rc;

    printf("== 1. SetupDiClassGuidsFromNameA(\"Ports\") ==\n");
    if (!SetupDiClassGuidsFromNameA("Ports", guids, 4, &required)) {
        fail("SetupDiClassGuidsFromNameA");
    }
    printf("   required=%lu, guid[0]={%08lx-...}\n", (unsigned long)required, (unsigned long)guids[0].Data1);
    if (!IsEqualGUID(&guids[0], &GUID_DEVCLASS_PORTS)) {
        fprintf(stderr, "FAIL: returned GUID does not match GUID_DEVCLASS_PORTS\n");
        return 1;
    }

    printf("== 2. SetupDiGetClassDevsA(&GUID_DEVCLASS_PORTS, ...) ==\n");
    set = SetupDiGetClassDevsA(&guids[0], NULL, NULL, DIGCF_PRESENT);
    if (set == INVALID_HANDLE_VALUE) fail("SetupDiGetClassDevsA");
    printf("   got device info set handle\n");

    printf("== 3. SetupDiEnumDeviceInfo(index=0) ==\n");
    devInfo.cbSize = sizeof(devInfo);
    if (!SetupDiEnumDeviceInfo(set, 0, &devInfo)) fail("SetupDiEnumDeviceInfo(0)");
    printf("   got device, DevInst=%lu\n", (unsigned long)devInfo.DevInst);

    printf("== 3b. SetupDiEnumDeviceInfo(index=1) should fail with ERROR_NO_MORE_ITEMS ==\n");
    if (SetupDiEnumDeviceInfo(set, 1, &devInfo)) {
        fprintf(stderr, "FAIL: expected no more items but got a second device\n");
        return 1;
    }
    if (GetLastError() != ERROR_NO_MORE_ITEMS) {
        fprintf(stderr, "FAIL: expected ERROR_NO_MORE_ITEMS, got %lu\n", (unsigned long)GetLastError());
        return 1;
    }
    printf("   correctly reported no more items\n");

    /* re-fetch index 0 since devInfo may be undefined after the failed call */
    devInfo.cbSize = sizeof(devInfo);
    if (!SetupDiEnumDeviceInfo(set, 0, &devInfo)) fail("SetupDiEnumDeviceInfo(0) refetch");

    printf("== 4. SetupDiGetDeviceRegistryPropertyA(SPDRP_FRIENDLYNAME) ==\n");
    if (!SetupDiGetDeviceRegistryPropertyA(set, &devInfo, SPDRP_FRIENDLYNAME, &regType,
                                            (PBYTE)nameBuf, sizeof(nameBuf), &nameLen)) {
        fail("SetupDiGetDeviceRegistryPropertyA");
    }
    printf("   friendly name = \"%s\" (regType=%lu, len=%lu)\n", nameBuf, (unsigned long)regType, (unsigned long)nameLen);

    printf("== 5. SetupDiOpenDevRegKey(...) + real RegQueryValueExA(\"PortName\") ==\n");
    hKey = SetupDiOpenDevRegKey(set, &devInfo, DICS_FLAG_GLOBAL, 0, DIREG_DEV, KEY_QUERY_VALUE);
    if (hKey == INVALID_HANDLE_VALUE) fail("SetupDiOpenDevRegKey");

    rc = RegQueryValueExA(hKey, "PortName", NULL, &portNameType, (BYTE *)portName, &portNameLen);
    if (rc != ERROR_SUCCESS) {
        fprintf(stderr, "FAIL: RegQueryValueExA(PortName) rc=%ld\n", rc);
        return 1;
    }
    printf("   PortName = \"%s\"\n", portName);
    RegCloseKey(hKey);

    printf("== 6. SetupDiDestroyDeviceInfoList ==\n");
    if (!SetupDiDestroyDeviceInfoList(set)) fail("SetupDiDestroyDeviceInfoList");
    printf("   destroyed cleanly\n");

    printf("\nALL CHECKS PASSED\n");
    return 0;
}
