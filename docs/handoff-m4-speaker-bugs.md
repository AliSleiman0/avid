# Handoff — two `AlsaSpeaker` defects blocking the M4 gate (AVID-91)

**Status:** found and measured on the Pi on 2026-07-26. **Fixed on `fix/alsa-speaker-drops`** —
this file is the evidence behind that branch, committed with it and kept as written.
**Blocks:** `#91` (M4 on-Pi gate) and therefore `v0.M4.0`.
**Pi state:** powered off. Read `deploy/PI_OPERATIONS.md` before touching it again.

> **What the fix changed, against what this document proposed.** Three corrections are worth
> carrying forward, because they are the parts the original analysis got wrong:
>
> - **Period-slicing does not fix bug 1.** The underrun comes from a persistent handle used
>   *intermittently*; slicing only moves it to the utterance boundary. Recover-and-retry is the
>   whole fix. Slicing earns its place for three other reasons — barge-in granularity, partial
>   rather than all-or-nothing accounting, and 50 fewer thread hops per second in `play_file`.
> - **Resampling was never an option**, so the reopen-on-rate-change was forced: `audioop` is
>   removed in Python 3.13.
> - **§0's headline needed a port change, not just a harness one.** `Speaker.play`/`play_file`
>   now return the milliseconds the device *accepted*, `AudioService` publishes that as
>   `played_ms`, and the harness fails on zero or on elapsed-vs-played divergence. Note the
>   honest wording: ALSA acknowledges frames into its ring buffer, not out of its DAC — exact
>   about drops, optimistic by up to one buffer depth (~107 ms) about sound in the room.

---

## 0. The headline finding

The M4 gate harness printed

```
turn       round-trip
1             0.13 ms
2             0.13 ms
3             0.14 ms
PASS: all 3 turns within the 200 ms turnaround budget
```

**while the robot was mute.** Nothing came out of the speaker, and the run still passed.

That is the finding to carry forward, above either individual bug. The harness measures
`playback_started.monotonic_ns − speech_ended.monotonic_ns`, which proves the *event* path fired.
It says nothing about whether PCM reached the DAC. `AudioService` then publishes
`audio.playback_finished` with a `played_ms` computed **arithmetically from the buffer length**
(`_pcm_ms(pcm, …)`), not from anything the device reported — so the system confidently asserts it
played audio it never played.

Any fix should close that gap, not just the two proximate bugs. A gate that can pass on silence is
not a gate.

---

## 1. Bug 1 — `write()`'s return value is discarded, so audio is silently dropped

**Where:** `avid/adapters/speaker.py:195-199`

```python
def _write_blocking(self, pcm: bytes) -> None:
    if self._stopped.is_set():
        return
    self._ensure_open().write(pcm)      # ← return value thrown away
```

**Mechanism.** `play()` writes an entire utterance in one call to a persistent handle. The write
blocks for the audio's real duration, then the ALSA ring buffer drains to empty and the stream
underruns. The **next** `write()` returns `-32` (`-EPIPE`, XRUN) immediately, having played
nothing; the one after that recovers. So playback alternates: heard, dropped, heard, dropped.

**Raw measurement** (Pi, `device="default"`, `rate=24000`, `periodsize=480`, 3 s buffer):

```
write 1: returned 48000  after 1.898s
write 2: returned -32    after 0.000s
write 3: returned 48000  after 1.909s
write 4: returned -32    after 0.000s
info: {'period_size': 512, 'buffer_size': 2560, 'rate': 24000}
```

Reproduced through the adapter itself, same alternation:

```
same handle write 1: 1.901s
same handle write 2: 0.001s      ← dropped
same handle write 3: 1.899s
```

A **fresh** handle per write never drops — that is why the standalone five-clip diagnostic was
audible while the gate was not.

**Why it matters beyond M4.** At M5 the Realtime stream calls `play()` once per PCM delta, many
times per response, against this same persistent handle. Every underrun silently eats a delta.

**Fix sketch.** Check the return; on a negative result treat it as XRUN, recover, retry once, and
log a warning with the correlation ID. Never return normally from a write that moved no samples.
Note `pyalsaaudio` may also raise `alsaaudio.ALSAAudioError` rather than return negative depending
on version — handle both. Verify against the version actually installed in `/opt/avid/.venv`.

---

## 2. Bug 2 — `AudioChunk.sample_rate` is ignored

**Where:** `avid/adapters/speaker.py:169-173`, `201-204`

```python
async def play(self, chunk: AudioChunk) -> None:
    await asyncio.to_thread(self._write_blocking, chunk.pcm)   # ← chunk.sample_rate unused

def _ensure_open(self) -> Any:
    if self._pcm is None:
        self._pcm = self._open_blocking(self._sample_rate, self._channels)  # ← config's rate
```

The handle opens at `config.speaker.sample_rate` — **24 kHz**, correct for the Realtime API — and
then plays whatever bytes it is handed at that rate. The M4 loopback echoes **microphone** PCM,
which is **16 kHz**, and labels the chunk honestly:

```python
# avid/services/audio.py, _loopback()
await self._speaker.play(
    AudioChunk(pcm=pcm, sample_rate=self._sample_rate, channels=self._channels)
)
```

So the echo plays **1.5× fast and a fifth high**. Measured: 6.00 s of captured audio finished in
**4.01 s**. A one-second "Hello Pico" comes back as a ~0.6 s squeak — genuinely easy to miss in a
room, which is why this went unnoticed until someone put on a headset.

**The chunk is not lying; the adapter is.** `AudioChunk` carries `sample_rate` precisely so a
consumer can honour it. The adapter's docstring asserts the opposite:

