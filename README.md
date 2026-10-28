# 12VHPWR Guard
**Standalone GPU 12VHPWR Pin Safety Monitor for Windows**

A lightweight Windows watchdog that continuously monitors **per-pin 12VHPWR current**, read **directly from the card's own sensor chip** (no HWiNFO required; HWiNFO Shared Memory is an optional fallback). When a pin goes over threshold it **slows the GPU down within about a second**, and escalates to a **forced, safe system shutdown** if that does not fix it.

Built to protect modern high-power GPUs from connector overheating and burnout caused by sustained current imbalance.

**IMPORTANT: This utility is designed specifically for ASUS ROG Astral graphics cards (RTX 5080 / RTX 5090), which carry the per-pin current sensors it depends on. It will not work with other GPU models that do not expose per-pin current telemetry.**

---

## Why this exists

Sustained current above ~**9.5 A per 12VHPWR pin** has been shown to cause:
- connector overheating
- melting
- permanent GPU damage

GPU firmware, drivers, and PSU protections **do not reliably mitigate this scenario**.

This tool adds a last-line, OS-level safety net by:
- reading **per-pin current telemetry**
- detecting **sustained**, not transient, overloads
- **cutting the card's power draw immediately**, which is far quicker than a shutdown and
  does not cost you your session
- **powering down the system** if the current stays dangerous anyway

---

## Key Features

- Monitors **GPU 12VHPWR Pin1-Pin6 Current**
- **Standalone: reads the card's own sensor chip directly, no HWiNFO required**
- Falls back to **HWiNFO SM2 shared memory** if the direct path is unavailable
- Requires **sustained threshold breach** (default: 15 s)
- **Slows the GPU down first** (clock lock plus minimum power limit) and only shuts down if
  that does not bring the current back under control
- **Severity tiers**: the higher the current, the faster the response (15 s / 3 s / immediate)
- **Fully reversible**: your exact power limit is restored and the clocks unlocked once the
  fault clears, including after a crash
- **Tells you why**: after an emergency shutdown, the next start shows what happened and
  when, so a trip while you were away is not a mystery
- **Warns about a dead or imbalanced pin** before it becomes dangerous, without touching the
  GPU
- **Never shows green when it cannot see the load**: pins carrying no current (slot power on a
  BTF board, or a dead sensor block) turn the icon purple and say so, in every response mode
- **Flight recorder**: the last minute of per-pin samples is saved to `logs/` whenever the
  guard acts, evidence for a bug report or an RMA claim
- **Force-closes blocking apps** during shutdown so nothing can stall it (on by default; `SHUTDOWN_FORCE_CLOSE_APPS` in the source)
- **Silent background operation**
- **Tray icon UI** with live status
- **Configurable settings** via tray menu (threshold amps, sustained seconds)
- **Autostart toggle** from the tray, without reinstalling
- **Persistent configuration** saved to `config.ini`
- Pause / Resume from tray
- Toast notifications
- **Windows Event Viewer logging**
- Quiet rotating file logs
- **Detects sensor loss** (HWiNFO exit/crash/freeze, or I2C failure) and auto-reconnects; never reports OK on stale data
- **Recovers from a driver update**: if the card's sensor path dies and stays dead for 5
  minutes, the guard restarts itself and comes back with a fresh driver session
- **Resets its timer across sleep/resume** instead of trusting a stale reading
- **Single-instance guard** (one guard per PC, even with several Windows accounts signed in; a second copy exits instead of double-monitoring)
- **Restarts itself within seconds if it crashes or is killed** (a small built-in supervisor;
  Task Scheduler starts it at logon)

---

## Requirements

### Hardware Compatibility
**This utility ONLY works with the ASUS-built cards listed below:** the ROG Astral family, plus
one ASUS-built OEM card. These carry an ITE IT8915FN monitoring chip that measures each 12VHPWR
pin individually. Recognized models (by PCI subsystem ID):

| Subsystem ID | Model |
|---|---|
| `1043:89E3` | ROG Astral RTX 5090 OC |
| `1043:89EA` | ROG Astral RTX 5090D OC |
| `1043:8A61` | ROG Astral RTX 5090 Matrix |
| `1043:89EC` | ROG Astral RTX 5090 LC |
| `1043:89DE` | ROG Astral RTX 5080 OC |
| `1043:8A2E` | ROG Astral RTX 5090 OC White |
| `1043:8A2B` | ROG Astral RTX 5080 OC White |
| `1043:8A45` | ROG Astral RTX 5080 OC Hatsune Miku |
| `1043:8A5A` | ROG Astral RTX 5090 BTF OC (see BTF note) |
| `1043:8A3C` | ROG Astral RTX 5090 BTF OC, second id revision of the same card (see BTF note) |
| `1043:89DF` | ROG Astral RTX 5080 (non-OC) |
| `1043:89FF` | RTX 5090 built by ASUS as an OEM card for Cooler Master (not an Astral, same IT8915FN sensor) |

**BTF note:** the BTF card is supported when it draws power through the normal 16-pin cable
in a standard motherboard, which is where its card-side sensors sit. Installed in an actual
BTF motherboard (power through the GC-HPWR slot connector), the load bypasses those sensors:
the guard would read near zero on every pin and could not protect anything. Do not rely on
this tool for a GC-HPWR powered card. Since v1.4 the guard says so itself rather than leaving
you to read this paragraph: within a minute of starting on a slot-powered card the tray icon
turns purple and the tooltip reads "NOT PROTECTED", with the reason in the log and in Event
Viewer. That check runs in every response mode and cannot be switched off, because it is not
a warning about your cable, it is the guard telling you it cannot see the load.

