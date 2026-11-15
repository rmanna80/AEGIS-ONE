# Raspberry Pi companion computer

The Pi runs the AEGIS backend and dashboard. The **simulated** autopilot (PX4 SITL) stays on
your PC and sends its MAVLink to the Pi over your network. This is how a companion computer
is developed before it ever meets an aircraft: real companion hardware, simulated vehicle.

```
   PC (WSL2)                               Raspberry Pi 5
   PX4 SITL  ── MAVLink over UDP ───────▶  AEGIS backend + dashboard
   (simulated vehicle)                     (detection, mapping, search, monitors)
                                                  ▲
   your browser ── SSH tunnel ────────────────────┘
```

**Status: flown with a Raspberry Pi 5 in the loop.** On the owner's Pi 5 the setup script and
benchmark ran, the backend attested the simulator across the home network (PX4 SITL on a PC
under WSL2, default networking), and Demo 1 passed 5 of 5 (details in
`docs/testing/README.md`). Other demos, on-board sensors during a flight, sustained
temperature, the systemd unit and Gazebo with the Pi in the loop are not yet run.

**Do not connect the Pi to a real flight controller.** AEGIS refuses to command anything that
does not prove it is a simulator, and no hardware path exists in this release.

## Part 1: Set up the Raspberry Pi 5

### What you need

| Item | Why |
| --- | --- |
| Raspberry Pi 5 (4 GB or more) | |
| Official 27 W USB-C power supply (5.1 V, 5 A) | A phone charger causes under-voltage: slowdowns and resets. AEGIS raises an alert when the Pi reports it. |
| Active cooler, or a case with a fan | A bare Pi 5 under load reaches its 85 C limit and slows itself down. |
| microSD card, 32 GB or more, A2 class | An NVMe drive on an M.2 HAT is faster, but not needed. |
| A card reader for your PC | To write the operating system. |

A monitor, keyboard and mouse are optional: the steps below set the Pi up to be reached over
your network from the start.

### Write the operating system

1. On your Windows PC, install **Raspberry Pi Imager** from raspberrypi.com/software and put
   the microSD card in the reader.
2. In Imager choose the device **Raspberry Pi 5**, the operating system **Raspberry Pi OS
   (64-bit)** (the recommended entry), and the card as storage. It must be 64-bit.
3. When Imager offers to customise the settings, accept and fill in:
   - **Hostname:** `aegis-pi`
   - **Username and password:** your choice; you will log in with these
   - **Wi-Fi:** your network name, password and country (skip if you will plug in Ethernet)
   - **Time zone and keyboard layout**
   - **Enable SSH**, with password authentication
4. Write the card. This erases it.

### First start

1. Put the card in the Pi, fit the cooler, and plug in the power supply. The first start takes
   two to three minutes.
2. From Windows PowerShell or your Ubuntu (WSL) terminal:
   ```bash
   ssh YOUR_USERNAME@aegis-pi.local
   ```
   If the name is not found, look up the Pi's address in your router's device list and use
   that instead (`ssh YOUR_USERNAME@192.168.x.y`).
3. Update it, then restart:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```
4. Log in again and check the basics:
   ```bash
   uname -m                   # must print aarch64 (64-bit)
   cat /etc/os-release | head -2
   vcgencmd measure_temp      # idle is typically 40-55 C with a cooler
   vcgencmd get_throttled     # throttled=0x0 means power and temperature are fine
   hostname -I                # the Pi's address on your network; note it down
   ```
   Anything other than `0x0` from `get_throttled` right after start-up points at the power
   supply.

## Part 2: Install AEGIS on the Pi

On the Pi:

```bash
sudo apt install -y git
git clone https://github.com/rmanna80/AEGIS-ONE.git ~/AEGIS-ONE
cd ~/AEGIS-ONE
bash scripts/setup_pi.sh
```

It installs the backend, downloads the detection model, builds the dashboard (the slow part)
and writes a `.env` with a new operator token and the network MAVLink listener. It does not
install PX4 or Gazebo. Then see what the Pi can do:

```bash
.venv/bin/python scripts/pi_benchmark.py
```

That times object detection, the thermal pipeline and depth mapping on the Pi and compares
them with what a flight needs. Measured medians: 158 ms, 15 ms and 21 ms on a Pi 5; 78 ms, 11 ms and
14 ms on a 2-core x86 virtual machine.

## Part 3: Fly the simulator with the Pi in the loop

1. **Pi:** start the backend.
   ```bash
   cd ~/AEGIS-ONE && bash scripts/run_backend.sh
   ```
2. **PC (Ubuntu / WSL):** start the simulator with the Pi's address (from `hostname -I`).
   ```bash
   cd ~/AEGIS-ONE && git pull
   AEGIS_COMPANION_IP=192.168.x.y bash scripts/run_px4.sh sih
   ```
   A few seconds after it starts it prints `PX4 instance 0 now also sends MAVLink to ...`,
   and the Pi's backend log shows `Simulator attested`. The link belongs to the running
   simulator: if the simulator is restarted without `AEGIS_COMPANION_IP`, the Pi loses its
   vehicle (this happened in the first test flight). `scripts/link_companion.sh <address>`
   adds the link to a simulator that is already running.
3. **PC:** open the dashboard through an SSH tunnel. In another Ubuntu (WSL) terminal:
   ```bash
   ssh -L 8000:127.0.0.1:8000 YOUR_USERNAME@aegis-pi.local
   ```
   Leave it open and browse to `http://127.0.0.1:8000`. Stop any AEGIS backend still running
   on the PC first, since it would occupy the same port.
