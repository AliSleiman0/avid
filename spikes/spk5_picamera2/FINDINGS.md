# SPK-5 Findings — Picamera2 + UV + `--system-site-packages` actually works

> Deliverable for issue #43 / AVID-43 (PMP §6.4, ADR-008). **Status: PROVEN — complete.**

- **Date:** 2026-07-19
- **Hardware:** Raspberry Pi 4 Model B Rev 1.5 (2 GB), sensor **ov5647** (Pi Camera v1, 5 MP)
- **OS:** Debian 12 (bookworm), kernel `6.12.93+rpt-rpi-v8` aarch64
- **System Python:** CPython **3.11.2** (`/usr/bin/python3`) — the ADR-008 target
- **uv:** 0.11.29 (`aarch64-unknown-linux-gnu`)
- **apt packages:** `python3-picamera2` **0.3.31-1**, `python3-libcamera`
  **0.5.2+rpt20250903-1**, `python3-numpy` 1.24.2

## The question (ADR-008 bets the camera story on this)

Does `uv venv --system-site-packages` on the Pi pick up the apt-installed
`python3-picamera2` (+ `libcamera`) so it imports under the Pi's **system Python
3.11**? M2's whole camera path depends on the answer being yes.

## TL;DR — **YES**, with one load-bearing caveat

- `picamera2` **and** `libcamera` import cleanly inside a `uv venv
  --system-site-packages` built on system CPython 3.11.2, resolving from the apt
  path `/usr/lib/python3/dist-packages/`.
- End-to-end works, not just the import: enumerated the camera
  (`Picamera2.global_camera_info()` → `ov5647`) and **captured a real frame**
  (`(480, 640, 3)` `uint8` numpy array) through the venv.
- **Caveat (the one thing M2 / deploy must not get wrong):** the venv MUST be
  created against the **system** interpreter — `uv venv --python /usr/bin/python3`.
  If uv is allowed to pick, it downloads a **uv-managed** Python (3.13), whose
  site-packages does **not** contain the apt `picamera2`, and the import fails.
  `--system-site-packages` only exposes apt packages when the venv's base
  interpreter is the system one. Pin both flags together, always.
- `requires-python = ">=3.11"` holds — no change needed (`pyproject.toml:6`).

## Exact commands (reproducible; see `check.sh`)

```bash
# 1. system camera stack from apt (NOT pip — ADR-008)
sudo apt-get install -y python3-picamera2          # pulls python3-libcamera, numpy, Qt

# 2. uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 3. the project venv — BOTH flags are load-bearing
uv venv --python /usr/bin/python3 --system-site-packages

# 4. proof
.venv/bin/python -c "import picamera2, libcamera; print(picamera2.__file__)"
#   -> /usr/lib/python3/dist-packages/picamera2/__init__.py
```

### Observed output

```
Using CPython 3.11.2 interpreter at: /usr/bin/python3
Creating virtual environment at: .venv

# interpreter the venv uses:
/home/alisleiman0/spk5/.venv/bin/python
3.11.2 (main, Apr  8 2026, 01:58:00) [GCC 12.2.0]

# resolution paths (apt dist-packages, exposed by --system-site-packages):
/usr/lib/python3/dist-packages/picamera2/__init__.py
/usr/lib/python3/dist-packages/libcamera/__init__.py
/usr/lib/python3/dist-packages/numpy/__init__.py   # numpy 1.24.2

# enumeration + capture through the venv:
global_camera_info: [{'Model': 'ov5647', 'Location': 2, 'Rotation': 0,
                      'Id': '/base/soc/i2c0mux/i2c@1/ov5647@36', 'Num': 0}]
captured frame shape: (480, 640, 3) dtype: uint8
```

`picamera2.__version__` does not exist — use `importlib.metadata.version("picamera2")`
(→ `0.3.31`). Note `libcamera` ships **no** pip dist-metadata, so
`importlib.metadata.version("libcamera")` raises `PackageNotFoundError` even though
the import works — don't gate any health check on package metadata for the system
libcamera; check the import (or `libcamera` version string from the manager log
`libcamera v0.5.2+99-...`) instead.

## Deviations from ADR-008's assumption → follow-ups before M2

1. **uv-managed Python defeats `--system-site-packages`.** The ADR text should
   read the flag pairing as mandatory: `uv venv --python /usr/bin/python3
   --system-site-packages`. This is exactly what the M2 camera-adapter venv and
   the deploy story (#39) must provision — otherwise the adapter imports a 3.13
   with no `picamera2`. **Action:** bake the `--python /usr/bin/python3` flag into
   the deploy/venv-provisioning step (#39) and the M2 adapter's setup docs.
2. **CSI camera is not hot-pluggable.** Attaching the ribbon to a running Pi does
   not detect; a reboot is required (`camera_auto_detect=1` in
   `/boot/firmware/config.txt` then does the rest). Fieldwork note for M2 bring-up.
3. `python3-picamera2` pulls a large tree (libcamera, numpy, Qt/GTK). ~acceptable
   on the 2 GB Pi 4 (idle RAM 150 MB / 1.8 GB). No headless-slimming needed at M2,
   but the Qt deps are dead weight for a headless adapter — optional cleanup later.

## Confirms for M2 (SDS §3.11.2)

- `picamera2` stays an **apt/optional** dependency, imported **only** inside
  `RealPicamera2Camera` (P1/P5). The pip `[project.optional-dependencies] pi`
  entry stays empty (`pyproject.toml:22`) — correct, no change.
- The `Camera` port's real adapter is viable on this exact stack. M2 can proceed
  on the ADR-008 assumption — it holds.
