#!/usr/bin/env python3
"""Forward a Gazebo Harmonic camera topic to AEGIS (AEGIS_CAMERA=stream).

Run this with the SYSTEM Python, not the project virtualenv: it needs the
Gazebo Python bindings that are installed with Gazebo Harmonic
(python3-gz-transport13, python3-gz-msgs10) and nothing else.

    /usr/bin/python3 scripts/gz_camera_bridge.py            # auto-detect the camera topic
    /usr/bin/python3 scripts/gz_camera_bridge.py --topic /world/.../image
    /usr/bin/python3 scripts/gz_camera_bridge.py --depth    # depth camera -> AEGIS_DEPTH=stream

It is a separate process on purpose: Gazebo's protobuf build and the backend's
gRPC stack have conflicting dependencies, and this keeps them apart.

STATUS: the RGB path has been run on the project owner's machine. The --depth
path is written against the Gazebo Harmonic API documentation and has not been
run yet. Simulated frames only (provenance is fixed to "simulated").
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    from gz.msgs10.image_pb2 import Image
    from gz.transport13 import Node
except ImportError as exc:  # pragma: no cover - depends on the Gazebo install
    sys.exit(
        f"Gazebo Python bindings not found ({exc}).\n"
        "Install them with: sudo apt install python3-gz-transport13 python3-gz-msgs10\n"
        "and run this script with /usr/bin/python3 (not the project .venv)."
    )

# gz.msgs.PixelFormatType values -> frame-stream encodings
ENCODINGS = {1: "mono8", 3: "rgb8", 8: "bgr8"}  # L_INT8, RGB_INT8, BGR_INT8
DEPTH_ENCODINGS = {13: "depth32f", 2: "depth16u"}  # R_FLOAT32 (metres), L_INT16 (millimetres)


def find_camera_topic(depth: bool) -> str:
    topics = subprocess.run(
        ["gz", "topic", "-l"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.split()
    if depth:
        candidates = [t for t in topics if t.endswith("/depth_image")]
        launch = "bash scripts/run_px4.sh gazebo-depth"
    else:
        candidates = [t for t in topics if t.endswith("/image") and "depth" not in t]
        launch = "bash scripts/run_px4.sh gazebo-cam"
    if not candidates:
        kind = "depth image" if depth else "camera image"
        sys.exit(
            f"No {kind} topic found. Start a vehicle that has one first:\n"
            f"  {launch}\n"
            f"Topics seen: {len(topics)}"
        )
    if len(candidates) > 1:
        print(f"Several topics found, using the first: {candidates}", file=sys.stderr)
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topic", help="Gazebo image topic (auto-detected when omitted)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--depth", action="store_true", help="forward the depth camera")
    parser.add_argument("--port", type=int, help="default 5600 (camera) or 5601 (depth)")
    parser.add_argument("--max-fps", type=float, default=15.0)
    args = parser.parse_args()

    topic = args.topic or find_camera_topic(args.depth)
    port = args.port or (5601 if args.depth else 5600)
    encodings = DEPTH_ENCODINGS if args.depth else ENCODINGS
    sock = socket.create_connection((args.host, port))
    lock = threading.Lock()
    state = {"last_sent": 0.0, "sent": 0, "skipped_format": 0, "alive": True}
    min_period = 1.0 / args.max_fps

    def on_image(message: Image) -> None:
        now = time.monotonic()
        if now - state["last_sent"] < min_period or not state["alive"]:
            return
        encoding = encodings.get(message.pixel_format_type)
        if encoding is None:
            if state["skipped_format"] == 0:
                print(f"unsupported pixel format {message.pixel_format_type}", file=sys.stderr)
            state["skipped_format"] += 1
            return
        stamp = message.header.stamp
        header = json.dumps(
            {
                "width": message.width,
                "height": message.height,
                "encoding": encoding,
                "sensor_id": "gz-depth" if args.depth else "gz-camera",
                "provenance": "simulated",
                "source": "gazebo",
                "source_time_s": stamp.sec + stamp.nsec * 1e-9,
            }
        ).encode()
        try:
            with lock:
                sock.sendall(struct.pack(">I", len(header)) + header)
                sock.sendall(struct.pack(">I", len(message.data)) + message.data)
        except OSError:
            state["alive"] = False
            return
        state["last_sent"] = now
        state["sent"] += 1

    node = Node()
    if not node.subscribe(Image, topic, on_image):
        sys.exit(f"Could not subscribe to {topic}")
    print(f"Forwarding {topic} -> {args.host}:{port} at up to {args.max_fps:g} fps")
    try:
        while state["alive"]:
            time.sleep(5.0)
            print(f"frames sent: {state['sent']}  unsupported-format: {state['skipped_format']}")
    except KeyboardInterrupt:
        pass
    if not state["alive"]:
        setting = "AEGIS_DEPTH=stream" if args.depth else "AEGIS_CAMERA=stream"
        print(f"AEGIS closed the connection (is the backend running with {setting}?)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
