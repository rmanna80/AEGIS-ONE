#!/usr/bin/env python3
"""Send a video file to AEGIS over the camera frame stream (AEGIS_CAMERA=stream).

Useful for trying detection and tracking on your own footage, and as a
reference sender for the wire format documented in aegis/perception/cameras.py.

    python scripts/stream_video.py path/to/video.mp4 [--loop] [--port 5600]

Frames are labelled ``replayed`` because they are prerecorded.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import time

import cv2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5600)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--jpeg-quality", type=int, default=85)
    args = parser.parse_args()

    capture = cv2.VideoCapture(args.video)
    if not capture.isOpened():
        raise SystemExit(f"Cannot open {args.video}")
    fps = capture.get(cv2.CAP_PROP_FPS)
    period = 1.0 / (fps if 1.0 <= fps <= 240.0 else 30.0)

    with socket.create_connection((args.host, args.port)) as sock:
        print(f"Streaming {args.video} to {args.host}:{args.port} (Ctrl+C to stop)")
        next_due = time.monotonic()
        while True:
            ok, image = capture.read()
            if not ok:
                if not args.loop:
                    break
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality])
            header = json.dumps(
                {
                    "width": image.shape[1],
                    "height": image.shape[0],
                    "encoding": "jpeg",
                    "sensor_id": "video-file",
                    "provenance": "replayed",
                    "source": "video-file",
                    "source_time_s": capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0,
                }
            ).encode()
            payload = jpeg.tobytes()
            sock.sendall(struct.pack(">I", len(header)) + header)
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            next_due += period
            time.sleep(max(0.0, next_due - time.monotonic()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, BrokenPipeError, ConnectionError) as exc:
        raise SystemExit(f"stopped ({type(exc).__name__})") from None
