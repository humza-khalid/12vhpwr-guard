"""
Development harness: emulates HWiNFO's SM2 shared-memory block.

Lets 12VHPWR Guard be exercised without HWiNFO or a supported GPU. Not part of the
shipped tool - it is here so the reconnect/stale-data paths can be tested at all.

Usage (two consoles):

    # console 1 - the emulator
    python tools/fake_hwinfo_sm2.py

    # console 2 - the guard, pointed at the emulator
    set HWINFO_SM2_NAME=Local\\HWiNFO_SENS_SM2_TEST
    set HWINFO_SM2_MUTEX=Local\\HWiNFO_SM2_MUTEX_TEST
    python hwinfo_12vhpwr_guard.py

Commands:
    set <amps> [pin]  set all pins (or one 1-6) to a current
    dead              write the DEAD signature, as HWiNFO does when it exits
    alive             restore the live signature
    freeze            stop advancing the timestamp, as a hung HWiNFO would
    unfreeze          resume advancing the timestamp
    rename            rename pin 1's user label (name_original still matches)
    status            print current state
    quit              exit

WARNING: driving values above the guard's threshold for longer than its sustained
window will shut the machine down for real. Raise sustained_seconds_required in
config.ini while testing.
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hwinfo_12vhpwr_guard import (  # noqa: E402
    HWINFO_MAGIC,
    PIN_LABELS,
    HWiNFOEntry,
    HWiNFOHeader,
)

MAP_NAME = os.environ.get("HWINFO_SM2_NAME", r"Local\HWiNFO_SENS_SM2_TEST")
MUTEX_NAME = os.environ.get("HWINFO_SM2_MUTEX", r"Local\HWiNFO_SM2_MUTEX_TEST")

# Deliberately far below 8 MB: the guard must map whatever size the section actually is.
MAP_SIZE = 64 * 1024
ENTRY_SECTION_OFFSET = 4096

DEAD_MAGIC = int.from_bytes(b"DEAD", "little")

PAGE_READWRITE = 0x04
FILE_MAP_ALL_ACCESS = 0x000F001F
INVALID_HANDLE_VALUE = wt.HANDLE(-1)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CreateFileMappingW = kernel32.CreateFileMappingW
CreateFileMappingW.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
CreateFileMappingW.restype = wt.HANDLE

MapViewOfFile = kernel32.MapViewOfFile
MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
MapViewOfFile.restype = wt.LPVOID

UnmapViewOfFile = kernel32.UnmapViewOfFile
UnmapViewOfFile.argtypes = [wt.LPCVOID]
UnmapViewOfFile.restype = wt.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wt.HANDLE]
CloseHandle.restype = wt.BOOL

CreateMutexW = kernel32.CreateMutexW
CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
CreateMutexW.restype = wt.HANDLE

WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
WaitForSingleObject.restype = wt.DWORD

ReleaseMutex = kernel32.ReleaseMutex
ReleaseMutex.argtypes = [wt.HANDLE]
ReleaseMutex.restype = wt.BOOL


def _fixed(value: str, size: int) -> bytes:
    """Encode to exactly `size` bytes so no stale characters survive a rename."""
    return value.encode("utf-8")[: size - 1].ljust(size, b"\x00")


class FakeSM2:
    """Writes a valid SM2 block that 12VHPWR Guard can read."""

    def __init__(self, map_name: str = MAP_NAME, mutex_name: str = MUTEX_NAME, size: int = MAP_SIZE):
        self.values = [2.0] * len(PIN_LABELS)
        self.labels = list(PIN_LABELS)
        self.alive = True
        self.frozen = False

        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.h_map = CreateFileMappingW(
            INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, size, map_name
        )
        if not self.h_map:
            raise RuntimeError(f"CreateFileMappingW failed (WinErr={ctypes.get_last_error()})")

        p_view = MapViewOfFile(self.h_map, FILE_MAP_ALL_ACCESS, 0, 0, 0)
        if not p_view:
            CloseHandle(self.h_map)
            raise RuntimeError(f"MapViewOfFile failed (WinErr={ctypes.get_last_error()})")

        self.view = int(ctypes.cast(p_view, ctypes.c_void_p).value)
        ctypes.memset(self.view, 0, size)

        self.h_mutex = CreateMutexW(None, False, mutex_name)
        if not self.h_mutex:
            raise RuntimeError(f"CreateMutexW failed (WinErr={ctypes.get_last_error()})")

        self.write_once()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(1.0):
            self.write_once()

    def write_once(self) -> None:
        with self.lock:
            values = list(self.values)
            labels = list(self.labels)
            alive = self.alive
            frozen = self.frozen

        WaitForSingleObject(self.h_mutex, 2000)
        try:
            entry_size = ctypes.sizeof(HWiNFOEntry)

            header = HWiNFOHeader.from_address(self.view)
            header.magic = HWINFO_MAGIC if alive else DEAD_MAGIC
            header.version = 1
            header.version2 = 0
            if not frozen:
                header.last_update = int(time.time())
            header.sensor_section_offset = 0
            header.sensor_element_size = 0
            header.sensor_element_count = 0
            header.entry_section_offset = ENTRY_SECTION_OFFSET
            header.entry_element_size = entry_size
            header.entry_element_count = len(PIN_LABELS)

            base = self.view + ENTRY_SECTION_OFFSET
            for i, canonical in enumerate(PIN_LABELS):
                e = HWiNFOEntry.from_address(base + i * entry_size)
                e.type = 4
                e.sensor_index = 0
                e.id = i
                e.name_original = _fixed(canonical, 128)
                e.name_user = _fixed(labels[i], 128)
                e.unit = _fixed("A", 16)
                e.value = values[i]
                e.value_min = 0.0
                e.value_max = values[i]
                e.value_avg = values[i]
        finally:
            ReleaseMutex(self.h_mutex)

    def set_value(self, amps: float, pin: int = 0) -> None:
        with self.lock:
            if pin:
                self.values[pin - 1] = amps
            else:
                self.values = [amps] * len(PIN_LABELS)
        self.write_once()

    def set_alive(self, alive: bool) -> None:
        with self.lock:
            self.alive = alive
        self.write_once()

    def set_frozen(self, frozen: bool) -> None:
        with self.lock:
            self.frozen = frozen
        if not frozen:
            self.write_once()

    def rename_pin(self, pin: int, label: str) -> None:
        with self.lock:
            self.labels[pin - 1] = label
        self.write_once()

    def status(self) -> str:
        with self.lock:
            return (
                f"values={['%.2f' % v for v in self.values]} alive={self.alive} "
                f"frozen={self.frozen} labels[0]={self.labels[0]!r}"
            )

    def close(self) -> None:
        self.stop_event.set()
        try:
            UnmapViewOfFile(ctypes.c_void_p(self.view))
        except Exception:
            pass
        for h in (self.h_mutex, self.h_map):
            try:
                CloseHandle(h)
            except Exception:
                pass


def main() -> None:
    sm2 = FakeSM2()
    print(f"Serving {MAP_NAME} ({MAP_SIZE} bytes) / {MUTEX_NAME}")
    print(f"{len(PIN_LABELS)} pin sensors at 2.00 A. Type 'help' for commands.")

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()

            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print("set <amps> [pin] | dead | alive | freeze | unfreeze | rename | status | quit")
            elif cmd == "set" and len(parts) >= 2:
                pin = int(parts[2]) if len(parts) > 2 else 0
                sm2.set_value(float(parts[1]), pin)
                print(sm2.status())
            elif cmd == "dead":
                sm2.set_alive(False)
                print("Signature set to DEAD.")
            elif cmd == "alive":
                sm2.set_alive(True)
                print("Signature restored.")
            elif cmd == "freeze":
                sm2.set_frozen(True)
                print("Timestamp frozen.")
            elif cmd == "unfreeze":
                sm2.set_frozen(False)
                print("Timestamp advancing.")
            elif cmd == "rename":
                sm2.rename_pin(1, "Renamed By User")
                print("Pin 1 user label renamed; name_original unchanged.")
            elif cmd == "status":
                print(sm2.status())
            else:
                print("Unknown command. Type 'help'.")
    finally:
        sm2.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
