"""
Linux Phase 0, step two: prove the Windows guard's GPU slowdown works on Linux, then undo it.

This does exactly what gpu_mitigation.GpuMitigator does when a pin goes over threshold, on the
ROG Astral only, for a few seconds, at idle:

    1. save the current power limit
    2. lock the graphics clock to the floor (the enumerated minimum, else 500 / 700 / 1000 MHz)
    3. drop the power limit to the card's minimum
    4. hold, printing clock, power draw and power limit once a second
    5. restore the saved power limit and read it back to prove it (one retry, like v1.4's F5)
    6. reset the clock lock

Both settings are volatile: a reboot or a driver reload clears them anyway. If this program is
killed hard in the middle, the recovery commands it prints before changing anything put the
card back, and so does `sudo python3 tools/nvml_limit_test_linux.py --recover`, which reads
the saved values from a marker in /run (a tmpfs, so a reboot clears the marker together with
the settings it describes).

Run it (root is required to change limits, and it asks before touching anything):
    sudo python3 tools/nvml_limit_test_linux.py
    sudo python3 tools/nvml_limit_test_linux.py --hold 20
    sudo python3 tools/nvml_limit_test_linux.py --recover

It refuses to start if the card is already drawing more than --max-start-watts (default 150 W),
so the first run happens at idle. Nothing here touches I2C.

Exit codes:
    0  limits applied, observed and restored, restore verified
    1  environment problem (not Linux, not root, a marker from an earlier run, busy GPU, declined)
    2  libnvidia-ml.so.1 missing, or nvmlInit failed
    3  no supported ROG Astral found (see --allow-unknown)
    4  the restore could not be verified; the marker is kept and the recovery commands printed
    5  the driver refused both changes; nothing was applied, nothing needed restoring
"""

import argparse
import json
import os
import signal
import sys
import time
from typing import Callable, List, Optional

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import ctypes  # noqa: E402

import nvml_probe_linux as probe  # noqa: E402  (also puts the repo root on sys.path)
import astral_i2c  # noqa: E402

MARKER_PATH = "/run/12vhpwr-guard-limit-test.json"
DEFAULT_HOLD_SECONDS = 10
DEFAULT_MAX_START_WATTS = 150
# How far above the lock target the graphics clock may read and still count as held. The
# driver reports the effective clock, which sits a step or two off the requested value.
CLOCK_HOLD_TOLERANCE_MHZ = 60


class LimitNvml(probe.ReadOnlyNvml):
    """The probe's getters plus the three calls the guard's mitigation uses. Nothing else."""

    def _bind_getters(self) -> None:
        super()._bind_getters()
        dev = ctypes.c_void_p
        self._bind("nvmlDeviceSetPowerManagementLimit", ctypes.c_int, [dev, ctypes.c_uint32])
        self._bind("nvmlDeviceSetGpuLockedClocks", ctypes.c_int,
                   [dev, ctypes.c_uint32, ctypes.c_uint32])
        self._bind("nvmlDeviceResetGpuLockedClocks", ctypes.c_int, [dev])

    def set_power_limit_mw(self, handle, milliwatts: int) -> None:
        self._call("nvmlDeviceSetPowerManagementLimit", handle, ctypes.c_uint32(int(milliwatts)))

    def set_locked_clocks(self, handle, min_mhz: int, max_mhz: int) -> None:
        self._call("nvmlDeviceSetGpuLockedClocks", handle,
                   ctypes.c_uint32(int(min_mhz)), ctypes.c_uint32(int(max_mhz)))

    def reset_locked_clocks(self, handle) -> None:
        self._call("nvmlDeviceResetGpuLockedClocks", handle)


class Interrupted(Exception):
    """SIGTERM or SIGHUP mid-test. Raised so the restore in `finally` still runs."""


def _raise_interrupted(signum, frame):
    raise Interrupted(f"signal {signum}")


# ----------------------------------------------------------------- matching
def find_astral(nvml, allow_unknown: bool):
    """(handle, subsystem id, bus id) of the card to test, or None. Same rule as the guard:
    a known Astral wins; a single GPU is accepted only with --allow-unknown."""
    found = []
    for index in range(nvml.device_count()):
        try:
            handle = nvml.handle(index)
            sub, bus = nvml.pci_info(handle)
        except Exception:
            continue
        found.append((handle, sub, bus))
    for entry in found:
        if entry[1] in astral_i2c.ASTRAL_SUBSYSTEM_IDS:
            return entry
    if allow_unknown and len(found) == 1:
        return found[0]
    return None


