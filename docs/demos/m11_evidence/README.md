# M11 soak evidence — ⛔ WINDOW STOPPED 2026-08-23, VOID as O5 evidence

> **This window ran ~26 hours of a planned 30 days and was stopped deliberately.** It is **not**
> evidence for O5. The robot was wedged in `THINKING` from minute 3 (#452), so the window measured
> a catatonic robot at 99.89% uptime with every graded criterion passing.
>
> O5 is amended (#389): the 30-day **hardware** soak moves to the production board, because the
> board under test here is an MVP prototype and the board-specific half of a soak — thermals, SD
> wear, brownout, the no-RTC clock — is evidence about hardware nobody will own. The
> **software-endurance** half stays, as a 72-hour run, once #452 is fixed and the harness has a
> liveness criterion and a thermal series.
>
> ⚠️ Everything below is kept because the *findings* were real and front-loaded. The numbers are
> not.

`window.json` is the **window-start epoch**, written the moment the clock started.

It is committed for one reason: an epoch held only on the Pi is one SD-card failure away from
making thirty days of data ungradeable. `--since` is not recoverable by inspection afterwards —
the sampler's earliest row tells you when *sampling* began, not what window was claimed.

## Opened — and stopped

**2026-08-22T13:18:08Z** (epoch `1787404688`). Planned close **2026-09-21T13:18:08Z**;
⛔ **actually stopped 2026-08-23T15:18Z, ~26 hours in.** 1560 samples.

Build under test: **`v0.M10.0-47-g8d03690`**.

⚠️ An earlier window opened at 12:50:29Z and was **deliberately restarted 28 minutes in**, to test
that the soak survives a reboot and to land the intervention log before the window rather than
during it. Recorded in `window.json` rather than quietly overwritten.

## ⚠️ Do not deploy to the Pi during a window — HISTORICAL, this window is closed

§12.6's first bullet: *a window whose build identifier changes mid-flight is not graded as one
window.* That guard is real now — `build` became the deployed commit at #388, and #410's tests
prove the criterion fires — so a `git pull && systemctl restart robot` on the rig **will** split
this window and the split **will** be reported.

Working on `main` is fine. Deploying to `/opt/avid` is not, while a window is open.

⛔ **This window is closed, so the freeze is lifted.** Kept because it applies again to the next
one — and because the deploy freeze is what made this window expensive: it blocked the fix for the
very defect that made the window meaningless.

If something must be deployed, that is a decision to restart the window, and it should be recorded
here as such rather than absorbed.

## 🔴 Read this before quoting any figure here — the robot was wedged (#452)

Found 2026-08-23, one day in. **The robot entered `THINKING` at minute 3 of this window and never
left.** Three state transitions in 24 hours; 147 of its 175 log lines are `ignored illegal
transition ... in state THINKING`. A spurious `audio.speech_started` in an empty room drove it
there, the Realtime session open timed out, and the 10-second `THINK_TIMEOUT` that exists for
exactly this never armed — the arming call lives in the turn path the failed open aborted.

⚠️ **The uptime arithmetic is not wrong; what it describes is not what O5 means.** The *process* is
genuinely healthy — heartbeating, RSS flat, zero bus drops, watchdog satisfied — so AC-2's **99.89%
is true and is not evidence for O5.** A thirty-day window that cannot distinguish a working robot
from a catatonic one is not a soak.

⚠️ And `soak_pi.py` **had no liveness criterion** — nothing here asserted the robot ever changed
state, took a turn or moved. That is the M8 lesson verbatim, and it was #452 AC-4.

> ✅ **Closed.** `soak_pi.py` now grades a **`LIVE`** criterion (SDS §12.6): it records `GET /state`
> and `/metrics`' `transitions` counter per sample, and **fails** the run when a transient state is
> *provably* held past the bound the design states for it — THINKING against `[gate]
> think_timeout_s`, read from config. Provably is the load-bearing word: identical state readings
> 60 s apart are equally consistent with a wedged robot and a conversing one, so a run counts only
> when the transitions counter stood still across all of it. A window with no state series, or no
> counter, is **INCONCLUSIVE and never a pass** — a clean liveness figure from an instrument that
> was not there would be this window all over again.
>
> Run against *this* window's database it would report `INCONCLUSIVE`, not `FAIL`: the build under
> test predates the columns, so nothing here was recorded. That is the honest answer. **The wedge
> is evidenced above, by the journal, not by a criterion that could not see it.**

## Grading it

```sh
cd /opt/avid && sudo /opt/avid/.venv/bin/python /opt/avid/docs/demos/soak_pi.py \
    --mode grade --since 1787404688 --config /etc/robot/config.toml
```

⚠️ **The `cd` is load-bearing, not tidiness.** `[ai] personality` is a *relative* path, and
SDS §6.5 resolves relative paths against the **process working directory** — which for
`robot.service` is `WorkingDirectory=/opt/avid`. Run this from anywhere else and `load_config`
raises `FileNotFoundError` before a single criterion is graded. The command recorded here from
2026-08-22 until 2026-08-23 omitted it, and **could not run as written**.

Exit code is non-zero on **fail or inconclusive** — inconclusive is not a pass, and most often
means the sampler itself has holes, which makes every other number a statement about a smaller
window than the one claimed.

## What a power cut costs you — measured, not guessed

The Pi was rebooted before this window opened, to find out:

| | |
|---|---|
| both units come back unattended | ✅ `robot` and `soak-sampler` are `enabled` |
| `samples.db` survives | ✅ persisted across the reboot, sampler resumed writing to the same file |
| the window epoch survives | ✅ it is this file |

**But it is not free, and which criterion it costs depends on how the power went:**

- **A graceful `reboot`** records `stop_reason=signal` — a **clean** stop, which §12.6 defines as a
  **manual restart**. That fails **AC-3**, and AC-3 is part of O5.
- **A power cut** leaves `stopped_at` NULL — an unclean stop, indistinguishable from a crash. That
  fails **AC-3b**, which is *not* part of O5's definition but is reported and makes the exit
  non-zero.
- Either way the outage counts against uptime (7h12m of slack at the 99% bar over 30 days) and
  against AC-0 coverage, since the sampler is down too.

**If it happens, write it down** — `/var/lib/soak/interventions.jsonl`, one JSON object per line:

```json
{"at": 1787404800, "kind": "power_cut", "note": "unplugged the bench strip"}
```

⚠️ That note **explains** an event; it does not **excuse** one. AC-3 and AC-3b keep their verdicts.
It exists because a power cut and a crash leave byte-identical records and journald is volatile
(#381), so thirty days from now nothing else will remember which was which.

### ⚠️ It stopped being hypothetical on day 1

**It happened, 22.5 minutes into this window** — and the log above was empty when it did. The
machine rebooted; boot `c7c6c3d5` left `stopped_at` NULL, so **AC-3b fails for the whole
window**. It is *not* the deliberate reboot: that one is earlier and clean. **#439** carries the
full diagnosis; the evidence trail is in `window.json` under `_unplanned_stop_2026_08_22`.

The cause is now **established by elimination** — a hard power loss or hardware reset; `wtmp` has
no `shutdown` record for that boot while every other reboot in the machine's history does, and
every software route (panic, watchdogs, OOM, timers) is independently excluded. **Why** power was
lost is still unknown. Full evidence in #439 and in `window.json`.

⚠️ But note *how* that was recovered: from `wtmp`, `boot_log.started_mono` and sampler **rowids** —
never from a note, because nobody wrote one. Reconstruction happened to be possible here and will
not always be. **Write the note at the time.**

## What is being measured

O5: **≥99% uptime, zero manual restarts, over 30 days.** Uptime comes from the robot's own
`boot_log` (#379); the sampler polls `/health` and `/metrics` from **outside** the robot and writes
to its **own** database, so a robot whose storage is the problem still gets measured.

Memory is **recorded, not graded** (#404) — `rss_bytes` and `mem_available_bytes` per sample. A
thirty-day window is the only instrument this project has that can find a slow leak, and on a 2 GB
board that is the failure mode rather than a curiosity.