4. **Pi** (a second SSH session): run a demo. The token is in `~/AEGIS-ONE/.env`.
   ```bash
   cd ~/AEGIS-ONE
   .venv/bin/python scripts/demo1_survey.py --token "$(grep '^AEGIS_OPERATOR_TOKEN=' .env | tail -1 | cut -d= -f2)"
   ```

The dashboard's **Security** section shows the Pi's model, temperature and any power flags
under "This computer". It will list the MAVLink listener as a warning: on the Pi it has to
accept packets from the network, because the simulator is on another machine.

### If the Pi never shows "Simulator attested"

- Check the address: `ping 192.168.x.y` from the Ubuntu terminal.
- Both machines must be on the same network (a guest Wi-Fi often isolates devices).
- WSL2's default networking carried the link in testing. If packets do not arrive on another
  setup, allow outbound UDP for WSL in Windows Firewall, or try mirrored networking
  (`networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig`, then
  `wsl --shutdown`); neither was needed, so neither is tested.
- Was the simulator restarted? A new instance has no link unless it was started with
  `AEGIS_COMPANION_IP`.
- On the Pi, `sudo ss -lun | grep 14540` should show the listener.

### Start the backend with the Pi (optional)

`deploy/aegis-backend.service` is a systemd unit that starts the backend at boot. Its header
has the install and removal commands.

## What runs where

| | PC | Raspberry Pi |
| --- | --- | --- |
| PX4 SITL (and Gazebo, if used) | yes | no |
| AEGIS backend, monitors, audit log, recordings | no | yes |
| Dashboard files | no | served from the Pi |
| Browser | yes | no |

## Limits

- Wi-Fi adds delay and drops packets. Expect occasional `link.lost` or `telemetry.stale`
  alerts that a cable would not produce; Ethernet is better for the Pi.
- The software thermal and depth sensors run on the Pi. Gazebo camera or depth frames would
  have to be sent to the Pi (`gz_camera_bridge.py --host <Pi address>`, with
  `AEGIS_CAMERA_STREAM_HOST=0.0.0.0` on the Pi); that path is untested and unencrypted.
- The tunnel keeps the dashboard private. `AEGIS_HOST=0.0.0.0 bash scripts/run_backend.sh`
  makes it reachable by anyone on the network over plain HTTP, with the operator token as
  the only protection. The Security section flags it.
- The Pi's clock comes from the network. Recordings made while it is wrong carry wrong times.

## Measuring the Pi during a flight

`scripts/pi_benchmark.py` times each workload alone. To measure the Pi while it is flying a
simulated vehicle (CPU, memory, temperature, loop lag, telemetry arrival rate, network
round trip, and camera frame rate and detection time when perception is on):

```bash
.venv/bin/python scripts/edge_measure.py --seconds 90 --peer <PC address>
```

Run it while a demo is in flight. The full procedure, and the list of what has and has not
been verified on the Pi, is in [docs/hardware/README.md](../hardware/README.md), section 3.


## Part 4: The USB webcam (a physical camera on the bench)

Your USB webcam is AEGIS's first physical sensor. Plugged into the Pi, it is labelled
`physical` and shown as a **bench camera**: it is not on any vehicle, so what it sees has
nothing to do with the simulated flight, and AEGIS never places its detections in the
world. Detection is by object class only; nobody is identified.

After `git pull`, reinstall once (a signing library was added):

```bash
cd ~/AEGIS-ONE && git pull && .venv/bin/pip install -e '.[perception]'
```

### 1. Check the camera and measure the detector on it

Stop the backend first if it is running (only one program can open the camera).

```bash
cd ~/AEGIS-ONE
v4l2-ctl --list-devices                         # optional: sudo apt install v4l-utils
.venv/bin/python scripts/camera_check.py --seconds 30
```

It finds the webcam by its driver, so it works whatever device number the webcam gets
(on this Pi it was `/dev/video0`; with a camera module attached it would be higher), then reports what format it agreed to, how many frames per second it really
delivers, how fast the detector processes them, how many it had no time for, and the
temperature before and after. Nothing is saved but the numbers
(`data/generated/camera_checks/`). Add `--snapshot` to save one picture if you want to see
what it sees.

