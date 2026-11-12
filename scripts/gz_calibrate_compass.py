#!/usr/bin/env python3
"""Make PX4's compass declination match the magnetic field Gazebo simulates (SIMULATION ONLY).

Measured on the project owner's machine: with the vehicle on the pad facing due east,
Gazebo's simulated magnetic field points about 2.5 degrees WEST of true north, while PX4
looks up a declination of 3.4 degrees EAST for that location. PX4 therefore believes it
is facing about 6 degrees clockwise of where it is, and maps built from depth land in
the wrong place. Why the two disagree is not known (it is upstream of AEGIS).

This script measures the simulated field with the vehicle sitting still, using Gazebo's
true heading for the vehicle, and tells PX4 to use that declination instead of its
lookup table. It changes two parameters of the SIMULATED autopilot and nothing else:

    EKF2_MAG_DECL   the declination to use
    EKF2_DECL_TYPE  0 = use EKF2_MAG_DECL as given (no table lookup, no auto-save)

Usage, with `bash scripts/run_px4.sh gazebo-depth urban` running and the vehicle on the pad:

    python3 scripts/gz_calibrate_compass.py            # measure and report only
    python3 scripts/gz_calibrate_compass.py --apply    # also set the parameters
    python3 scripts/gz_calibrate_compass.py --restore  # put the previous values back

After --apply or --restore, restart the simulator so the estimator starts with the new
value. The measured declination and the previous values are kept in
data/generated/compass_calibration.json; scripts/run_px4.sh applies the measured value to
Gazebo launches and PX4's own lookup to built-in-simulator launches, so each simulator
gets the declination that matches its magnetic field.

Never point this at a real autopilot: a real compass is calibrated by the procedure in
the PX4 documentation, not by copying a simulator's field.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAVED = REPO_ROOT / "data" / "generated" / "compass_calibration.json"


def px4_dir() -> Path:
    if os.environ.get("PX4_DIR"):
        return Path(os.environ["PX4_DIR"])
    for line in (REPO_ROOT / "scripts" / "versions.env").read_text().splitlines():
        match = re.match(r'\s*(?:export\s+)?PX4_DIR="?\$\{PX4_DIR:-([^}"]+)\}"?', line)
        if match:
            return Path(os.path.expandvars(match.group(1).replace("$HOME", str(Path.home()))))
    return Path.home() / "PX4-Autopilot"


def run(command: list[str], timeout: float = 15.0, env: dict[str, str] | None = None) -> str:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=env, check=False
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        sys.exit(f"Could not run {' '.join(command)}: {exc}")


class Px4:
    def __init__(self, folder: Path) -> None:
        self.bin = folder / "build" / "px4_sitl_default" / "bin"
        if not (self.bin / "px4-param").exists():
            sys.exit(f"PX4 command-line tools not found in {self.bin}.")

    def listen(self, topic: str) -> str:
        return run([str(self.bin / "px4-listener"), topic], timeout=10.0)

    def param(self, name: str) -> float:
        text = run([str(self.bin / "px4-param"), "show", name])
        match = re.search(rf"{name}\s*\[[^\]]*\]\s*:\s*(-?[\d.]+)", text)
        if not match:
            sys.exit(f"Could not read {name}. Is the simulator running?\n{text}")
        return float(match.group(1))

    def set_param(self, name: str, value: float) -> None:
        text = run([str(self.bin / "px4-param"), "set", name, f"{value:g}"])
        if abs(self.param(name) - value) > 1e-3:
            sys.exit(f"Setting {name} did not take effect.\n{text}")

    def simulated(self) -> bool:
        """Only a simulation airframe may be touched (SYS_AUTOSTART 4000-4999: Gazebo;
        10040-10049: built-in simulator, accepted for testing this script)."""
        autostart = int(self.param("SYS_AUTOSTART"))
        return 4000 <= autostart <= 4999 or 10040 <= autostart <= 10049

    def magnetometer(self, samples: int) -> tuple[float, float, float]:
        readings = []
        for _ in range(samples):
            text = self.listen("sensor_mag")
            values = [re.search(rf"\b{axis}: (-?[\d.]+)", text) for axis in "xyz"]
            if all(values):
                readings.append(tuple(float(v.group(1)) for v in values if v))
        if len(readings) < max(5, samples // 2):
            sys.exit("Could not read the simulated magnetometer (sensor_mag).")
        return tuple(statistics.fmean(r[i] for r in readings) for i in range(3))  # type: ignore[return-value]

    def estimated_heading_deg(self) -> float:
        match = re.search(r"Yaw: (-?[\d.]+) deg", self.listen("vehicle_attitude"))
        if not match:
            sys.exit("Could not read PX4's attitude estimate.")
        return float(match.group(1)) % 360.0


def gazebo_true_heading_deg(model: str | None) -> tuple[float, str]:
    """True heading from Gazebo's own pose of the vehicle (0 = north, 90 = east)."""
    env = dict(os.environ, GZ_IP=os.environ.get("GZ_IP", "127.0.0.1"))
    if model is None:
        names = re.findall(r"- (x500\w*_\d+)", run(["gz", "model", "--list"], env=env))
        if not names:
            sys.exit(
                "No x500 vehicle found in Gazebo. Start it first:\n"
                "  bash scripts/run_px4.sh gazebo-depth urban"
            )
        model = names[0]
    text = run(["gz", "model", "-m", model, "-p"], env=env)
    rows = re.findall(r"\[\s*(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\]", text)
    if len(rows) < 2:
        sys.exit(f"Could not read the pose of {model} from Gazebo.\n{text}")
    roll, pitch, yaw = (math.degrees(float(v)) for v in rows[1])
    if abs(roll) > 3.0 or abs(pitch) > 3.0:
        sys.exit(f"{model} is not level (roll {roll:.1f}, pitch {pitch:.1f}); leave it on the pad.")
    # Gazebo yaw is counter-clockwise from east; heading is clockwise from north.
    return (90.0 - yaw) % 360.0, model


