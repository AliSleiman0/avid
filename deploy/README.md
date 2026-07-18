# Deploy — Pico on the Raspberry Pi

`robot.service` makes the Pi "boot to app": systemd starts the process on power-on,
and restarts it if it crashes **or wedges** (`Type=notify` + `sd_notify` watchdog,
paired with the notifier port from AVID-38). At M1 every device adapter is fake and
the Realtime session is `replay`, so this runs fully offline with **no API key**.

- Unit: `deploy/robot.service` (this directory).
- Config it points at: `/etc/robot/config.toml` (a copy of `config/pi.toml`).
- Logs: **journald, not the SD card** (SDS §3.12.2) — see [Operate](#operate).

The service runs as a dedicated, unprivileged, no-login `robot` user. The checkout
in `/opt/avid` stays **owned by your admin user and world-readable**; `robot` only
needs read + execute. Runtime writes go to `/var/lib/robot` (created by systemd via
`StateDirectory=`), so the code tree is read-only at runtime (`ProtectSystem=strict`).

## Prerequisites

- Raspberry Pi OS (Debian 12 "bookworm"), system Python **3.11** (`requires-python
  >=3.11`; the Pi deliberately runs system Python, not a uv-managed interpreter —
  ADR-008).
- [`uv`](https://docs.astral.sh/uv/) on the admin user's `PATH` (`~/.local/bin`).
- `git` credentials for the repo on the admin user.

## Provision

Run as your admin user (the one with `git`/`uv`); `sudo` where shown.

```sh
# 1. Dedicated, unprivileged, no-login service user
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent robot

# 2. Checkout — owned by you, world-readable (robot only reads/executes)
sudo mkdir -p /opt/avid && sudo chown "$USER:$USER" /opt/avid
git clone https://github.com/AliSleiman0/avid.git /opt/avid

# 3. Virtualenv on SYSTEM Python 3.11.
#    --python /usr/bin/python3 is load-bearing: without it uv fetches a managed
#    3.13 whose site-packages can't see the apt picamera2 stack (SPK-5 / ADR-008).
#    --system-site-packages is harmless now (all-fake) and required at M2 for
#    picamera2, so it's baked in from the start.
cd /opt/avid
uv venv --python /usr/bin/python3 --system-site-packages
uv pip install -e .                         # avid + pydantic into .venv
/opt/avid/.venv/bin/python -m avid --help   # smoke check: exits 0

# 4. Config systemd points at — a copy of config/pi.toml (no secrets in it)
sudo mkdir -p /etc/robot
sudo cp /opt/avid/config/pi.toml /etc/robot/config.toml
sudo chmod 0644 /etc/robot/config.toml

# 5. Install and verify the unit
sudo cp /opt/avid/deploy/robot.service /etc/systemd/system/robot.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/robot.service   # must be clean

# 6. Start it, supervised, and enable on boot
sudo systemctl enable --now robot
systemctl status robot                       # expect: active (running)
```

Because `Type=notify`, systemd only reports **`active (running)`** once the app has
sent `READY=1`, which the lifecycle does on reaching `IDLE`. So a green
`systemctl status` *is* the "reached IDLE" signal. Corroborate the loopback health
server:

```sh
curl -s http://127.0.0.1:8787/health        # -> ok
```

## Verify the watchdog restarts it

```sh
PID=$(systemctl show robot -p MainPID --value)

# Crash → Restart=always brings it back within RestartSec (~5s)
sudo kill -9 "$PID"
systemctl status robot                       # new MainPID, active (running)

# Wedge → WatchdogSec fires because WATCHDOG=1 stops arriving (~30s), then restart
PID=$(systemctl show robot -p MainPID --value)
sudo kill -STOP "$PID"
journalctl -u robot -f                       # "watchdog timeout" → killed → restarted
```

## Operate

```sh
journalctl -u robot -f                       # follow logs (journald, not the card)
sudo systemctl restart robot
sudo systemctl stop robot
systemctl show robot -p WatchdogUSec         # 30s — half of watchdog_interval_s
```

## Update

```sh
git -C /opt/avid pull                        # you own the tree, no sudo needed
cd /opt/avid && uv pip install -e .          # only if dependencies changed
sudo systemctl restart robot
```

## M2 readiness (not needed at M1)

- **API key**: create `/etc/robot/robot.env`, `root:root`, `chmod 600`, containing
  `OPENAI_API_KEY=…`. It is already wired via `EnvironmentFile=-` in the unit — the
  key never appears in the unit or in `config.toml` (P7 / SECURITY.md).
- **Camera / servo**: add `SupplementaryGroups=video gpio` to the unit and flip
  `camera = "picamera2"` (etc.) in `config.toml`. The venv already carries
  `--system-site-packages`, so the apt `python3-picamera2` is visible.
- **Tighter sandbox**: `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute`,
  and `IPAddressAllow=localhost` are deliberately deferred — validate they don't
  break sd_notify/asyncio before adding them.
