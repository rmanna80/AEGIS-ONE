#!/usr/bin/env bash
# Download the default object-detection model (YOLO11n, ONNX, about 11 MB) into models/.
#
# LICENCE: Ultralytics YOLO models are AGPL-3.0 (or a commercial Ultralytics
# licence). The file is therefore downloaded, not committed. Any other
# Ultralytics-export-compatible detection ONNX can be used instead via
# AEGIS_DETECTOR_MODEL.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx"
mkdir -p models
if [ -f models/yolo11n.onnx ]; then
  echo "models/yolo11n.onnx already present."
else
  curl -fL --progress-bar -o models/yolo11n.onnx.part "$URL"
  mv models/yolo11n.onnx.part models/yolo11n.onnx
  echo "Saved models/yolo11n.onnx"
fi
