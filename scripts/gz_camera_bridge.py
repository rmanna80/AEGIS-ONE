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

With --depth it also reads the simulator's ground-truth vehicle pose and attaches it
to each frame. AEGIS uses that ONLY to score the map and to measure the autopilot's
pose error; mapping and route decisions keep using the autopilot's estimate.
Disable with --no-truth-pose.

STATUS: the RGB and --depth paths have been run on the project owner's machine. The
ground-truth pose path is written against the Gazebo Harmonic API documentation and
has not been run yet. Simulated frames only (provenance is fixed to "simulated").
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
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
        # PX4's x500_depth (OakD-Lite model) publishes depth on the fixed topic
        # "/depth_camera"; other models use ".../depth_image". Point clouds and
        # camera_info topics share the prefix and are not images.
        candidates = [
            t
            for t in topics
            if "depth" in t.lower()
            and not t.endswith(("/points", "/camera_info"))
            and "camera_info" not in t
        ]
        candidates.sort(key=lambda t: (not t.endswith(("/depth_camera", "/depth_image")), t))
        launch = "bash scripts/run_px4.sh gazebo-depth"
    else:
        candidates = [t for t in topics if t.endswith("/image") and "depth" not in t]
        launch = "bash scripts/run_px4.sh gazebo-cam"
    if not candidates:
        kind = "depth image" if depth else "camera image"
        sys.exit(
            f"No {kind} topic found. Start a vehicle that has one first:\n"
            f"  {launch}\n"
            f"Topics seen: {len(topics)}\n"
            + "\n".join(f"  {t}" for t in topics if "image" in t or "depth" in t or "camera" in t)
        )
    if len(candidates) > 1:
        print(f"Several topics found, using the first: {candidates}", file=sys.stderr)
    return candidates[0]


class TruthPose:
    """Recent ground-truth poses of the vehicle model, looked up by simulation time."""

    def __init__(self, node: Node, model: str | None) -> None:
        self.model = model
        self.lock = threading.Lock()
        self.stamps: list[float] = []
        self.poses: list[dict[str, list[float]]] = []
        self.received = 0
        self.topic: str | None = None
        try:
            from gz.msgs10.pose_v_pb2 import Pose_V
        except ImportError as exc:  # pragma: no cover - depends on the Gazebo install
            print(f"ground-truth pose unavailable ({exc})", file=sys.stderr)
            return
        topics = subprocess.run(
            ["gz", "topic", "-l"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.split()
        found = [t for t in topics if re.fullmatch(r"/world/[^/]+/dynamic_pose/info", t)]
        if not found:
            print("ground-truth pose unavailable (no dynamic_pose topic)", file=sys.stderr)
            return
        if node.subscribe(Pose_V, found[0], self._on_poses):
            self.topic = found[0]

    def _on_poses(self, message) -> None:  # noqa: ANN001 - protobuf type from Gazebo
        stamp = message.header.stamp.sec + message.header.stamp.nsec * 1e-9
        for pose in message.pose:
            if self.model is None and re.fullmatch(r"x500\w*_\d+", pose.name):
                self.model = pose.name
                print(f"ground-truth pose: following model {pose.name}")
            if pose.name != self.model:
                continue
            p, q = pose.position, pose.orientation
            entry = {
                "position_enu_m": [p.x, p.y, p.z],
                "quaternion_wxyz_flu_enu": [q.w, q.x, q.y, q.z],
            }
            with self.lock:
                self.stamps.append(stamp)
                self.poses.append(entry)
                del self.stamps[:-400], self.poses[:-400]
                self.received += 1
            return

    def at(self, stamp: float) -> dict[str, list[float]] | None:
        """The pose nearest to ``stamp`` (simulation seconds), if within 0.1 s."""
        with self.lock:
            if not self.stamps:
                return None
            i = bisect.bisect_left(self.stamps, stamp)
            best = min(
                (j for j in (i - 1, i) if 0 <= j < len(self.stamps)),
                key=lambda j: abs(self.stamps[j] - stamp),
            )
            if abs(self.stamps[best] - stamp) > 0.1:
                return None
            return self.poses[best]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topic", help="Gazebo image topic (auto-detected when omitted)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--depth", action="store_true", help="forward the depth camera")
    parser.add_argument("--port", type=int, help="default 5600 (camera) or 5601 (depth)")
    parser.add_argument("--max-fps", type=float, help="default 15 (camera) or 5 (depth)")
    parser.add_argument("--no-truth-pose", action="store_true", help="do not attach it")
    parser.add_argument("--model", help="Gazebo model to follow (default: the x500_*_N vehicle)")
    args = parser.parse_args()

    topic = args.topic or find_camera_topic(args.depth)
    port = args.port or (5601 if args.depth else 5600)
    encodings = DEPTH_ENCODINGS if args.depth else ENCODINGS
    sock = socket.create_connection((args.host, port))
    lock = threading.Lock()
    state = {"last_sent": 0.0, "sent": 0, "skipped_format": 0, "alive": True, "with_truth": 0}
    max_fps = args.max_fps or (5.0 if args.depth else 15.0)
    min_period = 1.0 / max_fps

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
        fields = {
            "width": message.width,
            "height": message.height,
            "encoding": encoding,
            "sensor_id": "gz-depth" if args.depth else "gz-camera",
            "provenance": "simulated",
            "source": "gazebo",
            "source_time_s": stamp.sec + stamp.nsec * 1e-9,
        }
        if truth is not None:
            pose = truth.at(fields["source_time_s"])
            if pose is not None:
                fields["truth_pose"] = pose
                state["with_truth"] += 1
        header = json.dumps(fields).encode()
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
    truth = TruthPose(node, args.model) if args.depth and not args.no_truth_pose else None
    if truth is not None and truth.topic:
        print(f"Ground-truth pose from {truth.topic} (used by AEGIS for scoring only)")
    if not node.subscribe(Image, topic, on_image):
        sys.exit(f"Could not subscribe to {topic}")
    print(f"Forwarding {topic} -> {args.host}:{port} at up to {max_fps:g} fps")
    try:
        while state["alive"]:
            time.sleep(5.0)
            line = f"frames sent: {state['sent']}  unsupported-format: {state['skipped_format']}"
            if truth is not None:
                line += f"  with ground-truth pose: {state['with_truth']}"
            print(line)
    except KeyboardInterrupt:
        pass
    if not state["alive"]:
        setting = "AEGIS_DEPTH=stream" if args.depth else "AEGIS_CAMERA=stream"
        print(f"AEGIS closed the connection (is the backend running with {setting}?)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
