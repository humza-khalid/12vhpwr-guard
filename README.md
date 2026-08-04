# 12VHPWR Guard
**Standalone GPU 12VHPWR Pin Safety Monitor for Windows**

A lightweight Windows watchdog that continuously monitors **per-pin 12VHPWR current**, read **directly from the card's own sensor chip** (no HWiNFO required; HWiNFO Shared Memory is an optional fallback), and performs a **forced, safe system shutdown** if a sustained over-current condition is detected.

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
- powering down the system **before damage occurs**

---

## Key Features

- Monitors **GPU 12VHPWR Pin1-Pin6 Current**
- **Standalone: reads the card's own sensor chip directly, no HWiNFO required**
- Falls back to **HWiNFO SM2 shared memory** if the direct path is unavailable
- Requires **sustained threshold breach** (default: 15 s)
- **Force-closes blocking apps** during shutdown so nothing can stall it (on by default; `SHUTDOWN_FORCE_CLOSE_APPS` in the source)
- **Silent background operation**
- **Tray icon UI** with live status
- **Configurable settings** via tray menu (threshold amps, sustained seconds)
- **Persistent configuration** saved to `config.ini`
- Pause / Resume from tray
- Toast notifications
- **Windows Event Viewer logging**
- Quiet rotating file logs
- **Detects sensor loss** (HWiNFO exit/crash/freeze, or I2C failure) and auto-reconnects; never reports OK on stale data
- **Resets its timer across sleep/resume** instead of trusting a stale reading
- **Single-instance guard** (a second copy exits instead of double-monitoring)
- Uses **Task Scheduler as watchdog**

---

## Requirements

### Hardware Compatibility
**This utility ONLY works with ASUS ROG Astral cards.** These carry an ITE IT8915FN monitoring
chip that measures each 12VHPWR pin individually. Recognized models (by PCI subsystem ID):

| Subsystem ID | Model |
|---|---|
| `1043:89E3` | ROG Astral RTX 5090 OC |
| `1043:89EA` | ROG Astral RTX 5090D OC |
| `1043:8A61` | ROG Astral RTX 5090 Matrix |
| `1043:89EC` | ROG Astral RTX 5090 LC |
| `1043:89DE` | ROG Astral RTX 5080 OC |

No other GPU exposes per-pin current, including non-Astral RTX 5080/5090 cards. On unsupported
hardware the guard cannot protect anything and will say so rather than pretend to work.

### OS
- Windows 10 / 11

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
   - Create a **scheduled task** named **"12VHPWR Guard"** that:
     - Runs at user logon
     - Starts the guard with `pythonw.exe` (no console window)
     - Restarts automatically on failure (up to 999 times, 1-minute interval)
     - Runs indefinitely: the default 72-hour Task Scheduler execution limit is disabled,
       and battery-power settings will not stop it

4. When asked **"Start 12VHPWR Guard now? (y/N)"**, type `y` and Enter to start immediately, or `N` to have it start at next logon.

**After installation:** The guard runs from the system tray. You'll see the 12VHPWR Guard icon when it's active. On a supported Astral card nothing else is needed; hover the icon and the tooltip should read `... | direct`. Only if you use the `hwinfo` backend does **HWiNFO64** need to be running with **Shared Memory Support** and the per-pin sensors enabled.

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
   - **Settings:** Restart on failure suggested, and set **"Stop the task if it runs longer
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
| Red | Shutdown triggered |
| Purple | No sensor source / waiting to reconnect (not monitoring) |
| Gray | Source connected, but per-pin sensors not found |
| Blue | Paused |

**Purple vs. Gray:** purple means the guard currently has **no data source**: no supported
Astral card could be read directly, and HWiNFO shared memory is not available (not running,
exited, or frozen). Gray means a source is connected but the six per-pin sensors are missing;
with the `hwinfo` backend that usually means renamed or disabled sensors, so check the names
under [HWiNFO Configuration](#hwinfo-configuration). In both cases **no protection is active**.

Hovering the icon shows the current status, the highest pin current, and the active data
source (`... | direct` or `... | HWiNFO`).

## Tray Menu

Right-click the tray icon to access:

- **Open Log** - Opens the log file
- **Settings** submenu:
  - **Change Threshold Amps...** - Adjust the current threshold (default: 9.5 A)
  - **Change Sustained Seconds Required...** - Adjust the sustained time requirement (default: 15.0 s)
  - **Reset to Defaults** - Reset both settings to default values
- **Pause / Resume** - Temporarily pause monitoring
- **Exit** - Stop the guard

---

## Event Viewer

Logs written to:
```
Event Viewer > Windows Logs > Application
Source: 12VHPWR Guard
```

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
```

`sensor_backend` is described under [How it reads the sensors](#how-it-reads-the-sensors) and is
only read at startup. The tray menu never touches it, and editing a threshold from the tray
preserves whatever value you set here.

### Default Safety Settings

```
THRESHOLD_AMPS = 9.5 A
SUSTAINED_SECONDS_REQUIRED = 15.0 s
POLL_INTERVAL_SEC = 0.5 s
SHUTDOWN_FORCE_CLOSE_APPS = True
```

### Changing Settings

**WARNING: Modifying safety thresholds can be dangerous!**

**Increasing threshold values above defaults (9.5 A, 15.0 s) reduces protection and may allow unsafe conditions to persist longer before shutdown. This can lead to:**
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

4. **System Shutdowns**: This utility will force a system shutdown when thresholds are exceeded. Ensure you save your work regularly, as unsaved data will be lost during emergency shutdowns.

5. **Testing & Validation**: This software has not been extensively tested across all system configurations. Use at your own risk.

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
