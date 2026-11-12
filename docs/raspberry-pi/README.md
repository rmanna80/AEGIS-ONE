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

**Status: installed and benchmarked on a Raspberry Pi 5; not yet flown with it.** The setup
script and benchmark ran on the owner's Pi 5 (results in `docs/testing/README.md`). The extra
MAVLink link was flown end to end on one machine only (Demo 1, 5 of 5, with the backend
configured as on the Pi). The network path between WSL2 and the Pi, a flight with the Pi in
the loop, the tunnel and the systemd unit are still unverified.

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
2. **PC (Ubuntu / WSL):** start the simulator, then point it at the Pi (use the address from
   `hostname -I`).
   ```bash
   cd ~/AEGIS-ONE && git pull
   bash scripts/run_px4.sh sih
   # in a second terminal, once it shows "Ready for takeoff!":
   bash scripts/link_companion.sh 192.168.x.y
   ```
   Within a few seconds the Pi's backend log should show `Simulator attested`.
3. **PC:** open the dashboard through an SSH tunnel. In another Ubuntu (WSL) terminal:
   ```bash
   ssh -L 8000:127.0.0.1:8000 YOUR_USERNAME@aegis-pi.local
   ```
   Leave it open and browse to `http://127.0.0.1:8000`. Stop any AEGIS backend still running
   on the PC first, since it would occupy the same port.
4. **Pi** (a second SSH session): run a demo. The token is in `~/AEGIS-ONE/.env`.
   ```bash
   cd ~/AEGIS-ONE
   .venv/bin/python scripts/demo1_survey.py --token "$(grep AEGIS_OPERATOR_TOKEN .env | cut -d= -f2)"
   ```

The dashboard's **Security** section shows the Pi's model, temperature and any power flags
under "This computer". It will list the MAVLink listener as a warning: on the Pi it has to
accept packets from the network, because the simulator is on another machine.

### If the Pi never shows "Simulator attested"

- Check the address: `ping 192.168.x.y` from the Ubuntu terminal.
- Both machines must be on the same network (a guest Wi-Fi often isolates devices).
- WSL2 reaches the network through Windows. If packets do not arrive, allow outbound UDP for
  WSL in Windows Firewall, or switch WSL to mirrored networking (`networkingMode=mirrored`
  under `[wsl2]` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`). I have not been able
  to test either.
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
