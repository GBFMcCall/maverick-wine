# Builds the headless test harness EXE and stages it next to a copy of the
# shim DLL, exactly mimicking the Wine same-directory DLL search order trick
# used in the real Maverick install (so the harness picks up the local shim
# instead of Wine's real builtin setupapi.dll, with zero registry/override
# changes needed).

CC     = winegcc
EXE    = test_harness.exe
CFLAGS = -m32 -Wall
SRC    = test_harness.c

.PHONY: all clean run

all: $(EXE) setupapi.dll

$(EXE): $(SRC)
	$(CC) $(CFLAGS) -o $@ $(SRC) -lsetupapi

setupapi.dll: ../src/setupapi.dll
	cp ../src/setupapi.dll .

run: all
	wine $(EXE)

clean:
	$(RM) $(EXE) $(EXE).so setupapi.dll
	$(RM) -r tmp*