Have an Astral variant that is not in this table (the guard sits purple)? Run
`nvidia-smi --query-gpu=name,pci.sub_device_id --format=csv` and open an issue with the
output; adding a verified id is a one line change.

No other GPU exposes per-pin current, including non-Astral RTX 5080/5090 cards. On unsupported
hardware the guard cannot protect anything and will say so rather than pretend to work.

### OS
- Windows 10 / 11 for the guard itself. On Linux only a read-only sensor proof of concept
  exists so far, see [Reading the card on Linux](#reading-the-card-on-linux-toolsastral_poc_linuxpy).

### Software
- Python 3.10+
- NVIDIA graphics driver (the direct backend uses NVAPI, which is already part of every
  NVIDIA driver install; nothing extra is needed)
- **HWiNFO64 is optional.** By default the guard talks to the card directly. HWiNFO is only
  used as a fallback source; see [How it reads the sensors](#how-it-reads-the-sensors).

### Python Dependencies

```
winotify
pystray
pillow
pywin32
```

---

## How it reads the sensors

The guard has two interchangeable sensor backends. Both feed the same threshold engine, so
protection behaves identically either way; only the data source differs.

| Backend | What it does | Needs HWiNFO? |
|---|---|---|
| **direct** (default) | Reads the card's own IT8915FN chip over the GPU's I2C bus using NVIDIA's driver API | No |
| **hwinfo** | Reads HWiNFO's SM2 shared memory, as earlier versions did | Yes |

The active source is shown in the tray tooltip (`... | direct` or `... | HWiNFO`) and named in
the log at startup.

**Why direct is the default:** the free version of HWiNFO automatically **disables Shared Memory
after 12 hours** and requires you to re-enable it by hand. On a long session that silently
removes the guard's data source. Reading the card directly has no such limit and means one
less moving part. The direct backend is read-only; it never writes to the I2C bus.

Set the backend in `config.ini`:

```ini
[Settings]
sensor_backend = auto
```

- `auto` *(default)*: use the card directly if a supported Astral is found, otherwise HWiNFO
- `astral`: direct backend only; if the card can't be read, the guard waits (purple icon)
  rather than falling back
- `hwinfo`: HWiNFO only, matching the behavior of earlier versions. This is the rollback
  switch if the direct path ever misbehaves.

Changing this requires a restart of the guard.

### When the sensor source goes away

If the source dies (HWiNFO exits or freezes, or an I2C read fails hard enough), the guard drops
the connection rather than trusting the last values it saw, turns the tray icon purple, and
tries to connect again every few seconds. Most of the time that works: restart HWiNFO and the
next attempt picks it up.

One case never works. A driver update, or a driver crash, takes the session the direct backend
holds with it, and nothing inside the running process ever gets it back. On this project's own
rig that was 131 reconnect attempts in a row, all failed, with the card unprotected until the
scheduled task was restarted by hand. A new process gets a new driver session, so since v1.4
the guard does that itself: if it has **no source at all for 5 minutes** after having read the
direct sensor earlier in that session, it logs the reason, writes event **9002**, toasts, and
exits with its restart code. The guard's supervisor (below) starts it again two seconds later
with a clean session, and the log shows a fresh "Starting" line followed by "Restarted by the
supervisor".

Two cases deliberately never restart:

- **An HWiNFO-only session.** If the direct sensor was never the source, closing HWiNFO is a
  normal thing to do and restarting over it would be a loop. The guard waits purple, as before.
- **A session that never connected at all.** An unsupported card, or `sensor_backend = hwinfo`
  with HWiNFO not running, has no direct session to recover, so a restart would only fail the
  same way.

Time spent **paused** does not count either: a paused guard is not trying to reconnect, and the
clock starts over when you resume. The window is `sensor_loss_restart_seconds` in
`config.ini`, and `0` turns the whole behaviour off.

### If the guard crashes or is killed

Task Scheduler's "restart on failure" only covers a task that fails to start. A guard that
started and then died was never brought back by it: on this project's rig in October 2026 the
guard was killed and stayed down, with 999 restarts configured. So since v1.4 the process the
task starts is a small supervisor that runs the real guard as a child process and does nothing
else. If the guard exits for any reason other than you choosing **Exit** from the tray, the
supervisor starts it again: after 2 seconds for the planned self-restart above, otherwise after
5 seconds, backing off to a minute if it keeps dying so a startup problem cannot spin the CPU.
A crash or kill also toasts and writes event **9003**. The one exit it never restarts is the
guard's own emergency shutdown: the shutdown note is already written, and a restarted guard would
read it out and delete it before the next boot could warn you.

It works the other way too: the guard watches its supervisor and stops within a second once the
supervisor is gone, so stopping the scheduled task (which the installer and uninstaller both do)
still stops everything.

That is why Task Manager shows more `pythonw.exe` processes than before. Running the script
with `--no-supervisor` starts the guard without it, for debugging; nothing restarts that one.

---

## How it responds

Shutting a machine down takes a while, and it costs you whatever you were doing. Telling the
GPU to stop pulling current takes about a second and costs you nothing but frame rate. So the
first response to an over-current pin is a **reversible GPU slowdown**: the graphics clock is
locked to the card's floor and the power limit is dropped to the card's minimum, both through
NVIDIA's own management API. Shutdown remains the escalation for when that is not enough.

### Severity tiers

The tiers exist because some currents are too high to sit through a 15 second window.

| Tier | Trigger (any single pin) | Response |
|---|---|---|
| 1 - elevated | >= 9.5 A sustained 15 s | Limit the GPU. Shut down if the pin is still at or above 9.5 A after 10 s |
| 2 - critical | >= 13 A sustained 3 s | Limit the GPU right away. Shut down if the pin is still at or above 9.5 A after 5 s |
| 3 - catastrophic | >= 16 A on 2 consecutive samples (about 1 s) | Limit the GPU and shut down immediately |

For scale: spec load at 600 W is about 8.3 A per pin, ASUS's own warning fires at 9.2 A,
documented slow-cook failures run 11-15 A, and the melted connector everyone has seen measured
over 22 A on one wire. Tier 3 needs two consecutive samples so a single bad read can never
reach it, and readings above 30 A are discarded as garbage before any of this runs.

### Getting the GPU back

Once limited, the card stays limited for at least **120 s**, and is only released after all
pins have been below the threshold for **60 s**. Currents drop the instant the clocks do, so
releasing on current alone would just flap. On release your power limit is restored to
**exactly the value it had**, not to the card's default, so a custom limit survives.

One caveat for undervolters: the emergency slowdown replaces any clock lock you set yourself
(`nvidia-smi -lgc` style), and the release returns the card to its automatic clock management
rather than to your lock. NVIDIA's API offers no way to read an existing lock back, so the
guard cannot restore what it cannot see. Curve based undervolts (MSI Afterburner and similar)
are not affected. Every mitigation announces itself with a toast and a log entry, so you will
know to reapply your lock afterwards.

If a tier triggers again within **600 s** of a release, that is treated as a persistent fault
rather than a transient: the guard limits the card and shuts down. Pausing from the tray, or a
suspend/resume, releases the limit instead of holding it, since in both cases the decision has
gone back to you.

If the guard is killed while the GPU is limited, it leaves a `mitigation.active` marker file
behind and restores everything the next time it starts. A reboot clears both changes on its
own, since neither survives a driver reload.

**Why it drops so deep:** the power limit alone cannot protect you. All six pins share one
12 V rail, and how the current splits between them is set by contact resistance, which is
exactly the thing that has gone wrong. At the card's 400 W minimum limit, a badly seated pin
can still carry 11-13 A while the total draw looks fine. Only near idle draw is safe under
any imbalance, and that is what the clock floor delivers. Validated mid game (Alan Wake 2,
roughly 500 W, about 7 A on the busiest pin): on engage the clock hit the floor in 1 second,
the worst pin fell under 2 A inside 4 seconds and settled at 0.86 A, and on release the game
went straight back to full clocks with the power limit restored to exactly its prior value.

**The drop targets are deliberately not configurable.** The power limit target is whatever
minimum the card reports and the clock target is its lowest supported frequency, both read
from the card at startup. There is no useful way to tune an emergency response except weaker,
and the session survives floor clocks for two minutes either way. What you *can* configure is
when it happens, never how hard.

### After it fires

An emergency shutdown leaves a note. At the next start the guard announces exactly what
happened and when, as a notification, a log warning and an Event Viewer entry, then removes
the note. Without this, a shutdown that fires while you are away looks like a Windows crash,
and the bad cable gets plugged straight back into a heavy load. If you see that message,
check the connector seating before the next long session.

Whenever the guard limits the GPU or shuts down, it also writes the last minute of per-pin
samples to a `flight_*.csv` in the `logs` folder (the ten most recent are kept, and nothing
is written to disk in normal operation). That file shows what every pin was doing in the
sixty seconds before the guard acted, which is exactly what a bug report or an RMA claim
needs.

### Response mode

Right-click the tray icon, **Settings**, and pick one:

- **Response: Tiered (limit GPU first, then shutdown)** *(default)*
- **Response: Shutdown only**

The change takes effect on the next poll, no restart. The severity tiers apply in **both**
modes, so shutdown-only is still meaningfully faster than v1.0 at high currents: 13 A gets you
a shutdown in 3 s rather than 15. Shutdown-only never touches the NVIDIA management API at
all, which makes it the clean rollback if anything about the GPU limiting misbehaves on your
system.

### Elevation

Changing clocks and power limits requires an elevated process. The installer's scheduled task
already runs at the highest privileges, so a normally installed guard can do it. A guard
started by hand from a normal console cannot: it logs a warning, the tray tooltip reads
`shutdown-only`, the menu shows why, and protection falls back to shutting down. It never
silently pretends to have a capability it does not have.

---

## What it warns about

Everything above acts on **absolute** per-pin current, because that is what melts a connector.
That leaves one blind spot, and a community member put real numbers on it in PR #2: at a 540 W
power limit, a connector with **one open pin** puts 44.8 A through the five that are left, so
each carries 8.96 A. That is under the 9.5 A tier 1 trip. Nothing is over threshold, the guard
stays green, and the connector is running on five wires with a hot spot where the sixth should
be. Invisible to a threshold, obvious in the spread.

So the guard also watches the **shape** of the six readings, and says something when it looks
wrong:

- **Dead pin** - one pin reads at or below **0.5 A** while the other five average **3.0 A** or
  more. That pin, or its contact, is open. If more than one pin is near zero, the message names
  the quietest and says how many.
- **Imbalance** - the gap between the busiest and the quietest pin is **3.0 A** or more, with
  the busiest at **5.0 A** or more, and no dead pin. Uneven contact resistance puts the heat on
  the busiest pin. For scale, a healthy Astral in this project's own logs runs a 0.7 A spread at
  7.5 A per pin, so 3.0 A has real margin.

Both only apply while the connector is actually carrying something: the six pins have to total
at least **18 A** (about 215 W) before either rule can start, so idle noise cannot trip them. A
condition has to hold for **10 s** before the first warning, then repeats at most every
**10 minutes** while it lasts, and logs one "cleared" line once it has been gone for a minute.

There is a third thing it watches, and this one is about the guard rather than the cable:

- **No current on any pin** - all six pins read **0.1 A or less** for a full **minute** while
  the guard is connected. Nothing is flowing through the connector it is watching, so nothing
  above this line means anything: the readings would sit at a comfortable zero no matter what
  the card was doing. That is what a BTF card powered through the GC-HPWR slot looks like (see
  the BTF note under [Hardware Compatibility](#hardware-compatibility)), and it is also what a
  dead or unpowered sensor block looks like. Either way the answer is the same, so the guard
  stops showing green: the icon goes **purple**, which has always meant "cannot protect", and
  the tooltip reads "NOT PROTECTED". It goes back to green a minute after the pins carry
  current again. A cabled Astral cannot look like this, because it idles at roughly 0.5 to
  1.0 A per pin: it takes nearly everything through the 16-pin and almost nothing from the
  slot. In tiered mode the guard also compares the pins against the board power the card
  reports (above **150 W**, pins adding up to less than a **quarter** of what that power
  implies, for **30 s**), which catches the partial case where slot and cable are both
  connected. That message names both numbers.

  Unlike the two above, **`advisories = off` does not silence this one**, and neither does
  shutdown-only mode. It is not an opinion about your hardware that you might not want; it is
  the guard admitting it cannot see the load, and a tool that hides that is worse than no tool.

**An advisory never does anything to your machine.** It is a toast, a log line, an Event Viewer
entry and a tooltip suffix, and that is all. It never limits the GPU, never shuts down, and
never touches the tiers above. The guard has not acted, because there is nothing here for it to
act on yet. The one thing any of this changes is the icon colour, and only for the no-current
case, where green would be a lie.

At the **first** warning of an episode the guard writes `logs/flight_*_advisory.csv`, the last
minute of per-pin readings. That file is the evidence, and the fault may not repeat on demand:
reseat the cable at both ends, look at the pins in the connector for discolouration or a
retracted terminal, and keep the file if you end up filing an RMA.

Advisories are configured in `config.ini` and are on by default:

```ini
[Settings]
advisories = on
advisory_load_amps = 18.0
dead_pin_max_amps = 0.5
imbalance_spread_amps = 3.0
advisory_sustain_seconds = 10.0
advisory_repeat_seconds = 600.0
```

Set `advisories = off` to silence the dead-pin and imbalance warnings completely. The
sensor-path check is not one of them and keeps running either way. Like the tier settings,
these are read at startup only, and the tray menu never touches them.

---

## HWiNFO Configuration

Only needed if you use the `hwinfo` backend (or want HWiNFO running alongside for its own sake).

1. Launch **HWiNFO64**
2. Open **Sensors**
3. Enable **Shared Memory Support**
4. Confirm these sensors exist:

```
GPU 12VHPWR Pin1 Current
GPU 12VHPWR Pin2 Current
GPU 12VHPWR Pin3 Current
GPU 12VHPWR Pin4 Current
GPU 12VHPWR Pin5 Current
GPU 12VHPWR Pin6 Current
```

---

## Installation

### Option A: Automated Installer (Recommended)

The easiest way to install 12VHPWR Guard is using the included installer.

**Prerequisites:**
- Windows 10 or 11
- [Python 3.10+](https://www.python.org/downloads/) installed and on your PATH
- Administrator rights (required for Task Scheduler and Event Viewer registration)

**Steps:**

1. **Extract or clone** the project to a folder (e.g. `C:\12vhpwr_guard`).

2. **Run the installer:**
   - Double-click **`install.bat`**, or
   - From PowerShell/Command Prompt in the project folder, run:
     ```
     .\install.bat
     ```
   - If you use PowerShell directly:  
     `.\install.ps1`  
     (You may need to allow script execution:  
     `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`)

3. **Follow the prompts.** The installer will:
   - Create a Python virtual environment (`venv`) in the project folder
   - Install dependencies from `requirements.txt`
   - Run pywin32 post-install (for Event Viewer support)
   - Create a default `config.ini` if none exists
   - Create a `logs` folder
   - Register the **"12VHPWR Guard"** Event Viewer source (if permitted)
   - Lock down the folder so only administrators can change the code (see
     [Multiple Windows accounts](#multiple-windows-accounts))
   - Create a **scheduled task** named **"12VHPWR Guard"** that:
     - Runs at logon of your account, or of every account if you answer `y` to the
       all-accounts question
     - Starts the guard with `pythonw.exe` (no console window)
     - Restarts the guard within seconds if it crashes or is killed (the built-in
       supervisor), and retries a launch that fails up to 999 times, 1 minute apart
     - Runs indefinitely: the default 72-hour Task Scheduler execution limit is disabled,
       and battery-power settings will not stop it

4. When asked **"Start 12VHPWR Guard for every Windows account on this PC?"**, answer `N`
   (the default) if only you use this PC, or `y` if other people log on to it as well. See
   [Multiple Windows accounts](#multiple-windows-accounts).

5. When asked **"Start 12VHPWR Guard now? (y/N)"**, type `y` and Enter to start immediately, or `N` to have it start at next logon.

**After installation:** The guard runs from the system tray. You'll see the 12VHPWR Guard icon when it's active. On a supported Astral card nothing else is needed; hover the icon and the tooltip should read `... | direct`. Only if you use the `hwinfo` backend does **HWiNFO64** need to be running with **Shared Memory Support** and the per-pin sensors enabled.

### Multiple Windows accounts

By default the guard starts only when the account that ran the installer logs on. If other
people use the PC with their own Windows accounts, answer `y` to **"Start 12VHPWR Guard for
every Windows account on this PC?"**. The task is then registered for the built-in Users
group, so it starts for whoever logs on, in their own session, with the tray icon on their
desktop.

Before it does that, the installer checks two things that would otherwise stop it working
for the other accounts:

- **The folder is not inside a user profile.** Other accounts cannot read `C:\Users\<you>\...`
  (Desktop, Downloads and so on). Put it somewhere like `C:\12vhpwr_guard`.
- **Python is installed for all users.** The python.org "Install Now" button installs Python
  inside your own profile, where other accounts cannot run it. Reinstall with *Customize
  installation* and tick *Install Python for all users*, delete the `venv` folder, and run the
  installer again.

What the other accounts get depends on their account type:

- **Administrator accounts** get the full guard, including the GPU clock and power limit.
- **Standard accounts** get monitoring, the tray icon, warnings and the emergency shutdown,
  but not the GPU clock and power limit, because Windows only lets administrators change
  those.

Only one guard runs per PC. If a second person logs on while the first is still signed in
(switch user), the first person's guard keeps protecting the card in the background and the
second person sees no tray icon. If the first person then signs out, the guard starts again
at the next logon.

The installer also removes write access to the program folder for everyone except
administrators, in both modes. The guard runs with administrator rights, so anyone who can
edit its files could get their own code run as administrator. Standard accounts can still
write the logs and `config.ini`, which is all the guard needs from them.

---

### Upgrading

1. Get the new code: `git pull` if you cloned the repo, or download the new release and
   extract it over your existing folder.
2. Re-run the installer (`install.bat` as administrator).

That is the whole process. The installer stops the running guard, reuses the existing venv,
re-registers the scheduled task, and leaves `config.ini` and your logs untouched, so your
thresholds and settings survive. If you started the guard by hand instead of through the
scheduled task, exit it from the tray first.

The version you are running is shown at the top of the tray menu and in the first log line
at startup. The guard makes no network connections at all, so it will never check for
updates itself; watch the GitHub releases page for new versions.

---

### Uninstalling (Automated)

To stop and remove the scheduled task (the guard will no longer start at logon):

1. **Run the uninstaller:**
   - Double-click **`uninstall.bat`**, or
   - From the project folder:  
     `.\uninstall.bat`  
     or in PowerShell:  
     `.\uninstall.ps1`

2. The **"12VHPWR Guard"** scheduled task is removed. The script, `venv`, `config.ini`, and logs are left in place so you can run manually or reinstall later.

To fully remove the software, delete the entire project folder after running the uninstaller.

---

### Option B: Manual Installation

If you prefer not to use the installer or need a custom setup:

1. **Place the project** in a folder (e.g. `C:\12vhpwr_guard`).

2. **Create and activate a virtual environment:**
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

   Or install dependencies individually:
   ```
   pip install winotify pystray pillow pywin32
   ```

3. **Event Viewer logging (optional):**  
   If you want Application log entries:
   ```
   venv\Scripts\python -m pywin32_postinstall -install
   ```
   (May require administrator rights.)

4. **Run the guard** (no console window):
   ```
   venv\Scripts\pythonw.exe hwinfo_12vhpwr_guard.py
   ```

5. **Run at logon (optional):** Create a scheduled task as in the installer, or use Task Scheduler GUI:
   - **Program:** `venv\Scripts\pythonw.exe`
   - **Arguments:** `hwinfo_12vhpwr_guard.py`
   - **Start in:** project folder (e.g. `C:\12vhpwr_guard`)
   - **Trigger:** At log on  
   - **Settings:** Restart on failure suggested (it only covers a launch that fails; the
     guard's own supervisor handles crashes), and set **"Stop the task if it runs longer
     than"** to **disabled**; the Task Scheduler default silently kills the guard after
     72 hours of uptime

---

### Installer Files Reference

| File | Description |
|------|-------------|
| `install.bat` | Launches the PowerShell installer; double-click or run from Command Prompt. |
| `install.ps1` | PowerShell script that creates venv, installs deps, config, and the scheduled task. |
| `uninstall.bat` | Launches the PowerShell uninstaller. |
| `uninstall.ps1` | Stops the guard and removes the "12VHPWR Guard" scheduled task. |

---

## Tray Icon Status

| Color | Meaning |
|------|--------|
| Green | OK |
| Amber | Over threshold |
| Orange | GPU limited, waiting for the current to clear |
| Red | Shutdown triggered |
| Purple | Cannot protect: no sensor source, or the pins carry no current |
| Gray | Source connected, but per-pin sensors not found |
| Blue | Paused |

**Purple vs. Gray:** purple means the guard cannot protect the card, for one of two reasons.
Either it has **no data source** (no supported Astral card could be read directly and HWiNFO
shared memory is not available: not running, exited, or frozen), or it has readings that carry
**no current at all**, which is the sensor-path check under
[What it warns about](#what-it-warns-about). The tooltip says which: "Waiting for pin sensors"
against "NOT PROTECTED". Gray means a source is connected but the six per-pin sensors are
missing; with the `hwinfo` backend that usually means renamed or disabled sensors, so check the
names under [HWiNFO Configuration](#hwinfo-configuration). In all of these cases **no
protection is active**.

Hovering the icon shows the current status, the highest pin current, and the active data
source (`... | direct` or `... | HWiNFO`). While the GPU is limited the tooltip reads
`LIMITED`, and if a tier crossing would go straight to shutdown (either because you chose
that, or because the guard is not elevated) the tooltip says `shutdown-only`.

## Tray Menu

Right-click the tray icon to access:

- **Open Log** - Opens the log file
- **Settings** submenu:
  - **Change Threshold Amps...** - Adjust the current threshold (default: 9.5 A)
  - **Change Sustained Seconds Required...** - Adjust the sustained time requirement (default: 15.0 s)
  - **Response: Tiered / Response: Shutdown only** - Pick what an over-current does, see
    [How it responds](#how-it-responds). Takes effect immediately, no restart
  - **Start with Windows** - Checkbox mirroring whether the logon task is enabled; click to turn autostart on or off
  - **Reset to Defaults** - Reset the threshold and sustained time to default values
- **Pause / Resume** - Temporarily pause monitoring (releases the GPU if it is limited).
  While paused, a reminder fires every 30 minutes; a paused guard protects nothing
- **Exit** - Stop the guard

### Start with Windows

The checkbox reflects the real state of the **"12VHPWR Guard"** scheduled task, so it stays
accurate even if the task is changed from Task Scheduler directly.

It enables and disables the existing task rather than creating one, so `install.bat` still has
to be run once first; until then the item is unchecked and clicking it explains why. Keeping
task creation in the installer means the trigger, principal and restart settings are defined in
exactly one place.

Changing a scheduled task needs administrator rights. Started from the task itself the guard is
already elevated and the toggle applies immediately; started by hand it raises a single UAC
prompt, and declining leaves the setting untouched.

---

## Event Viewer

Logs written to:
```
Event Viewer > Windows Logs > Application
Source: 12VHPWR Guard
```

Every setting you change from the tray is written to the log and to Event Viewer (event ID
1010) with its old and new value, so a trip can be read against the settings that were live.

The warn-only pin advisories are event IDs **7001** (imbalance) and **7002** (dead pin), and
the sensor-path check is **7003** (no current on any pin), all three warnings. **7004**
(cleared) is the information entry that ends any of them. See
[What it warns about](#what-it-warns-about).

Event ID **9002** (error) is the self-restart after a long sensor loss, described under
[When the sensor source goes away](#when-the-sensor-source-goes-away). It is followed by the
usual **1003** stopped entry, and then a **1000** start from the run the supervisor brings up.

Event ID **9003** is the supervisor starting the guard again: information after the planned
self-restart, a warning after a crash or a kill. See
[If the guard crashes or is killed](#if-the-guard-crashes-or-is-killed).

---

## Configuration

### Config File

Settings are saved to `config.ini` in the script directory. The file is created automatically when you first change a setting via the tray menu.

**File format:**
```ini
[Settings]
threshold_amps = 9.5
sustained_seconds_required = 15.0
sensor_backend = auto
response_mode = tiered
```

`sensor_backend` is described under [How it reads the sensors](#how-it-reads-the-sensors) and is
only read at startup. The tray menu never touches it, and editing a threshold from the tray
preserves whatever value you set here.

`response_mode` is `tiered` or `shutdown_only`, described under
[How it responds](#how-it-responds). It is the one setting the tray menu changes at runtime.

**Self-restart.** Optional, read at startup only, described under
[When the sensor source goes away](#when-the-sensor-source-goes-away):

```ini
sensor_loss_restart_seconds = 300.0
```

| Key | Meaning |
|---|---|
| `sensor_loss_restart_seconds` | How long the guard goes without any sensor source, after having read the direct sensor, before it exits so the supervisor restarts it. `0` turns the self-restart off and it waits instead |

**Tier settings.** These are optional and only read at startup. Leave them out and the
defaults below apply:

```ini
critical_amps = 13.0
critical_seconds = 3.0
catastrophic_amps = 16.0
mitigation_grace_seconds = 10.0
mitigation_clear_seconds = 60.0
mitigation_min_hold_seconds = 120.0
retrigger_window_seconds = 600.0
```

| Key | Meaning |
|---|---|
| `critical_amps` / `critical_seconds` | Tier 2: current high enough to skip tier 1's 15 s window |
| `catastrophic_amps` | Tier 3: limit and shut down at once, on 2 consecutive samples |
| `mitigation_grace_seconds` | How long a limited GPU has to bring the current down before the guard shuts down instead (tier 2 uses a fixed 5 s) |
| `mitigation_clear_seconds` | How long all pins must stay clear before the limit lifts |
| `mitigation_min_hold_seconds` | Minimum time the limit stays on regardless of how fast the current falls |
| `retrigger_window_seconds` | A tier firing again this soon after a release is a persistent fault, and shuts down |

**Advisory settings.** Also optional, also read at startup only, described under
[What it warns about](#what-it-warns-about). Leave them out and the defaults below apply:

```ini
advisories = on
advisory_load_amps = 18.0
dead_pin_max_amps = 0.5
imbalance_spread_amps = 3.0
advisory_sustain_seconds = 10.0
advisory_repeat_seconds = 600.0
```

| Key | Meaning |
|---|---|
| `advisories` | `on` or `off`. Off silences the warn-only advisories completely |
| `advisory_load_amps` | The six pins must total at least this before any advisory can start |
| `dead_pin_max_amps` | A pin at or below this, while the others average 3.0 A or more, is an open pin |
| `imbalance_spread_amps` | Busiest minus quietest pin at or above this (with the busiest at 5.0 A or more) is an imbalance |
| `advisory_sustain_seconds` | How long a condition must hold before the first warning |
| `advisory_repeat_seconds` | How often a warning repeats while the condition lasts |

### Default Safety Settings

```
THRESHOLD_AMPS = 9.5 A
SUSTAINED_SECONDS_REQUIRED = 15.0 s
CRITICAL_AMPS = 13.0 A / 3.0 s
CATASTROPHIC_AMPS = 16.0 A (2 consecutive samples)
POLL_INTERVAL_SEC = 0.5 s
SHUTDOWN_FORCE_CLOSE_APPS = True
RESPONSE_MODE = tiered
```

### Changing Settings

**WARNING: Modifying safety thresholds can be dangerous!**

**Increasing threshold values above defaults (9.5 A, 15.0 s, or any of the tier settings) reduces protection and may allow unsafe conditions to persist longer before the guard acts. This can lead to:**
- Connector overheating
- Melting or physical damage to the 12VHPWR connector
- Permanent GPU damage
- Fire risk

**Only modify settings if you fully understand the risks and have a specific, justified reason. Lower values increase safety but may cause false positives during legitimate high-load scenarios.**

**Via Tray Menu (Recommended):**
1. Right-click the tray icon
2. Select **Settings**
3. Choose the setting to modify
4. Enter the new value in the dialog
5. Entering a value **above** the default prompts for confirmation, because that reduces protection
6. Settings are saved automatically and take effect immediately

**Via Config File:**
- Edit `config.ini` directly (restart required for changes to take effect)
- If the file is missing or invalid, defaults are used
- Values must be positive numbers

---

## Development

### Reading the card directly (`tools/astral_poc.py`)

Prints live per-pin voltage and current straight from the IT8915 chip, and can A/B itself
against HWiNFO to confirm the two agree:

```
python tools/astral_poc.py --once --raw    # one sample plus the raw 24 bytes
python tools/astral_poc.py                 # 1 Hz table
python tools/astral_poc.py --crosscheck    # 60s agreement check vs HWiNFO
```

The protocol (chip address, register layout, pin ordering, NVAPI struct) is documented in the
header of `astral_i2c.py`, with the two independent sources it was verified against. All access
is **read-only**; the module deliberately does not bind NVAPI's I2C write function.

### Reading the card on Linux (`tools/astral_poc_linux.py`)

The guard itself is Windows only, but the sensor path is not. This read-only proof of
concept reads the same IT8915 block over the kernel's `/dev/i2c-N` interface, no NVAPI and
no HWiNFO, and prints the six pins once a second. It finds the card through sysfs, checks
the subsystem id against the same allowlist, and probes only the GPU's own I2C adapters.

```
sudo modprobe i2c-dev
python3 tools/astral_poc_linux.py --list                      # candidate adapters, no root needed
sudo python3 tools/astral_poc_linux.py --once --raw --volts   # one sample plus the raw bytes
sudo python3 tools/astral_poc_linux.py                        # 1 Hz table, Ctrl-C to stop
```

Verified 2026-09-07 on Linux Mint (kernel 7.0, driver 610.43) with a ROG Astral RTX 5090 OC:
the chip answers on "NVIDIA i2c adapter 1" at address 0x2B and reads about 11.95 V and 0.5 to
0.6 A per pin at idle, the same numbers Windows shows. One quirk worth knowing if you write
your own reader: on the NVIDIA adapters a plain write-then-read transaction returns all zeros
silently, while the SMBus "I2C block data" read works, which is what the tool uses. That
finding comes from the astral-hwmon project, which reads the same chip from a kernel driver.
Do not scan the GPU bus with `i2cdetect -y`; its default probe writes to every address.

The tool guards nothing on its own. A Linux daemon built on this path is planned; until then
astral-hwmon offers a hwmon driver with a report-only guard and a poweroff service example.
All access here is **read-only**: the register pointer is the only byte the tool ever puts on
the bus, the device is opened read-only, and a test bans every write path.

### Testing the GPU slowdown (`tools/mitigation_poc.py`)

Shows what the guard can do to the card, and proves the round trip puts everything back:

```
python tools/mitigation_poc.py                  # read-only status, no admin needed
python tools/mitigation_poc.py --engage-test    # engage, watch, release, verify (admin)
python tools/mitigation_poc.py --simulate-crash # engage and die without releasing (admin)
python tools/mitigation_poc.py --recover        # clean up after --simulate-crash
```

`--engage-test` locks the GPU to its floor clock for 30 seconds while printing the clock,
power limit and live pin currents once a second, then releases and checks that the power limit
came back to exactly its previous value. Run it at the desktop, not mid-game.

`--simulate-crash` deliberately leaves the card limited so you can see the marker file do its
job: `--recover` (or just starting the guard) puts everything back.

### Emulating HWiNFO (`tools/fake_hwinfo_sm2.py`)

Emulates HWiNFO's shared-memory block so the HWiNFO backend can be exercised
without HWiNFO or a supported GPU. It serves the six per-pin sensors with settable values and can
simulate HWiNFO exiting (`dead`), freezing (`freeze`), and renaming a sensor (`rename`).

```
# Console 1 - start the emulator
python tools/fake_hwinfo_sm2.py

# Console 2 - point the guard at the emulator instead of the real HWiNFO
set HWINFO_SM2_NAME=Local\HWiNFO_SENS_SM2_TEST
set HWINFO_SM2_MUTEX=Local\HWiNFO_SM2_MUTEX_TEST
python hwinfo_12vhpwr_guard.py
```

Set `sensor_backend = hwinfo` in `config.ini` while doing this, or `auto` will pick the real
card and ignore the emulator entirely.

The `HWINFO_SM2_NAME` / `HWINFO_SM2_MUTEX` variables are a development convenience only; when
unset (the normal case) the guard reads the real `Global\HWiNFO_SENS_SM2` block.

**Warning:** driving emulator values above the threshold for longer than the sustained window
will shut the machine down for real. Raise `sustained_seconds_required` in `config.ini` while
testing.

### Running the tests

```
python -m pip install pytest
python -m pytest -q tests
```

Windows only, since the guard imports `ctypes.wintypes`. The suite fakes the sensor and the
GPU and stubs the shutdown, so it cannot power the machine off or touch the card, and it
writes nothing outside a temporary directory. The rig end-to-end script is separate and is not
in this repo: it drives the real NVML mitigation on a real Astral card and needs the hardware.

---

## Disclaimer & Legal Notice

### IMPORTANT WARNINGS

**This software is provided "AS IS" without warranty of any kind, express or implied.**

1. **Hardware Compatibility**: This utility is designed exclusively for ASUS ROG Astral graphics cards (RTX 5080 / RTX 5090) with per-pin current sensors. It will not function correctly with other GPU models.

2. **Safety Thresholds**: The default safety thresholds (9.5 A per pin, 15.0 s sustained) are based on documented failure modes. **Modifying these values, especially increasing them, can be dangerous and may:**
   - Allow unsafe current conditions to persist
   - Lead to connector overheating, melting, or physical damage
   - Cause permanent GPU damage
   - Create fire hazards
   - Void warranties

3. **No Guarantee of Protection**: This tool is a **supplementary safety measure only**. It does not guarantee protection against all failure modes, and should not be relied upon as the sole safety mechanism. GPU firmware, drivers, and PSU protections should remain active.

4. **System Shutdowns**: This utility will force a system shutdown when thresholds are exceeded and limiting the GPU does not resolve the condition. Ensure you save your work regularly, as unsaved data will be lost during emergency shutdowns.

5. **GPU Limiting**: In its default tiered mode this utility changes your GPU's clock and power
   limit through NVIDIA's management API when it detects an over-current condition. The change
   is temporary and reversed automatically, and neither setting survives a reboot, but while it
   is active the card will run at drastically reduced performance. Choose **Response: Shutdown
   only** in the tray menu if you would rather it never touched your GPU settings.

6. **Testing & Validation**: This software has not been extensively tested across all system configurations. Use at your own risk.

### Liability

**THE AUTHORS, CONTRIBUTORS, AND DISTRIBUTORS OF THIS SOFTWARE:**
- **SHALL NOT BE LIABLE** for any direct, indirect, incidental, special, exemplary, or consequential damages (including, but not limited to, procurement of substitute goods or services; loss of use, data, or profits; or business interruption) however caused and on any theory of liability, whether in contract, strict liability, or tort (including negligence or otherwise) arising in any way out of the use of this software, even if advised of the possibility of such damage.

- **SHALL NOT BE RESPONSIBLE** for any hardware damage, data loss, or other consequences resulting from the use or misuse of this software.

- **DO NOT WARRANT** that this software will meet your requirements or that its operation will be uninterrupted or error-free.

**By using this software, you acknowledge that you have read this disclaimer, understand it, and agree to be bound by its terms. You assume full responsibility for any consequences resulting from the use of this software.**

### AI-Assisted Development

This code was developed with the assistance of AI tools (including Claude Code, Cursor AI, and similar assistants). While efforts have been made to ensure correctness, the code is provided without warranty. Users are encouraged to review the code and understand its operation before use.

### Use at Your Own Risk

**This tool is a protective measure only. Use at your own risk.**