### 2. Use it in AEGIS

In the Pi's `.env`:

```bash
AEGIS_CAMERA=usb
```

Then start the simulator on the PC and the backend on the Pi as in Part 3. The dashboard's
camera panel shows **PHYSICAL**, the bench-camera notice, the camera's delivered frame
rate, the share of frames not processed, and what is being recorded. By default the
**video is not saved** (it would record your room and anyone in it); labels and boxes
are. Set `AEGIS_RECORD_CAMERA_VIDEO=always` if you want the video kept.

Measure everything together during a flight:

```bash
.venv/bin/python scripts/edge_measure.py --seconds 90 --peer 192.168.1.7 --label "Pi 5, webcam, Demo 1"
```

### What the runs showed

Owner's Pi 5, USB webcam at 640×480 MJPG, simulator on the PC, Demo 1 flown in every
run, 90 s of measurement each (2026-11-15). One run per setting.

| | A: as installed then | B: `AEGIS_DETECTOR_THREADS=4` | C: `AEGIS_DETECTOR_MAX_FPS=2` |
| --- | --- | --- | --- |
| CPU, all 4 cores (median) | 92% | 99.5% | 78% |
| Temperature (max) | 61.1 °C | 63.9 °C | 58.4 °C |
| Frames detected per second | 2.6 | 2.0 | 2.0 |
| Detection time per frame (median) | 348 ms | 273 ms | 231 ms |
| Fresh vehicle data, longest gap | 205 ms | 505 ms | 202 ms |
| Vehicle data age (max) | 220 ms | 526 ms | 262 ms |
| Round trip Pi to PC (median) | 4.8 ms | 139 ms | 11.2 ms |
| Webcam frames delivered per second (median) | 7.6 | 15.0 | 7.6 |

No run raised an alert or showed throttling. What follows from them:

- **Four detector threads made things worse.** No more frames were detected, the CPU
  was saturated, the gap between fresh vehicle updates more than doubled and the round
  trip timed from the Pi rose from about 5 ms to 139 ms (most likely the Pi being too busy
  to handle the reply, since only the thread count changed). Leave the default of two.
- **Capping the detector at 2 frames per second freed about 14 points of CPU** for 23%
  fewer detections, ran 3 °C cooler, and each detection was quicker. `setup_pi.sh` now
  writes `AEGIS_DETECTOR_MAX_FPS=2` into a new `.env`; add it to yours by hand.
- **Even capped, the Pi sat at 78%.** Inference at 2 frames per second accounts for
  roughly a quarter of the processor, so something else is using about half: candidates
  are ONNX Runtime threads spinning between frames (now off by default), decoding every
  webcam frame, the dashboard if it is open on the Pi itself, and the MAVSDK server. A
  run without the camera is needed to tell.
- **The webcam agreed to 30 frames per second but delivered 7.6 to 15.** It was not CPU:
  the fastest delivery came in the busiest run. Webcams commonly lower their rate in dim
  light; that is not confirmed here.
- One 90-second run per setting: differences of a few percent or milliseconds (for
  example the round trip in A and C) are within what one run cannot separate from noise.

The first run, on 2026-11-14, also showed that `edge_measure.py`'s ping to a PC that did
not answer (Windows blocks ping by default) stalled its sampling; fixed. To let the PC
answer ping from your own network only, in PowerShell as administrator:

```powershell
New-NetFirewallRule -DisplayName "AEGIS ping test" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress LocalSubnet -Action Allow
# remove it afterwards:
Remove-NetFirewallRule -DisplayName "AEGIS ping test"
```

## Part 5: Close the MAVSDK control port to the network

The MAVSDK server listens on port 50051 on every network interface, and it cannot be told
not to. Anyone on your network who can reach it can command the autopilot without the
operator token. Close it to other computers:

```bash
sudo bash scripts/lock_mavsdk_port.sh
```

Then check **from your PC** (the Pi cannot test itself):

```bash
nc -zv 192.168.1.57 50051        # must fail or time out
```

To keep the rule after a reboot, install `deploy/aegis-firewall.service` (instructions at
the top of that file). To undo: `sudo bash scripts/lock_mavsdk_port.sh --remove`.

## Part 6: This Pi's signing key

Incident packages made on the Pi are signed with a key that only the Pi has
(`~/.config/aegis/device_ed25519.pem`, created the first time it is needed, readable by
you only). See its fingerprint with:

```bash
.venv/bin/python scripts/device_key.py
```

Write that fingerprint down somewhere else. To check a package came from this Pi:
`python scripts/incident_tool.py verify <package> --trust <fingerprint>`. Never copy the
key file into the repository or share it.