# ------------------------------------------------------------------- marker
def write_marker(path: str, bus_id: str, saved_mw: Optional[int]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"bus_id": bus_id, "saved_power_limit_mw": saved_mw, "time": time.time()}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_marker(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_marker(path: str) -> None:
    for p in (path + ".tmp", path):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def recovery_commands(bus_id: str, saved_mw: Optional[int], gpu_uv_present: bool = False) -> List[str]:
    cmds = []
    if saved_mw:
        watts = f"{saved_mw / 1000:.0f}" if saved_mw % 1000 == 0 else f"{saved_mw / 1000:.2f}"
        cmds.append(f"sudo nvidia-smi -i {bus_id} -pl {watts}")
    # With gpu-uv installed a plain clock reset (-rgc) would leave its offset uncapped, so
    # its own apply puts back the lock, power limit and offset in its validated order.
    cmds.append("sudo gpu-uv apply" if gpu_uv_present else f"sudo nvidia-smi -i {bus_id} -rgc")
    return cmds


def restore_clock_lock(nvml, handle, floor: Optional[int], gpu_uv) -> "tuple[str, Optional[str]]":
    """Undo our floor lock. Returns (what happened, error or None).

    Without gpu-uv this is a plain reset, exactly like the Windows guard. With gpu-uv the
    owner's own lock of [lowest supported clock, max_clock_mhz] goes back instead, because a
    reset would leave gpu-uv's positive offset with no cap. When that lock cannot be rebuilt
    (unreadable config, or no enumerated lowest clock) our floor lock stays: slow and safe,
    never an untested overclock.
    """
    present, cap = gpu_uv
    if not present:
        try:
            nvml.reset_locked_clocks(handle)
            return "clock lock reset", None
        except Exception as ex:
            return "clock lock reset", str(ex)
    if not cap:
        return ("gpu-uv lock not restored",
                "gpu-uv config unreadable, floor lock left in place; run `sudo gpu-uv apply`")
    if not floor:
        return ("gpu-uv lock not restored",
                "driver did not enumerate the lowest clock, floor lock left in place; "
                "run `sudo gpu-uv apply`")
    try:
        nvml.set_locked_clocks(handle, floor, cap)
        return f"clock lock back to gpu-uv's {floor}-{cap} MHz", None
    except Exception as ex:
        return f"clock lock back to gpu-uv's {floor}-{cap} MHz", str(ex)


# ---------------------------------------------------------------- the test
def verify_power_limit(nvml, handle, saved_mw: int, sleep: Callable[[float], None]) -> Optional[str]:
    """None when the card reports the saved limit, else the reason. Mirrors v1.4's F5:
    read back, on a mismatch set once more, settle 0.1 s, read again."""
    actual = None
    for attempt in (1, 2):
        if attempt == 2:
            sleep(0.1)
        try:
            actual = nvml.power_limit_mw(handle)
        except Exception as ex:
            return f"could not read the limit back ({ex}), expected {saved_mw} mW"
        if actual == saved_mw:
            return None
        if attempt == 1:
            try:
                nvml.set_power_limit_mw(handle, saved_mw)
            except Exception as ex:
                return f"card reports {actual} mW, expected {saved_mw} mW (retry refused: {ex})"
    return f"card reports {actual} mW, expected {saved_mw} mW"


def lock_clocks(nvml, handle, floor: Optional[int], say) -> Optional[int]:
    """Lock to the first floor the driver accepts. Returns the MHz used, or None."""
    candidates = ([floor] if floor else []) + list(probe.CLOCK_FLOOR_LADDER)
    seen = set()
    for mhz in candidates:
        if mhz in seen:
            continue
        seen.add(mhz)
        try:
            nvml.set_locked_clocks(handle, mhz, mhz)
            say(f"  clock lock at {mhz} MHz     accepted")
            return mhz
        except Exception as ex:
            say(f"  clock lock at {mhz} MHz     refused ({ex})")
    return None


def _sample(nvml, handle) -> str:
    clock, _ = probe._try(nvml.graphics_clock_mhz, handle)
    usage, _ = probe._try(nvml.power_usage_mw, handle)
    limit, _ = probe._try(nvml.power_limit_mw, handle)
    return (f"clock {clock if clock is not None else 'n/a'} MHz, "
            f"draw {probe._watts(usage)}, limit {probe._watts(limit)}")


def run_limit_test(nvml, handle, bus_id: str, *, hold_seconds: float, marker_path: str,
                   gpu_uv=(False, None),
                   say: Callable[[str], None] = print,
                   sleep: Callable[[float], None] = time.sleep) -> int:
    """Engage like the guard, hold, release like the guard, verify. Returns the exit code."""
    saved_mw = nvml.power_limit_mw(handle)
    min_mw, max_mw = nvml.power_limit_constraints_mw(handle)
    floor = nvml.min_supported_graphics_clock_mhz(handle)

    say("Before")
    say(f"  {_sample(nvml, handle)}")
    say(f"  saved power limit      {probe._watts(saved_mw)} (range {probe._watts(min_mw)} "
        f"to {probe._watts(max_mw)})")
    say(f"  clock floor            {f'{floor} MHz (enumerated)' if floor else 'not enumerated, using the ladder'}")
    if gpu_uv[0]:
        say(f"  gpu-uv                 installed, its clock lock goes back on release "
            f"(cap {f'{gpu_uv[1]} MHz' if gpu_uv[1] else 'UNREADABLE'})")
    say("If this program dies before it finishes, these put the card back:")
    for cmd in recovery_commands(bus_id, saved_mw, gpu_uv[0]):
        say(f"  {cmd}")
    say("")

    write_marker(marker_path, bus_id, saved_mw)
    locked_at = None
    applied_power = False
    held_ok = []
    verify_warning = None
    clock_reset_error = None
    try:
        say("Engage (clocks first, the same order as the guard)")
        locked_at = lock_clocks(nvml, handle, floor, say)
        try:
            nvml.set_power_limit_mw(handle, min_mw)
            applied_power = True
            say(f"  power limit {probe._watts(min_mw)}       accepted")
        except Exception as ex:
            say(f"  power limit {probe._watts(min_mw)}       refused ({ex})")

        if locked_at is None and not applied_power:
            say("")
            say("The driver refused both changes, so nothing was applied.")
            return 5

        say("")
        say(f"Hold for {hold_seconds:g} s")
        steps = max(1, int(round(hold_seconds)))
        for i in range(steps):
            sleep(1.0)
            clock, _ = probe._try(nvml.graphics_clock_mhz, handle)
            if locked_at is not None and clock is not None:
                held_ok.append(clock <= locked_at + CLOCK_HOLD_TOLERANCE_MHZ)
            say(f"  {i + 1:>3}s  {_sample(nvml, handle)}")
    finally:
        # Same order as GpuMitigator.release(): the power limit first, proven, then the clocks.
        say("")
        say("Release")
        if applied_power:
            try:
                nvml.set_power_limit_mw(handle, saved_mw)
            except Exception as ex:
                verify_warning = f"restore refused ({ex})"
            else:
                verify_warning = verify_power_limit(nvml, handle, saved_mw, sleep)
            say(f"  power limit back to {probe._watts(saved_mw)}  "
                f"{'verified' if verify_warning is None else 'NOT VERIFIED: ' + verify_warning}")
        if locked_at is not None:
            what, clock_reset_error = restore_clock_lock(nvml, handle, floor, gpu_uv)
            say(f"  {what}  " + ("ok" if clock_reset_error is None
                                 else f"FAILED ({clock_reset_error})"))
        if verify_warning is None and clock_reset_error is None:
            clear_marker(marker_path)

    sleep(3.0)
    say("")
    say("After (3 s to settle)")
    say(f"  {_sample(nvml, handle)}")
    say("")
    say("Results")
    say(f"  clock lock             "
        + (f"applied at {locked_at} MHz" if locked_at is not None else "REFUSED at every floor"))
    if locked_at is not None and held_ok:
        say(f"  clock held at the lock {sum(held_ok)}/{len(held_ok)} samples within "
            f"{CLOCK_HOLD_TOLERANCE_MHZ} MHz")
    say(f"  power limit drop       {'applied' if applied_power else 'REFUSED'}")
    if applied_power:
        say(f"  power limit restore    {'verified' if verify_warning is None else 'NOT VERIFIED'}")
    if locked_at is not None:
        say(f"  clock lock released    {'ok' if clock_reset_error is None else 'FAILED'}")

    if verify_warning is not None or clock_reset_error is not None:
        say("")
        say(f"The card may not be fully restored. The marker stays at {marker_path}.")
        say("Run these, or `sudo python3 tools/nvml_limit_test_linux.py --recover`:")
        for cmd in recovery_commands(bus_id, saved_mw, gpu_uv[0]):
            say(f"  {cmd}")
        return 4
    return 0


def recover(nvml, handle, bus_id: str, marker: dict, marker_path: str,
            gpu_uv=(False, None),
            say: Callable[[str], None] = print,
            sleep: Callable[[float], None] = time.sleep) -> int:
    """Put back what an interrupted run left behind, from its marker."""
    saved_mw = marker.get("saved_power_limit_mw")
    ok = True
    if isinstance(saved_mw, int) and saved_mw > 0:
        try:
            nvml.set_power_limit_mw(handle, saved_mw)
            warning = verify_power_limit(nvml, handle, saved_mw, sleep)
        except Exception as ex:
            warning = f"restore refused ({ex})"
        say(f"power limit back to {probe._watts(saved_mw)}: "
            f"{'verified' if warning is None else 'NOT VERIFIED: ' + warning}")
        ok = warning is None
    else:
        say("the marker holds no saved power limit; leaving the limit alone")
    floor = nvml.min_supported_graphics_clock_mhz(handle)
    what, error = restore_clock_lock(nvml, handle, floor, gpu_uv)
    say(f"{what}: {'ok' if error is None else 'FAILED (' + error + ')'}")
    if error is not None:
        ok = False
    if ok:
        clear_marker(marker_path)
        say("recovered; marker removed")
        return 0
    say(f"not fully recovered; the marker stays at {marker_path}")
    return 4


def main(argv: Optional[List[str]] = None, *, is_root: Optional[Callable[[], bool]] = None,
         nvml_factory=LimitNvml, ask: Callable[[str], str] = input,
         marker_path: str = MARKER_PATH, sleep: Callable[[float], None] = time.sleep,
         gpu_uv_check: Callable[[], tuple] = probe.gpu_uv_clock_cap) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the 12VHPWR Guard GPU slowdown on Linux for a few seconds, then "
                    "restore and verify it. Root only; asks first.")
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_SECONDS, metavar="SECONDS",
                        help=f"how long to hold the limits (default {DEFAULT_HOLD_SECONDS}, max 60)")
    parser.add_argument("--max-start-watts", type=float, default=DEFAULT_MAX_START_WATTS,
                        metavar="W", help="refuse to start above this power draw "
                                          f"(default {DEFAULT_MAX_START_WATTS})")
    parser.add_argument("--yes", action="store_true", help="do not ask before applying")
    parser.add_argument("--recover", action="store_true",
                        help="restore what an interrupted run left behind, then exit")
    parser.add_argument("--allow-unknown", action="store_true",
                        help="accept a single NVIDIA GPU that is not on the Astral allowlist")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux") and is_root is None:
        print("This test is for Linux.", file=sys.stderr)
        return 1
    if not 1 <= args.hold <= 60:
        print("--hold must be between 1 and 60 seconds.", file=sys.stderr)
        return 1
    root_check = is_root or (lambda: os.geteuid() == 0)
    if not root_check():
        print("Changing clocks and power limits needs root: run it with sudo.", file=sys.stderr)
        return 1

    marker = read_marker(marker_path)
    if marker is not None and not args.recover:
        print(f"A marker from an earlier run is at {marker_path}, so the card may still be "
              f"limited. Run with --recover first.", file=sys.stderr)
        return 1
    if args.recover and marker is None:
        print("Nothing to recover: no marker found.")
        return 0

    nvml = nvml_factory()
    try:
        nvml.init()
    except Exception as ex:
        print(f"NVML failed: {ex}", file=sys.stderr)
        return 2

    gpu_uv = gpu_uv_check()
    previous = {}
    try:
        target = find_astral(nvml, args.allow_unknown)
        if target is None:
            print("No supported ROG Astral found (see --allow-unknown).", file=sys.stderr)
            return 3
        handle, sub, bus_id = target
        model = astral_i2c.ASTRAL_SUBSYSTEM_IDS.get(sub, "unknown card")

        if args.recover:
            if marker.get("bus_id") not in (None, bus_id):
                print(f"The marker is for {marker.get('bus_id')}, not {bus_id}; refusing.",
                      file=sys.stderr)
                return 1
            return recover(nvml, handle, bus_id, marker, marker_path, gpu_uv=gpu_uv, sleep=sleep)

        usage, _ = probe._try(nvml.power_usage_mw, handle)
        if usage is not None and usage > args.max_start_watts * 1000:
            print(f"The card is drawing {probe._watts(usage)}, above {args.max_start_watts:g} W. "
                  f"Close anything using the GPU and run again at idle.", file=sys.stderr)
            return 1

        min_mw, _ = nvml.power_limit_constraints_mw(handle)
        print(f"Card: {model} at {bus_id} (subsystem 0x{sub:08X})")
        print(f"This locks the graphics clock to its floor and the power limit to "
              f"{probe._watts(min_mw)} for {args.hold:g} s, then puts both back and checks.")
        if not args.yes:
            if ask("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 1
        print()

        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                previous[sig] = signal.signal(sig, _raise_interrupted)
        try:
            return run_limit_test(nvml, handle, bus_id, hold_seconds=args.hold,
                                  marker_path=marker_path, gpu_uv=gpu_uv, sleep=sleep)
        except (KeyboardInterrupt, Interrupted):
            print("\nInterrupted; the release above ran before exiting.")
            return 4 if read_marker(marker_path) is not None else 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        nvml.shutdown()


if __name__ == "__main__":
    sys.exit(main())
