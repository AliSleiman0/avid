# M11 — the 30-day soak window

`window.json` is the **window-start epoch**, written the moment the clock started.

It is committed for one reason: an epoch held only on the Pi is one SD-card failure away from
making thirty days of data ungradeable. `--since` is not recoverable by inspection afterwards —
the sampler's earliest row tells you when *sampling* began, not what window was claimed.

## Opened

**2026-08-22T12:50:29Z** (epoch `1787403029`), closing **2026-09-21T12:50:29Z**.

Build under test: **`v0.M10.0-45-g2bcb39e`**.

## ⚠️ Do not deploy to the Pi during the window

§12.6's first bullet: *a window whose build identifier changes mid-flight is not graded as one
window.* That guard is real now — `build` became the deployed commit at #388, and #410's tests
prove the criterion fires — so a `git pull && systemctl restart robot` on the rig **will** split
this window and the split **will** be reported.

Working on `main` is fine. Deploying to `/opt/avid` is not, until 2026-09-21.

If something must be deployed, that is a decision to restart the window, and it should be recorded
here as such rather than absorbed.

## Grading it

```sh
sudo /opt/avid/.venv/bin/python /opt/avid/docs/demos/soak_pi.py \
    --mode grade --since 1787403029 --config /etc/robot/config.toml
```

Exit code is non-zero on **fail or inconclusive** — inconclusive is not a pass, and most often
means the sampler itself has holes, which makes every other number a statement about a smaller
window than the one claimed.

## What is being measured

O5: **≥99% uptime, zero manual restarts, over 30 days.** Uptime comes from the robot's own
`boot_log` (#379); the sampler polls `/health` and `/metrics` from **outside** the robot and writes
to its **own** database, so a robot whose storage is the problem still gets measured.

Memory is **recorded, not graded** (#404) — `rss_bytes` and `mem_available_bytes` per sample. A
thirty-day window is the only instrument this project has that can find a slow leak, and on a 2 GB
board that is the failure mode rather than a curiosity.
