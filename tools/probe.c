/*
 * Asks Wine's setupapi for the COM port list the same way Maverick does
 * (SetupDiClassGuidsFromNameA("Ports") -> SetupDiGetClassDevsA). Only reads
 * the registry; never opens a port, so it's safe with the radio attached.
 *
 * Build:  i686-w64-mingw32-gcc -O2 -o probe.exe tools/probe.c -lsetupapi
 * Run:    WINEPREFIX=~/.wine32-maverick WINEARCH=win32 wine probe.exe
 *
 * Exit 0 = ports found; 1 = "Ports" class not found (fix 1 missing, e.g.
 * reset by a Wine update); 2 = class found but no devices present.
 */
#include <windows.h>
#include <setupapi.h>
#include <stdio.h>
static const GUID PORTS = {0x4D36E978,0xE325,0x11CE,{0xBF,0xC1,0x08,0x00,0x2B,0xE1,0x03,0x18}};
int main(void){
    GUID g[8]; DWORD n=0; char buf[256];
    BOOL r=SetupDiClassGuidsFromNameA("Ports",g,8,&n);
    printf("SetupDiClassGuidsFromNameA(\"Ports\") -> ret=%d count=%lu\n",r,n);
    if(!n) return 1;
    HDEVINFO h=SetupDiGetClassDevsA(&g[0],NULL,NULL,DIGCF_PRESENT);
    SP_DEVINFO_DATA d={sizeof d}; int i;
    for(i=0;SetupDiEnumDeviceInfo(h,i,&d);i++){
        buf[0]=0; SetupDiGetDeviceRegistryPropertyA(h,&d,SPDRP_FRIENDLYNAME,NULL,(BYTE*)buf,sizeof buf,NULL);
        printf("  device %d: %s\n",i,buf);
    }
    printf("present devices: %d\n",i);
    return i?0:2;
}
