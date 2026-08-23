# Runbook — the robot is misbehaving right now

> **Audience: you, at 23:00, with a robot that has stopped talking.** Not you with the design in
> your head. Entries are named by what you *observe*, because when you open this you do not yet
> know which subsystem it is — that is the problem you have.
>
> This is **not** a bring-up guide. Reaching the Pi, provisioning it, rebuilding its venv and every
> hardware trap the sealed gates paid for live in [`PI_OPERATIONS.md`](PI_OPERATIONS.md); the
> per-milestone gate commands live in [`README.md`](README.md). This file **links** to them and
> deliberately does not copy them: a procedure duplicated here is a copy that will rot, which is
> the exact failure `PI_OPERATIONS.md` exists to teach.

⏱️ **The M11 soak is running (2026-08-22 → 2026-09-21).** Every diagnostic in §1 is read-only and
safe. Everything in §4 is an intervention that costs the window something. Read
[§4.0](#40-before-you-intervene-during-the-soak) before you touch anything.

---

## 0. How to use this

1. **[§1 First five minutes](#1-the-first-five-minutes)** — run these *before* forming a theory.
   Most of the time one of them names the fault outright.
2. **[§2 Symptom index](#2-symptom-index)** — find the line that matches what you saw.
3. **[§3 The entries](#3-the-entries)** — each one is **Confirm** (a command and the output that
   proves it), **Fix** (the smallest correct action), and **Not to be confused with** (the
   look-alike, and the single thing that tells them apart).

That third part is where the value is. Nearly every fault in this project's history had a twin that
looked identical from the couch: *mute because the venv lost its OpenAI extra* and *mute because the
amp is dead* are the same silence.

---

## 1. The first five minutes

**Rule for all five: read what the machine has, never what the repo says.** The repo is a claim; the
machine is the evidence. Ignoring that cost this project #310, #265 and #264 — three live-behaviour
defects investigated as model or prompt problems before anyone asked what had actually been running.

### 1.1 Is it even running, and is it meant to be

```sh
systemctl is-active  robot soak-sampler
systemctl is-enabled robot soak-sampler
```

`active` and `enabled` are different questions. `active` but not `enabled` means the next reboot
comes up without a robot; `enabled` but not `active` means something stopped it and
`Restart=always` gave up, which is nearly impossible by design (`StartLimitIntervalSec=0`) and
therefore interesting.

### 1.2 What is actually running — the startup banner

```sh
journalctl -u robot -b | grep -m1 'build='
```

One line, and it settles most of §3 before you read any of it:

| field | what it decides |
|---|---|
| `build=` | which commit is running. **`-dirty` means someone edited files on the machine.** `0.0.0` on a checkout means the resolver has regressed and §12.6's split-window guard is inert again |
| `config=` | which file was loaded — usually `/etc/robot/config.toml`, which is a **copy** of `config/pi.toml` and rots |
| `realtime_model=` / `voice=` | ⚠️ if this names a *mini* model you did not choose, a key is **missing** from the deployed config and the schema default won (F-9) |
| `openai_key=` | `present` or `absent`. `absent` and mute are the same symptom, which is why this field exists |
| `adapters=` | `speaker=alsa` or `speaker=fake` — a robot wired to fakes is silent and healthy |

### 1.3 The control API

```sh
curl -s 127.0.0.1:8787/health                                  # -> ok
curl -s 127.0.0.1:8787/metrics | python3 -m json.tool
```

`/health` answering **at all** is the liveness proof; it is deliberately dumb. In `/metrics`, read
`uptime_s`, `rss_bytes`, `mem_available_bytes`, `turns`, `transitions`, `bus_queues`,
`triggers_fired`, `cost_usd`, `build`.

⚠️ **Read the `absent` list first.** A provider that could not be read is named there instead of
being reported as a number. *Absent is not zero* — a `0` from an instrument that is not running
reads exactly like a real zero, and this project has already shipped that bug once.

### 1.3b Watch it happen, live

```sh
curl -s 127.0.0.1:8787/state | python3 -m json.tool
curl -N 127.0.0.1:8787/events/stream
```

`/state` reports the operational `state`, the `affect` and whether a `session` is open — **three
independent readings** (SDS §3.10 makes state and affect orthogonal, so do not infer one from the
other), plus an `absent` list for anything it could not read.

`/events/stream` is the live event feed (#385). It is the fastest way to answer *"is anything
happening at all"* on a robot that looks stuck: talk to it and watch. A line beginning `: dropped`
means **your client** fell behind, not the robot.

`/facts` is §7.10's privacy audit — *what do you know about me?* — and it answers from the same
store the robot writes to:

```sh
curl -s '127.0.0.1:8787/facts' | python3 -m json.tool
curl -s '127.0.0.1:8787/facts?include_superseded=1' | python3 -m json.tool
```

Without the flag you get the **live** facts; with it, the full history including the rows
supersession retired. The response echoes `include_superseded` back, so a short list can be told
from a filtered one. ⚠️ **A forgotten fact is in neither view** — `forget` is a hard cascading
DELETE, so it is absent because it is gone, not because it is hidden.

⚠️ **Every route SDS §9.5 specifies now exists.** If a curl to one of them 404s, that is a routing
bug or the wrong port, not an unbuilt feature — which was the right first guess until #386 and is
now the wrong one.

### 1.4 The uptime record, which outlives the logs

```sh
sudo /opt/avid/.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/var/lib/robot/robot.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT boot_id, build, started_at, last_seen_at, stopped_at, stop_reason"
                   " FROM boot_log ORDER BY started_at DESC LIMIT 10"):
    print(dict(r))
PY
```

`stopped_at` NULL means that run ended **without the ordered teardown** — a crash, a watchdog kill
or a power cut. A row with a `stop_reason` is a *clean* stop, which means a person or a deploy asked
for it.

⚠️ **This lives in SQLite and not in the journal on purpose.** journald here is `Storage=volatile`
([`journald-avid.conf`](journald-avid.conf)) — **a crash's last words are gone after the reboot that
follows it.** So if you are about to reboot, capture the journal *first*:
`journalctl -u robot -b > /tmp/before-reboot.txt`.

### 1.5 A count of restarts, which is the thing nothing watches

```sh
systemctl show robot -p NRestarts
```

Nothing in the system alarms on restart *count*. `Restart=always` once faithfully restarted a robot
that could never work **942 times**, and the only evidence was a journal nobody was reading (F-6).
That is a deliberate trade — see
[the service keeps restarting](#the-service-keeps-restarting).

---

## 2. Symptom index

| What you observed | Go to |
|---|---|
| It says nothing at all, ever | [The robot does not speak at all](#the-robot-does-not-speak-at-all) |
| I talk to it and nothing happens | [The robot never answers me](#the-robot-never-answers-me) |
| It spoke in the middle of the night, or hours after it should have | [The robot answers at the wrong time](#the-robot-answers-at-the-wrong-time) |
| The face keeps flashing the boot screen; it cycles | [The service keeps restarting](#the-service-keeps-restarting) |
| systemctl says active and the robot is inert | [The service is active but nothing happens](#the-service-is-active-but-nothing-happens) |
| It will not come up at all, and says why | [The robot will not start at all](#the-robot-will-not-start-at-all) |
| It works, but the voice, the memory or the answers are wrong | [It talks but it behaves subtly wrong](#it-talks-but-it-behaves-subtly-wrong) |
| It never greets me; it thinks the room is empty | [The robot never notices me](#the-robot-never-notices-me) |
| It talks but the head does not move | [The robot does not move](#the-robot-does-not-move) |
| Speech is clipped, or the log warns about dropped audio | [Audio is choppy or the log says audio was dropped](#audio-is-choppy-or-the-log-says-audio-was-dropped) |
| The face is stuck on one expression, or the panel is black | [The face is frozen or the screen is blank](#the-face-is-frozen-or-the-screen-is-blank) |
| Writes fail; the DB errors; the card is full | [The disk is filling up](#the-disk-is-filling-up) |
| **It works when I run it by hand** | [It works by hand and not under systemd](#it-works-by-hand-and-not-under-systemd) |

---

## 3. The entries

### The robot does not speak at all

**Confirm.** In this order, because each check is cheaper than the next:

```sh
journalctl -u robot -b | grep -m1 'build='          # openai_key= and adapters=speaker=
sudo -u robot /opt/avid/.venv/bin/python -c "import openai; print(openai.__version__)"
sudo -u robot speaker-test -D default -c 1 -t sine -f 440 -l 1
```

**Fix.** Three different faults, in the order those commands separate them:

- `openai_key=absent` → the key is missing from `/etc/robot/robot.env` (root-owned, `0600`; never in
  the unit and never in `config.toml` — [`../SECURITY.md`](../SECURITY.md)).
- the `import openai` line fails → the venv was rebuilt **without `--extra openai`**. Rebuild it per
  [`PI_OPERATIONS.md` §2](PI_OPERATIONS.md#2-the-venv--three-ways-to-break-it), naming every
  extra.
- `speaker-test` is silent → hardware or ALSA; [`PI_OPERATIONS.md` §5](PI_OPERATIONS.md#5-audio).

**Not to be confused with** [the robot never answers me](#the-robot-never-answers-me) — that one is
deaf, not mute, and one line separates them: if `journalctl -u robot -f` shows `conversation.*`
events while you speak, it heard you and the fault is downstream. No events at all means it never
heard you.

⚠️ Nor with `adapters=speaker=fake`: a robot wired to the fake speaker is working perfectly and
writing frames and WAVs nobody plays. The banner is the only thing that will tell you.

### The robot never answers me

**Confirm.**

```sh
amixer -c Device sget "Auto Gain Control"    # want: [off]
amixer -c Device sget Mic                    # want: 10/16, +14.88 dB, [on]
journalctl -u robot -b | grep -i 'agc\|ALSAAudioError\|No such device'
```

**Fix.** In descending order of how often it has actually been this:

- **AGC on** — the trap that cost a milestone. It amplifies a *quiet* room until the capture path's
  own noise floor reads as speech: an empty room measured **−16.9 dBFS with AGC on** against −36.4
  off, and Silero called **20% of that empty room speech**. `AlsaMicrophone` logs an ERROR at
  capture-open when it finds AGC enabled and deliberately does not refuse to start, so **that log
  line is the whole of the warning**. Full numbers in
  [`PI_OPERATIONS.md` §5.1](PI_OPERATIONS.md#-51-auto-gain-control--the-trap-that-cost-a-milestone-avid-296).
- **The wrong capture device** — `[microphone] device` missing from the deployed config falls back
  to `"default"`, which opens the *amp*, not the USB mic. F-9 again.
- **ONNX starving the loop** — Silero at 306% CPU makes the robot deaf while every log line looks
  healthy. Check with `top -H`; the guard is `tests/adapters/test_onnx_session_options.py`.

**Not to be confused with** a genuinely dead mic. AGC-on and a dead mic are opposites that present
identically as "it does not respond": AGC-on produces **too many** sessions in an empty room
(nothing to answer, degrade, reconnect, repeat) and a dead mic produces none. Leave it alone in an
empty room for two minutes and count `conversation.*` — a fault that fires when nobody is speaking
is AGC, not deafness.

⚠️ Do not compensate with `Mic` gain. Gain is linear; AGC is a feedback loop that raises the floor
*precisely when the room is quiet*, which is the condition the session gate exists to act on.

### The robot answers at the wrong time

**Confirm.**

```sh
timedatectl                                  # synchronised? and did it step?
journalctl -u robot -b | grep -i 'clock\|proactive'
```

Then read `boot_log.started_at` against `started_mono`
([§1.4](#14-the-uptime-record-which-outlives-the-logs)) — the pair exists so a clock **step** is
detectable after the fact rather than inferred from a scheduler that overslept.

**Fix.** **The Pi has no RTC.** An offline boot restores a stale clock and NTP steps it forward
later; a delay computed across that step once slept through its own booking by ~34,700 s (F-7). If
the robot has just booted without a network, wait for NTP before believing any wall-clock behaviour.
Durations in the code use `monotonic_ns` and wall clock is for humans (SDS §9.1.1) — if you find a
new place subtracting `timestamp_ms`, that is the bug.

**Not to be confused with** a booking missed while the robot was *off* (F-8), which fires **late but
correct**, once — or with quiet hours, which fires **not at all**. Check the deployed window rather
than the repo's:

```sh
grep -n quiet /etc/robot/config.toml
```

⚠️ The machine's quiet window is **deliberately different** from the repo's
(see [§5](#5-what-not-to-do)). A mismatch there is not the bug.

### The service keeps restarting

**Confirm.**

```sh
systemctl show robot -p NRestarts
journalctl -u robot -b --no-pager | tail -60          # the exception, before it rotates away
```

**Fix.** Read the traceback and fix *that*. The classic is `PermissionError: /dev/fb0` from a unit
missing its `SupplementaryGroups`, which produced F-6's 942 restarts. Reinstall the unit from the
repo ([§4.2](#42-reinstall-the-units-and-config-from-the-repo)) rather than patching the machine.

⚠️ `StartLimitIntervalSec=0` is deliberate and stays: a rate limiter that gave up would turn a
*transient* failure — a wet boot, a slow USB enumeration, a network stack not yet up — into a robot
that is off until a human notices, which is worse for a companion than restarting forever. **The
answer to a crash loop is not to stop restarting; it is to notice.**

**Not to be confused with** a watchdog **wedge**, which also restarts forever and is a different
fault. The cadence separates them: a crash loop cycles at `RestartSec=5` with a traceback each time;
a wedge cycles at roughly `WatchdogSec` with systemd reporting a watchdog timeout and **no
application exception at all**. A wedge means the loop stopped pinging — look for blocking I/O on
the event loop (P8), not for a bug in startup.

### The service is active but nothing happens

**Confirm.**

```sh
curl -s 127.0.0.1:8787/health                        # ok? then the loop is alive
journalctl -u robot -b | grep -m1 'build='           # adapters=...
```

**Fix.** `Type=notify` means `active (running)` is claimed only after the app sends `READY=1`, so
this is a robot that booted **successfully into the wrong configuration** far more often than it is
a hang. Nearly always the banner's `adapters=` line: `realtime=replay` replays a recorded session,
and every device set to `fake` is a complete, healthy robot that touches no hardware.
`config/pi.toml` ships every device `fake` **on purpose** — selecting real hardware is a
provisioning act — so a machine restored from the repo template is exactly this symptom.

**Not to be confused with** a genuine wedge. `/health` is answered from the event loop, so a reply
of `ok` proves the loop is not blocked; a wedged robot does not answer and systemd kills it within
`WatchdogSec`. If `/health` answers and nothing happens, stop looking for a hang.

### The robot will not start at all

**Confirm.**

```sh
journalctl -u robot -b --no-pager | tail -40
sudo /opt/avid/.venv/bin/python -c \
  "from avid.core.config import load_config; load_config('/etc/robot/config.toml')"
```

**Fix.** Two failures are *designed* to stop the robot, and both say so loudly:

- `Extra inputs are not permitted` — SDS §9.6's sections are `extra="forbid"`, so a machine carrying
  an old section (a pre-#200 flat servo, say) fails hard rather than running half a rig. Splice the
  stale sections from the freshly-pulled template — **do not copy `config/pi.toml` wholesale**, that
  erases provisioning flips which exist nowhere else. Recipe in
  [`PI_OPERATIONS.md` §3](PI_OPERATIONS.md#-syncing-the-pi-to-main-will-refuse-to-start-on-the-200-schema--and-that-is-correct).
- A bad API key at boot (F-11) — the one permitted hard stop (SDS §3.12.3).

**Not to be confused with** the far more dangerous **opposite**: a config that is *missing* a key
does **not** fail. It silently adopts a schema default, and `[ai] model`'s default is the **mini**
while the Pi profile has pinned the flagship since 2026-08-01 (F-9). A refusal to start is the
*good* outcome — you are being told. The bad one is
[it talks but it behaves subtly wrong](#it-talks-but-it-behaves-subtly-wrong).

### It talks but it behaves subtly wrong

**Confirm.** The banner, then a real diff — never an eyeball:

```sh
journalctl -u robot -b | grep -m1 'build='
diff <(ssh alisleiman0@AVID cat /etc/robot/config.toml) config/pi.toml
```

**Fix.** This is **F-9, the row this project underestimated for longest**: a deployed config missing
a key produces *silently wrong behaviour, not an error*. Reinstall or splice from the repo
([§4.2](#42-reinstall-the-units-and-config-from-the-repo)) and re-apply the provisioning flips.

⚠️ Before investigating a model or a prompt, **ask what was running.** Three live-behaviour defects
(#310, #265, #264) were investigated as model problems first. The banner is one `grep`.

**Not to be confused with** a genuine model or prompt regression. The discriminator is the banner's
`realtime_model=` / `voice=` / `personality=` against what you believe you deployed: if they differ
you have a deployment defect and nothing about the model is yet in question. If they match, *then*
it is a behaviour question — and `build=` names the commit to reproduce it against.

### The robot never notices me

**Confirm.**

```sh
grep -n 'detector_scale\|confidence_threshold\|lose_window_s\|fps' /etc/robot/config.toml
journalctl -u robot -b | grep -i 'vision\|face'
```

**Fix.**

- ⚠️ **`detector_scale = 2` is blind — do not restore it.** Against a well-framed person at 93×116 px
  it detected them in **0 of ~75 frames**, never clearing even the adapter's own 0.3 floor. Scale 1
  on the identical frames: 84%.
- Thresholds tuned for a *posing* person will not see a *working* one: at `0.6` only 32.6% of frames
  qualify while someone is sitting there, and the longest stretch with no qualifying frame is
  **116 s** — looking down at a keyboard is not absence. The shipped values are `0.35` and `75 s`.
- The wrong model file: only the `2026may` YuNet export accepts the rig's 640×480; the `2023mar`
  files beside it are statically shaped and reject it
  ([`PI_OPERATIONS.md` §5a](PI_OPERATIONS.md#5a-vision-m8)).

**Not to be confused with** a camera delivering no frames at all — both present as "it never greets
me". If the log shows detections at low confidence the camera works and this is a tuning or scale
fault; if there are no frames it is the camera, and a near-black frame is usually the lens cap or
the room rather than the code. ⚠️ Note the empty-room false-positive rate barely moves across
thresholds — detection separates occupied from empty by roughly 600:1 everywhere — so **raising the
threshold "to be safe" costs true positives and buys almost nothing.**

### The robot does not move

**Confirm.**

```sh
sudo -u robot ls -l /dev/i2c-1                       # can the SERVICE user see it?
id robot                                             # want: video gpio audio i2c
journalctl -u robot -b | grep -i 'servo\|i2c\|lgd-nfy'
```

**Fix.** Both known causes are environment rather than logic, and both stayed invisible for as long
as every bench run was done as the login user:

- `robot` not in `i2c` → `/dev/i2c-1` is `root:i2c crw-rw----`, and the PCA9685 behind every servo
  command is an I²C device. Without the group the robot **cannot move at all under systemd** while
  every bench run drives it correctly.
- `FileNotFoundError: '.lgd-nfy-N'` → `lgpio` writes notification files into the process's *current
  working directory*, which is read-only under `ProtectSystem=strict`. `Environment=LG_WD` moves
  them somewhere writable.

Both are in [`robot.service`](robot.service) now, so the fix is to
[reinstall the unit](#42-reinstall-the-units-and-config-from-the-repo), not to hand-edit the
machine. The rig itself — channel map, the register read-back that proves a fault is downstream of
the chip, horn alignment, and why the reach limits are still provisional — is
[`PI_OPERATIONS.md` §5c](PI_OPERATIONS.md#5c-servos-and-motion-m9).

**Not to be confused with** a **power** fault, the third member of this family and identical from
the couch: the servos need their own 5–6 V rail, and a disconnected supply gives you a motionless
robot with a perfectly healthy I²C bus. The discriminator is the log — a permissions fault raises,
a power fault does not. **A servo write that returns cleanly and moves nothing is the rail.**

⚠️ Nor with a *reach* problem: if it moves but fights the linkage or stops short, that is
`min_deg`/`max_deg` on the axis, and it is fixed by reseating the horn — **never** by inverting a
sign in code ([§5](#5-what-not-to-do)).

### Audio is choppy or the log says audio was dropped

**Confirm.**

```sh
journalctl -u robot -b | grep -i 'dropped\|accepted .* of \|barge-in truncated'
```

**Fix.** Since #416 an interruption logs DEBUG `barge-in truncated the write`. **Five WARNINGs of
this shape in one conversation turned out to be barge-ins with no audio lost at all** (#414): the
writer resumed after `stop()` yielded, while the guard still read "live episode".

⚠️ If you see the **WARNING** wording on a current build, the #416 fix is not in the running build —
check `build=` — and #207's AC-8 pass should be reversed rather than explained.

⚠️ One outlier is still open: `accepted 140 of 150` at the **start** of a playback, with no
interruption anywhere near it. That one is a real short write, not a barge-in.

**Not to be confused with** an under-run caused by blocking the event loop, which also chops audio.
*Where* it starts separates them: a barge-in truncation is always at a boundary you created by
speaking, and a P8 stall is not correlated with your speech at all. If it chops when nobody is
talking, look for blocking I/O.

### The face is frozen or the screen is blank

**Confirm.**

```sh
sudo -u robot test -w /dev/fb0 && echo writable
journalctl -u robot -b | grep -i 'fb0\|display\|PermissionError'
sudo ~/bin/fbshot.py /tmp/face.png                   # what the panel WOULD show
```

**Fix.** `PermissionError: /dev/fb0` means the service user is not in `video` — reinstall the unit
([§4.2](#42-reinstall-the-units-and-config-from-the-repo)). If the framebuffer holds a correct face
and the glass is dark, the fault is the panel or its wiring and not the robot.

**Not to be confused with** *no panel attached*, which is the normal state of this rig: the 40-pin
header is occupied by the I²S amp and the I²C servo, so the ILI9486 **cannot be fitted while the
robot is otherwise wired**. ⚠️ `card0-SPI-1` reporting `connected` means *the overlay loaded*, never
*a screen is plugged in* — SPI has no hotplug detect. Read the face from `/dev/fb0` instead
([`PI_OPERATIONS.md` §4](PI_OPERATIONS.md#4-watching-the-panel-live-from-the-laptop)).

### The disk is filling up

**Confirm.**

```sh
df -h /
du -sh /var/lib/robot/*
journalctl --disk-usage
curl -s 127.0.0.1:8787/metrics | python3 -m json.tool | grep -i 'mem_available\|rss'
```

**Fix.** journald is capped in tmpfs and cannot be the cause *if the drop-in is installed* — verify
that it is, because it is a **copy on the machine** exactly like the other two
([`journald-avid.conf`](journald-avid.conf)). Otherwise the growth is `robot.db` (episodes are
retained 90 days by design) or the fake display's frame dumps under the service's state directory.

⚠️ **Disk exhaustion during the soak invalidates the run**, so treat a rising curve as urgent rather
than as housekeeping.

**Not to be confused with** a **memory** leak, which the soak reports separately and which shows as
`rss_bytes` climbing while `mem_available_bytes` falls — a leak is the one failure class thirty days
can find and fifteen minutes cannot. Disk is `df`; memory is `/metrics`. Two different problems with
the same "it got slow and then it died" ending.

### It works by hand and not under systemd

**This is not one bug; it is the shape five separate bugs took**, and it is the first hypothesis to
test whenever a bench run and the service disagree.

| instance | cause |
|---|---|
| deaf and mute for two milestones | `robot` not in `audio` — no sound card opened at all, while the process still reached IDLE and reported healthy |
| motionless under the unit (#413) | `robot` not in `i2c` |
| every servo write failing (#413) | `lgpio` writing `.lgd-nfy-N` into a read-only working directory — fixed by `LG_WD` |
| `git` refusing on the machine (#419) | `safe.directory` — the checkout is owned by the login user while the service runs as `robot` |
| the 5 V rail | not a permission at all, but the same "the bench had it, the service did not" shape |

**Confirm.** Reproduce as the service user, never as yourself:

```sh
sudo -u robot /opt/avid/.venv/bin/python -m avid --config /etc/robot/config.toml
id robot
```

**Fix.** Whatever the difference turns out to be, fix it **in [`robot.service`](robot.service) in
the repo** and reinstall. A hand-patched machine is the drift this whole document is written
against.

**Not to be confused with** an application bug — and the command above is the discriminator. If it
fails as `robot` and succeeds as you, no amount of reading application code will find it.
⚠️ **When something works by hand and not under systemd, suspect user, permission and environment
before logic.** That pattern hit four times in a single session.

⚠️ The related habit, and the reason it kept hitting: #418 was merged, CI-green, fully tested and
**inert in production**, because it was not verified on the Pi before merging.

---

## 4. Recovery procedures

Each says what it destroys. Read that line before running the command above it.

### 4.0 Before you intervene during the soak

⏱️ While the M11 window is open (it closes **2026-09-21T13:18:08Z**), every action in this section
costs the run something, and the costs are cumulative and unrecoverable:

| action | what it costs O5 |
|---|---|
| `systemctl restart robot` | fails **AC-3** (zero manual restarts) — a clean stop *is* a manual restart |
| a crash or a power cut | fails **AC-3b** (zero unplanned stops); the boot row's `stopped_at` is NULL |
| either of the above | uptime against the 99% bar, and AC-0's coverage figure |
| `git pull` + restart | **a split window** — AC-4 sees two builds, and an average of two builds describes no robot that ever existed |
| **anything that reboots the board** | a **new clock frame** on top of the above. No RTC here, so the machine comes up stale and NTP steps it later; the grade report's `CLOCK` line names every backwards step, and AC-0/AC-2 then carry a caveat saying their figures span two clocks (#439) |

⚠️ **A second unplanned stop does not end the window** — decided in advance, #439 AC-5, recorded in
`docs/demos/m11_evidence/window.json`. AC-3b reports the **count**. But the costs above are
cumulative against a **7 h 12 m total**, so repeated stops fail this window on **AC-2**, which is a
bar that was agreed before the run rather than invented after it. **A deploy is the only thing that
restarts a window.**

**Write it down before you act**, one JSON object per line:

```sh
echo '{"at": 1787404800, "kind": "power_cut", "note": "unplugged the bench strip"}' \
  | sudo tee -a /var/lib/soak/interventions.jsonl
```

⚠️ It **explains** an event; it does not **excuse** one — AC-3 and AC-3b keep their verdicts either
way. It exists because a power cut and a crash leave byte-identical records and the journal is
volatile, so nothing else will remember which was which.

If a deploy genuinely must happen, that is a decision to **restart the window**, recorded as such in
`docs/demos/m11_evidence/window.json`. Restarting is not a disaster; *silently* restarting is.

### 4.1 Restart the robot

```sh
journalctl -u robot -b > /tmp/before-restart.txt     # FIRST — the journal is volatile
sudo systemctl restart robot
```

**Destroys:** the in-flight turn; the Realtime session (it is cold — there is no resumption, and the
next one is re-seeded with instructions and memory); every queued event (the bus is in-memory,
at-most-once, no replay, no dead-letter queue); and, unless you ran the first line, this boot's
journal once the machine reboots. **Survives:** every fact, routine, trigger and episode, the
quiet-hours overrides and cooldowns, and the boot history.

### 4.2 Reinstall the units and config from the repo

The recipe is [`PI_OPERATIONS.md` §3](PI_OPERATIONS.md#3--deployment-drift--the-machine-is-not-the-repo)
— `install(1)` for `robot.service`,
`soak-sampler.service`, the journald drop-in and `/etc/robot/config.toml`, then `daemon-reload`.
Back the live config up first, and **validate with the loader, never by eye**.

**Destroys:** the provisioning flips in the live config, if you copy the template wholesale — the
repo ships every device `fake`, so a wholesale copy gives you a robot that starts and does nothing
real. That is why §3 of that document splices sections instead of copying the file.

### 4.3 Rebuild the venv

[`PI_OPERATIONS.md` §2](PI_OPERATIONS.md#2-the-venv--three-ways-to-break-it): `--system-site-packages`, no `pip` inside it, numpy `< 2`,
and **name every extra** — a rebuild that omits `--extra openai` gives you a robot that starts and
cannot speak.

**Destroys:** nothing on disk, but roughly twenty minutes on ARM for the `pi` extra (onnxruntime),
during which the robot is down and the soak is losing uptime.

### 4.4 Back up and restore the database

```sh
sudo /opt/avid/.venv/bin/python -c "import sqlite3; \
 s=sqlite3.connect('file:/var/lib/robot/robot.db?mode=ro',uri=True); \
 d=sqlite3.connect('/var/backups/robot/pre.db'); s.backup(d)"
```

⚠️ There is **no `sqlite3` CLI** on this machine, and Python's `.backup()` is the correct online path
anyway — consistent even with the service running. Restore by stopping the robot, moving the file
back, and starting it.

**Destroys:** on restore, everything learned since the backup. Note that WAL with
`synchronous = NORMAL` means a *process crash* loses nothing committed while an *abrupt power cut*
may lose the last transaction or two; that trade is deliberate.

### 4.5 Re-image

Last resort. Provisioning is `PI_OPERATIONS.md`'s job from its §0 onward, and boot-from-USB-SSD is
#382.

**Destroys:** everything not in the repo — the live config's flips, `robot.db` (every fact, episode
and boot record), the ALSA mixer state, and the soak window outright.

---

## 5. What not to do

| Do not | Because |
|---|---|
| `uv sync` on the Pi | it **destroys** the venv — rebuilds it without `--system-site-packages` and silently loses `picamera2` |
| `uv run` on the Pi | the same rebuild and the same loss, while looking like it worked |
| name one extra and assume the rest survive | `uv sync --extra X` **uninstalls** the unnamed ones; dropping numpy once turned the suite 77 red in untouched files |
| invert a servo sign in code to fix a direction | the horn is mis-seated; a sign in code makes the linkage and the model disagree forever. Reseat it |
| edit `/etc/robot/config.toml` to change behaviour | that is the copy that rots. Change the repo profile and reinstall, or the next reinstall silently reverts you |
| reconcile the two configs' quiet window | ⚠️ the machine's `02:00`→`09:00` and `session_idle_close_s = 300` are **deliberately** different from the repo profile's `22:00`→`07:30` and `30`. **Do not reconcile in either direction.** Every other differing key resolves to the same value via schema defaults |
| `git pull` and restart during the soak | a split window (AC-4). Working on `main` is fine; deploying is not |
| reboot before capturing the journal | `Storage=volatile` — a crash's last words die with the reboot that follows it |
| pipe a probe to `tail` | it hides the exit code. A probe once "completed" having died on line one, and a human sat watching a still robot |
| `pkill -f <pattern>` over SSH | it matches its own session; `ssh` returns 255 and the target may still be running |
| trust a level measurement taken at an unknown AGC state | it is uncalibrated, in either direction |
| read a `0` from `/metrics` as zero | check the `absent` list first — an instrument that did not run reports nothing, and nothing renders exactly like zero |

---

## 6. Where everything else lives

Pointers, not copies.

| For | Read |
|---|---|
| Reaching, driving and provisioning the Pi, and every hardware trap | [`PI_OPERATIONS.md`](PI_OPERATIONS.md) |
| Per-milestone gate commands, and verifying supervision | [`README.md`](README.md) |
| The failure catalogue (F-1…F-11), degraded modes, the watchdog, crash recovery, durability, O5 | [`../SDS.md`](../SDS.md) §12 |
| Secrets, network exposure, privacy, and what "forget" guarantees | [`../SECURITY.md`](../SECURITY.md) |
| What the last session did, and what is in flight | [`../docs/handoff.md`](../docs/handoff.md) |
| The soak harness itself (`--mode sample` and `--mode grade`) | [`../docs/demos/soak_pi.py`](../docs/demos/soak_pi.py) |
| The units and their comments — often the fastest answer of all | [`robot.service`](robot.service), [`soak-sampler.service`](soak-sampler.service) |

**Findings during the soak land here as they arrive.** An entry written the night it is learned is
worth more than five reconstructed from memory in October.
