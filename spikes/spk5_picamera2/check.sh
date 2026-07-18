#!/usr/bin/env bash
# SPK-5 reproducer — run ON THE PI. Proves apt python3-picamera2 imports through a
# uv --system-site-packages venv on system Python 3.11, then captures one frame.
# Deliverable context: spikes/spk5_picamera2/FINDINGS.md, issue #43 / AVID-43.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

echo "== apt camera stack =="
sudo apt-get install -y python3-picamera2
dpkg -l python3-picamera2 python3-libcamera | grep '^ii'

echo "== uv =="
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version

echo "== project venv: BOTH flags are load-bearing =="
work="$(mktemp -d)"
cd "$work"
# --python /usr/bin/python3 : force the SYSTEM 3.11 (else uv fetches managed 3.13
#                             whose site-packages has no apt picamera2).
# --system-site-packages    : expose /usr/lib/python3/dist-packages (the apt path).
uv venv --python /usr/bin/python3 --system-site-packages

echo "== interpreter + import resolution =="
.venv/bin/python - <<'PY'
import sys, picamera2, libcamera, numpy
from importlib.metadata import version
print("interpreter:", sys.executable)
print("python:", sys.version.split()[0])
print("picamera2:", version("picamera2"), "->", picamera2.__file__)
print("libcamera ->", libcamera.__file__)   # note: no pip dist-metadata, import still works
print("numpy:", numpy.__version__, "->", numpy.__file__)
PY

echo "== enumerate + capture one frame (needs a camera attached + a reboot after wiring) =="
.venv/bin/python - <<'PY'
from picamera2 import Picamera2
info = Picamera2.global_camera_info()
print("global_camera_info:", info)
if not info:
    print("NO CAMERA DETECTED — import proof still holds; reboot after wiring the CSI ribbon.")
    raise SystemExit(0)
cam = Picamera2()
cam.configure(cam.create_still_configuration(main={"size": (640, 480)}))
cam.start()
arr = cam.capture_array()
cam.stop(); cam.close()
print("captured frame:", arr.shape, arr.dtype)
PY

echo "== done: SPK-5 proven =="