def declination_deg(field_frd: tuple[float, float, float], heading_deg: float) -> float:
    """Direction of the horizontal field, clockwise from true north, for a level vehicle
    whose nose points at ``heading_deg`` (body x forward, y right)."""
    x, y, _ = field_frd
    h = math.radians(heading_deg)
    north = x * math.cos(h) - y * math.sin(h)
    east = x * math.sin(h) + y * math.cos(h)
    return math.degrees(math.atan2(east, north))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="set the PX4 parameters")
    parser.add_argument("--restore", action="store_true", help="restore the saved values")
    parser.add_argument("--model", help="Gazebo model name (default: the x500 vehicle found)")
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument(
        "--true-heading", type=float, help="skip Gazebo and use this true heading (degrees)"
    )
    args = parser.parse_args()
    px4 = Px4(px4_dir())
    if not px4.simulated():
        sys.exit("Refusing: this autopilot is not a simulation airframe.")

    if args.restore:
        if not SAVED.is_file():
            sys.exit("Nothing to restore (no saved values).")
        saved = json.loads(SAVED.read_text())
        px4.set_param("EKF2_DECL_TYPE", saved["EKF2_DECL_TYPE"])
        px4.set_param("EKF2_MAG_DECL", saved["EKF2_MAG_DECL"])
        SAVED.unlink()  # run_px4.sh stops applying the Gazebo declination
        print(
            f"Restored EKF2_DECL_TYPE={saved['EKF2_DECL_TYPE']:g}, "
            f"EKF2_MAG_DECL={saved['EKF2_MAG_DECL']:g}. Restart the simulator."
        )
        return 0

    if args.true_heading is not None:
        heading, source = args.true_heading % 360.0, "given on the command line"
    else:
        heading, model = gazebo_true_heading_deg(args.model)
        source = f"Gazebo's pose of {model}"
    field = px4.magnetometer(args.samples)
    measured = declination_deg(field, heading)
    current_decl = px4.param("EKF2_MAG_DECL")
    current_type = px4.param("EKF2_DECL_TYPE")
    estimate = px4.estimated_heading_deg()
    error = (estimate - heading + 180.0) % 360.0 - 180.0
    print(f"true heading:                 {heading:6.1f} deg  ({source})")
    print(f"PX4's heading estimate:       {estimate:6.1f} deg  (error {error:+.1f} deg)")
    print(f"simulated field declination:  {measured:+6.2f} deg  (mean of {args.samples} readings)")
    print(
        f"PX4 declination in use:       {current_decl:+6.2f} deg  (EKF2_DECL_TYPE {current_type:g})"
    )
    print(f"mismatch:                     {current_decl - measured:+6.2f} deg")

    if not args.apply:
        print(
            "Nothing changed. Run again with --apply to set PX4's declination to the "
            "simulated field's."
        )
        return 0
    # Keep the ORIGINAL values (not ones from an earlier --apply), and record the measured
    # declination so scripts/run_px4.sh can apply it to Gazebo launches only.
    saved = json.loads(SAVED.read_text()) if SAVED.is_file() else {}
    saved.setdefault("EKF2_DECL_TYPE", current_type)
    saved.setdefault("EKF2_MAG_DECL", current_decl)
    saved["gazebo_declination_deg"] = round(measured, 2)
    SAVED.parent.mkdir(parents=True, exist_ok=True)
    SAVED.write_text(json.dumps(saved, indent=2))
    px4.set_param("EKF2_DECL_TYPE", 0)
    px4.set_param("EKF2_MAG_DECL", round(measured, 2))
    print(
        f"Set EKF2_DECL_TYPE=0 and EKF2_MAG_DECL={measured:.2f}. Previous values saved in "
        f"{SAVED.relative_to(REPO_ROOT)}."
    )
    print(
        "Restart the simulator, then run this script again without --apply: the heading "
        "error should be under 1 degree."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