> Playback is fixed at **S16_LE, 2 bytes/sample, 24 kHz mono** … `AudioChunk` carries no
> bit-depth field, so it is implicit.

Bit depth being implicit is fine. Sample rate being implicit is the bug — the field exists.

**Fix sketch.** Track the rate the handle was opened at; when `chunk.sample_rate` differs, close
and reopen at the chunk's rate before writing. Rate changes only between utterances, so the reopen
cost is irrelevant. Update the docstring — it currently documents the defect as a design.

**Decide deliberately and record it:** does `[speaker] sample_rate` become an initial default
rather than a hard setting? If its meaning changes, `SDS.md` §6.2.4 and the config comment must
change with it (DoD: "SDS updated if an interface/event/schema changed").

---

## 3. Suggested shape of the work

Two defects, one file, one blast radius. My recommendation is **two issues, one PR** — they are
discovered together, tested together, and neither is independently releasable — but the project's
one-PR-per-issue discipline may argue otherwise. Decide and say why in the PR body.

Labels to mirror from `#90`: `comp:audio`, `prio:must`, `size:S`, milestone **M4 — Audio loop**.

### Tests

The contract suite is already parametrized over fake and real (`FAKE_REAL_PARAMS`, `skip_off_pi`
in `tests/contract/_hardware.py`), so both behaviours belong there and will run against the fake in
CI and the real adapter on the Pi:

- **consecutive `play()` calls both reach the device** — the regression test for bug 1. Against
  `FakeSpeaker` it asserts both chunks land in `played`; against `AlsaSpeaker` on the Pi it is the
  real XRUN path.
- **a chunk's declared `sample_rate` is preserved** — the regression test for bug 2. `FakeSpeaker`
  already records whole `AudioChunk`s, so this is assertable off-hardware today.

`unittest.mock` is banned outside `tests/adapters/` (SDS §14.3). A unit test for the `-EPIPE`
recovery branch needs a fake PCM object — that belongs in `tests/adapters/test_speaker.py`, where
mocks are permitted, or as a small hand-written stub.

### DoD reminders

`ruff check` / `ruff format`; `mypy --strict` on touched modules; `import-linter`; tests under
`PYTHONASYNCIODEBUG=1`; CI green on **3.11 and 3.13**; new failure paths log with a correlation ID;
no new `# type: ignore` without an inline reason. On the Pi, install tools at the versions
`uv.lock` pins (`ruff==0.15.22`) — a newer ruff reports rules that postdate the code.

---

## 4. What the gate still needs after the fix

**Already banked** — pulled to `C:\Users\Admin\avid-m4-takes\`, because the Pi's `/tmp` does not
survive a reboot:

| file | content | measured |
|---|---|---|
| `takeA.wav` | 140 s of the user speaking | peak −3.0 dBFS, Silero **34.3% speech** |
| `takeB.wav` | 60 s of deliberate transients — knocks, clap, door, chair | peak 0.0 dBFS, **0 / 3000 frames false-open** |
| `ambience_a.wav` | 135 s of unstaged room tone | 0.6% VAD-positive |
| `echo_in.wav` | 6 s headset capture | peak −8.4 dBFS |
| `loopback.log`, `echo.log`, `tone2.log` | the runs above | — |

`takeB` is SDS §6.3's door-slam requirement met on real audio rather than a synthesised burst, and
it is the strongest number in the run. `ambience_a` was an accident — a recording made while the
user had not yet been told to speak — and is therefore a genuinely unstaged negative control.

**Still owed for `#91`:**

1. Assemble one labelled ~10-minute WAV from the above (ambience as the canvas, speech and noise
   units spliced in). Label the **speech-energy region, not the clip extent** — labelling extents
   is what produced a bogus 56% "missed-speech" in an earlier attempt. Separate takes are what
   make the labels trustworthy: energy alone cannot tell a voice from a door slam.
2. Re-run `--mode loopback` live and confirm the echo **by ear** (AC-1's second half, per the
   clarification posted at `#91` comment 5081048057).
3. The 60-second AC-3 demo video. Audio cannot witness itself here — the only device that could
   record the speaker is the mic, and it is both held by the harness and too weakly coupled to the
   amp. A phone video is the artifact.
4. AC-4 `docs/demos/README.md`, AC-5 PMP/SDS rows, AC-6 tag and close epic `#84`.

---

## 5. Bench gotchas paid for tonight

- **The Logitech H540 headset has a hardware mute** the OS cannot see. ALSA reported
  `Mic … 89% [on]` while capture sat at **−46.7 dBFS**. Unmuted it read −8.4 dBFS. If capture looks
  dead, check the boom before you check anything else.
- **The mic swap silently repointed two files.** `/etc/robot/config.toml` `[microphone] device` and
  `/etc/asound.conf`'s `usbmic` block both named `CARD=Device`, which ceased to exist. Both now say
  `CARD=H540` and therefore **diverge from `config/pi.toml` in the repo**. Backups:
  `config.toml.bak-pre-headset`, `asound.conf.bak-pre-headset`. Decide which mic is the gate mic
  and reconcile — this is exactly the drift `deploy/PI_OPERATIONS.md` §3 exists to prevent.
- **`robot.service` is stopped but still `enabled`** — the next boot restarts it and it takes
  `127.0.0.1:8787`, failing `test_boot.py` and `test_supervision.py`.
- **Python buffers stdout when it is not a terminal.** The harness's "Speak 3 short phrases" prompt
  never reached the log, so the operator was talking into a void. Run it with `python -u`.
- **Detach background work with a launcher script.** `ssh host 'cmd &'` still waited on the channel
  and blocked for the full duration; a `.sh` that backgrounds and `exit 0`s returns in ~1 s.
