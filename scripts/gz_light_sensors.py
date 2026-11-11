#!/usr/bin/env python3
"""Lower the resolution and frame rate of PX4's Gazebo camera models, or restore them.

    python3 scripts/gz_light_sensors.py apply   [PX4_DIR]
    python3 scripts/gz_light_sensors.py restore [PX4_DIR]
    python3 scripts/gz_light_sensors.py status  [PX4_DIR]

Why: PX4's stock models render a 1920x1080 colour camera and a 640x480 depth
camera at 30 Hz each (x500_depth), or 1280x960 at 30 Hz (x500_mono_cam). On a
machine where Gazebo cannot keep up, the whole simulation slows to a fraction of
real time. AEGIS fuses depth at 5 Hz and runs detection at about 10 Hz, so the
extra frames are wasted.

This edits two files inside your PX4 checkout (Tools/simulation/gz/models). The
originals are kept next to them as model.sdf.aegis-orig and `restore` puts them
back exactly. Field of view and range are not changed.
"""

from __future__ import annotations

import re
import shutil
import sys
import xml.dom.minidom
from pathlib import Path

# model -> sensor name -> (width, height, update rate)
LIGHT = {
    "OakD-Lite": {"IMX214": (640, 360, 10), "StereoOV7251": (320, 240, 10)},
    "mono_cam": {"imager": (640, 480, 15)},
}
BACKUP_SUFFIX = ".aegis-orig"


def lighten(text: str, sensors: dict[str, tuple[int, int, int]]) -> str:
    for name, (width, height, rate) in sensors.items():
        pattern = re.compile(rf'(<sensor name="{re.escape(name)}".*?</sensor>)', re.DOTALL)
        match = pattern.search(text)
        if match is None:
            raise ValueError(f'sensor "{name}" not found')
        block = match.group(1)
        new = re.sub(r"<width>\d+</width>", f"<width>{width}</width>", block, count=1)
        new = re.sub(r"<height>\d+</height>", f"<height>{height}</height>", new, count=1)
        new = re.sub(
            r"<update_rate>[\d.]+</update_rate>", f"<update_rate>{rate}</update_rate>", new, count=1
        )
        if new == block:
            raise ValueError(f'sensor "{name}" has no width/height/update_rate to change')
        text = text.replace(block, new)
    xml.dom.minidom.parseString(text.lstrip("﻿"))  # refuse to write a broken model
    return text


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("apply", "restore", "status"):
        print(__doc__)
        return 2
    action = sys.argv[1]
    px4_dir = Path(sys.argv[2] if len(sys.argv) > 2 else Path.home() / "PX4-Autopilot")
    models = px4_dir / "Tools" / "simulation" / "gz" / "models"
    if not models.is_dir():
        print(f"No Gazebo models in {models}", file=sys.stderr)
        return 1
    for model, sensors in LIGHT.items():
        sdf = models / model / "model.sdf"
        backup = sdf.with_name(sdf.name + BACKUP_SUFFIX)
        if not sdf.is_file():
            print(f"{model}: not present, skipped")
            continue
        if action == "status":
            print(f"{model}: {'light' if backup.exists() else 'stock'}")
        elif action == "apply":
            if backup.exists():
                print(f"{model}: already light")
                continue
            original = sdf.read_text(encoding="utf-8")
            light = lighten(original, sensors)
            shutil.copy2(sdf, backup)
            sdf.write_text(light, encoding="utf-8")
            summary = ", ".join(f"{n} {w}x{h}@{r}Hz" for n, (w, h, r) in sensors.items())
            print(f"{model}: now {summary}")
        elif backup.exists():
            shutil.move(backup, sdf)
            print(f"{model}: restored to stock")
        else:
            print(f"{model}: already stock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
