# Builds the Maverick CPS setupapi.dll proxy shim.
#
# This is a genuine PE32 DLL (built with the MinGW-w64 i686 cross compiler,
# NOT winegcc's ELF-based winelib format -- see README.md "Why MinGW, not
# winegcc" for why that distinction turned out to matter on modern Wine).
#
# setupapi.def forwards every real setupapi.dll export to a renamed copy of
# the original file (setupapi_wine_orig.dll) except the six SetupDi*
# functions we override. Regenerate it with `make regen-def` if the
# reference export list in ../reference/ is ever updated for a new Wine
# version.

CC      = i686-w64-mingw32-gcc
DLL     = setupapi.dll
CFLAGS  = -Wall
DEF     = setupapi.def
SRC     = setupapi.c
EXPORTS_LIST = ../reference/setupapi_exports_wine-11.15-staging_win32.txt

.PHONY: all clean regen-def

all: $(DLL)

$(DLL): $(SRC) $(DEF)
	$(CC) $(CFLAGS) -shared -o $@ $(SRC) $(DEF) -Wl,--kill-at

regen-def: setupapi_gen_forward.py $(EXPORTS_LIST)
	python3 setupapi_gen_forward.py $(EXPORTS_LIST) > $(DEF)

clean:
	$(RM) $(DLL)
