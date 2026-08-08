"""
Phase 2 proof-of-concept for the v1.1 tiered response: prove that engaging the GPU
slowdown really does cut connector current, and that releasing it puts the card back
exactly as it was.

Run this at the desktop, not mid-game. The engage test locks the GPU to its floor
clock for the duration, which is exactly what the guard will do in an emergency.

Usage:
    python tools/mitigation_poc.py                  # read-only status, no elevation needed
    python tools/mitigation_poc.py --engage-test    # engage, watch, release  (ADMIN console)
    python tools/mitigation_poc.py --engage-test --seconds 15
    python tools/mitigation_poc.py --simulate-crash # engage, then die without releasing
    python tools/mitigation_poc.py --recover        # clean up after --simulate-crash

The pin currents come from the same read-only IT8915 path the guard uses, so the
before/after numbers are the ones that actually matter.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_mitigation as gm  # noqa: E402

try:
    from astral_i2c import AstralPinSensor
    HAVE_PINS = True
except Exception:
    HAVE_PINS = False

# Below this a pin is drawing near-idle current. Only a meaningful yardstick when the
# card was actually loaded before the test.
NEAR_IDLE_AMPS = 2.0


class Pins:
    """Optional per-pin reader. Absent hardware is not a failure of this test."""

    def __init__(self):
        self.sensor = None
        if not HAVE_PINS:
            return
        try:
            sensor = AstralPinSensor()
            sensor.connect()
            self.sensor = sensor
        except Exception as ex:
            print(f"  (pin sensor unavailable: {ex})")

    def max_amps(self):
        if self.sensor is None:
            return None
        try:
            return max(a for _label, a, _u in self.sensor.read_pins())
        except Exception:
            return None

    def close(self):
        if self.sensor is not None:
            try:
                self.sensor.close()
            except Exception:
                pass


def print_status(m: gm.GpuMitigator) -> None:
    print(f"  GPU            : {m.gpu_name}")
    print(f"  Subsystem id   : 0x{(m.subsystem_id or 0):08X}")
    print(f"  Power limit    : {m.power_limit_mw() / 1000:.0f} W "
          f"(min {m.power_limit_min_mw / 1000:.0f} W, max {m.power_limit_max_mw / 1000:.0f} W)")
    floor = m.clock_floor_mhz
    print(f"  Clock floor    : {floor} MHz" if floor
          else f"  Clock floor    : not enumerated, will try {gm.CLOCK_FLOOR_LADDER[0]} MHz")
    print(f"  Graphics clock : {m.graphics_clock_mhz()} MHz")


def connect(require_elevation: bool) -> gm.GpuMitigator:
    elevated = gm.is_process_elevated()
    print(f"  Elevated       : {elevated}")
    if require_elevation and not elevated:
        print("\n  This test changes GPU state and needs an elevated console.")
        print("  Open PowerShell as Administrator and run it again.")
        sys.exit(2)

    # Status-only runs are read-only, so the elevation gate is bypassed deliberately
    # to let a normal console show what the guard would be working with.
    m = gm.GpuMitigator(elevation_check=(gm.is_process_elevated if require_elevation
                                         else (lambda: True)))
    if not m.connect():
        print(f"\n  Cannot reach the GPU: {m.unavailable_reason}")
        sys.exit(1)
    return m


def cmd_status() -> int:
    print("\n=== 12VHPWR Guard - mitigation status ===\n")
    m = connect(require_elevation=False)
    print_status(m)

    pins = Pins()
    amps = pins.max_amps()
    if amps is not None:
        print(f"  Max pin current: {amps:.2f} A")
    pins.close()

    marker = gm.MARKER_PATH
    print(f"\n  Marker file    : {marker}")
    print(f"  Marker present : {os.path.exists(marker)}")
    m.close()
    return 0


def cmd_engage_test(seconds: int) -> int:
    print("\n=== 12VHPWR Guard - engage/release test ===\n")
    m = connect(require_elevation=True)
    print_status(m)

    pins = Pins()

    print("\n  Baseline (3 samples before touching anything):")
    baseline_amps = []
    for _ in range(3):
        amps = pins.max_amps()
        clock = m.graphics_clock_mhz()
        if amps is not None:
            baseline_amps.append(amps)
        print(f"    clock {clock:>5} MHz   max pin "
              f"{f'{amps:.2f} A' if amps is not None else 'n/a'}")
        time.sleep(1.0)

    saved_limit = m.power_limit_mw()
    baseline_max = max(baseline_amps) if baseline_amps else None
    target_floor = m.clock_floor_mhz or gm.CLOCK_FLOOR_LADDER[0]

    print(f"\n  Captured power limit: {saved_limit / 1000:.0f} W")
    print(f"  Engaging for {seconds}s. Ctrl+C releases early.\n")

    first_below_idle = None
    first_at_floor = None
    engaged_at = time.time()

    try:
        if not m.engage("mitigation_poc engage test"):
            print("  ENGAGE FAILED - nothing was applied.")
            m.close()
            pins.close()
            return 1

        print(f"    {'t+s':>5}  {'clock':>9}  {'limit':>7}  {'max pin':>8}")
        while time.time() - engaged_at < seconds:
            t = time.time() - engaged_at
            clock = m.graphics_clock_mhz()
            limit = m.power_limit_mw()
            amps = pins.max_amps()

            if first_at_floor is None and clock is not None and clock <= target_floor + 50:
                first_at_floor = t
            if first_below_idle is None and amps is not None and amps < NEAR_IDLE_AMPS:
                first_below_idle = t

            print(f"    {t:5.1f}  {clock:>6} MHz  {limit / 1000:>5.0f} W  "
                  f"{f'{amps:.2f} A' if amps is not None else '     n/a':>8}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  Interrupted - releasing.")
    finally:
        released = m.release()

    time.sleep(1.5)
    after_limit = m.power_limit_mw()
    after_clock = m.graphics_clock_mhz()

    print("\n=== Results ===")
    print(f"  Clock reached the floor after : "
          f"{f'{first_at_floor:.1f} s' if first_at_floor is not None else 'NEVER'}")
    if baseline_max is not None:
        print(f"  Baseline max pin              : {baseline_max:.2f} A")
        if baseline_max < NEAR_IDLE_AMPS:
            print(f"  Pin current below {NEAR_IDLE_AMPS:.0f} A          : already there at idle "
                  f"(only meaningful under load)")
        else:
            print(f"  Pin current below {NEAR_IDLE_AMPS:.0f} A          : "
                  f"{f'{first_below_idle:.1f} s' if first_below_idle is not None else 'NEVER'}")
    print(f"  Power limit round trip        : {saved_limit / 1000:.0f} W -> "
          f"{after_limit / 1000:.0f} W")
    print(f"  Graphics clock after release  : {after_clock} MHz")
    print(f"  Marker file removed           : {not os.path.exists(gm.MARKER_PATH)}")

    checks = [
        ("release() reported success", released),
        ("power limit restored exactly", after_limit == saved_limit),
        ("clock lock took effect", first_at_floor is not None),
        ("marker file cleaned up", not os.path.exists(gm.MARKER_PATH)),
    ]
    print()
    failed = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1

    m.close()
    pins.close()
    print(f"\n  {'ALL CHECKS PASSED' if not failed else f'{failed} CHECK(S) FAILED'}\n")
    return 1 if failed else 0


def cmd_simulate_crash() -> int:
    """Engage and then die without releasing, the way a killed process would."""
    print("\n=== 12VHPWR Guard - crash simulation ===\n")
    m = connect(require_elevation=True)
    print_status(m)

    if not m.engage("crash simulation"):
        print("  ENGAGE FAILED.")
        return 1

    print(f"\n  Engaged. Marker at {gm.MARKER_PATH}")
    print("  Exiting hard, WITHOUT releasing - the GPU is now locked to its floor clock.")
    print("  Run this next to prove the guard cleans up after itself:")
    print("      python tools/mitigation_poc.py --recover\n")
    sys.stdout.flush()

    # No atexit, no finally, nothing: this is the case the marker file exists for.
    os._exit(0)


def cmd_recover() -> int:
    print("\n=== 12VHPWR Guard - startup recovery ===\n")
    elevated = gm.is_process_elevated()
    print(f"  Elevated       : {elevated}")
    print(f"  Marker present : {os.path.exists(gm.MARKER_PATH)}")

    m = gm.GpuMitigator()
    msg = m.startup_recovery()
    if msg is None:
        print("\n  No marker file - nothing to recover.")
        m.close()
        return 0

    print(f"\n  {msg}")
    if m.is_available:
        print(f"\n  Power limit now : {m.power_limit_mw() / 1000:.0f} W")
        print(f"  Graphics clock  : {m.graphics_clock_mhz()} MHz")
    print(f"  Marker removed  : {not os.path.exists(gm.MARKER_PATH)}")
    m.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live validation of the v1.1 GPU mitigation")
    ap.add_argument("--engage-test", action="store_true",
                    help="engage, observe, release, verify the round trip (needs admin)")
    ap.add_argument("--seconds", type=int, default=30, help="engage-test duration")
    ap.add_argument("--simulate-crash", action="store_true",
                    help="engage and exit without releasing (needs admin)")
    ap.add_argument("--recover", action="store_true",
                    help="run startup recovery, cleaning up after --simulate-crash")
    args = ap.parse_args()

    if args.engage_test:
        return cmd_engage_test(args.seconds)
    if args.simulate_crash:
        return cmd_simulate_crash()
    if args.recover:
        return cmd_recover()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
