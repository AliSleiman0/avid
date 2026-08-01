# Future enhancement — one turn-taking authority, not two

**Status:** deferred, not scheduled. Tracked as [AVID-194](https://github.com/AliSleiman0/avid/issues/194).
**Found:** 2026-08-01, chasing #106 AC-4, `main` @ `d1302fc`, flagship model live.
**Owner decision (2026-08-02):** documented and deferred. The M5 seal does not wait for it.

---

## The problem, exactly

The system runs **two independent voice-activity detectors over the same microphone audio**, and
both are authoritative for turn boundaries:

| | detector | declares the turn over after |
|---|---|---|
| local | Silero, `[gate] silence_hold_ms` | **900 ms** of silence |
| server | OpenAI, `[ai.turn_detection] silence_duration_ms` | **500 ms** of silence |

**A 500–900 ms pause is ordinary speech** — drawing breath, thinking mid-sentence, hesitating
before a name. Inside that window the *server* commits the buffer and starts answering, while the
local gate still considers the utterance open. The user is then talked over mid-sentence by a reply
to a fragment of what they said.

That is the whole defect. Everything below is a consequence of it.

### Why it is not a tuning problem

- The two windows cannot be made equal. AVID-176 measured that directly: 500/500 produced **2
  replies in 13 turns**, 900/900 produced **1 in 8**. `Config` now asserts a ≥ 200 ms margin.
- Given a required margin, **whichever window is smaller commits first** — by construction. There
  is no assignment of the two knobs where only one of them decides a turn.
- Widening the server's window to stop the fragment commits pushes O1 *up* by the same amount,
  which is the wrong direction for the criterion that exposed this.

### Why it is not the model

Measured on the same run, after the flagship swap (SDS §6.10.5):

```
first token (server speech-stop -> first audio delta), 8 replies:
  394, 375, 661, 609, 382, 460, 128, 379 ms        median ~390, spread ~140
```

Against O1 for the same six turns:

```
turn      O1
1     2574.8 ms
2      289.2 ms      <- BELOW the 500 ms commit delay: impossible for a correctly paired turn
3      839.6 ms
4     2315.4 ms
5     1519.8 ms
6     -537.0 ms      <- unpairable; playback preceded its own speech
```

Same session, same network, same model. **289 ms to 2575 ms.** A quantity whose upstream legs are
stable to ±140 ms cannot vary by 2.3 seconds for a model reason. Halving time-to-first-token
(530 → 390 ms) moved O1's P50 only 1773 → 1520 ms, so **~1100 ms of O1 is neither the model nor
the configured commit delay — and it is not constant.**

### Corroborating symptoms, all one cause

From the single run of 2026-08-01 19:10:

- **8 metered turns for 6 spoken utterances.** The server manufactured turns the user did not take.
- `conversation_already_has_active_response` naming `resp_E89P8q8pKDPma0ubqVO6P` — it committed a
  second turn while the first was still generating.
- `first token: 24 ms from the server's own speech-stop (no response.created in between)` — a delta
  for a response that had begun before the speech-stop being watched.
- One `no first token after 10.0s — degrading`.
- **Two barge-ins on a run whose protocol said "do NOT interrupt"** — the robot answered a fragment,
  the user carried on speaking, and the local VAD correctly read that as an interruption.
- Unpairable O1 samples (AVID-182's exclusion path) firing on ordinary turns.

It reads as *"not seamless"* because it is not one conversation. It is two turn-takers with
different opinions about whose turn it is.

---

## The shape of the fix

**One authority, and it should be ours.**

1. Disable server turn detection (`[ai.turn_detection]` off in the `session.update` payload).
2. Commit explicitly from the local falling edge: `input_audio_buffer.commit` followed by
   `response.create`, sent when `audio.speech_ended` fires.
3. `[gate] silence_hold_ms` can then drop to ~500 ms. It is 900 today **only** because it must
   clear the server's 500 by AVID-176's 200 ms margin; with no server VAD that constraint is gone.

Turn boundaries become a property the project owns, tunes and unit-tests, instead of a negotiation
with a remote heuristic. O1 becomes `silence_hold_ms + TTFT + network` — **deterministic instead of
bimodal**, which matters more than the mean.

### Cost of the change

Not a config edit. `RealtimeClient` today is `open` / `aclose` / `send_audio` / `events` /
`truncate` / `cancel` / `send_tool_output` — **there is no commit**. So:

- a new port method on `core/ports.py`,
- three adapters (`openai`, `replay`, `capturing`),
- one call site in `ConversationService._on_speech_ended`,
- `[ai.turn_detection]` config handling,
- contract tests across all three adapters (P6), and a fixture round-trip.

Roughly a day. Nothing in `domain/` moves — this is exactly the blast radius the port boundary
exists to bound (R-10, ADR-003).

### Risks to weigh when it is picked up

- **We inherit turn-end quality.** The server's VAD is tuned by people who do this full time; ours
  becomes the only thing standing between a thoughtful pause and a truncated question. AVID-77's
  Silero measurements (0.16% false-open on five minutes of broadband noise) are the starting
  evidence, but they were never gathered against *conversational* pauses.
- **A missed commit is a wedged turn**, where today the server would have rescued it. §6.9's
  first-token deadline (AVID-171) is the backstop and is now known to work on hardware (AVID-186),
  but it degrades rather than recovers.
- **`response.create` becomes ours to not-send twice.** AVID-182's response tracking already exists
  for this; it would gain a second caller.

---

## What it does **not** promise

Projected O1 after the change is **~990 ms** (500 ms hold + ~390 ms TTFT + ~100 ms network). That
still misses PMP §5.2's **P50 ≤ 800 ms**.

The reason to do this is that it fixes turn-taking and makes the latency *predictable*. It is not a
route to the budget, and it should not be sold as one. **The remaining ~190 ms gap is itself
deferred** by owner decision of 2026-08-02 — the difference is not perceptible against SDS §11's
own R-01 note that *"a robot that visibly and audibly thinks feels responsive at 1200 ms"*, and it
does not justify holding the milestone.

---

## Related

- [AVID-194](https://github.com/AliSleiman0/avid/issues/194) — the tracking issue.
- [AVID-176](https://github.com/AliSleiman0/avid/issues/176) — measured that the two windows cannot
  be equal, and installed the 200 ms margin assertion this enhancement removes the need for.
- [AVID-182](https://github.com/AliSleiman0/avid/issues/182) — the wire-speed reader and the
  unpairable-sample exclusion, both of which this defect keeps exercising.
- [AVID-106](https://github.com/AliSleiman0/avid/issues/106) — the M5 gate whose AC-4 exposed it.
- SDS §6.3 / ADR-007 — why the local gate exists (cost: do not hold a session open on silence).
  It was added *alongside* the server VAD, and turning the server's off was never considered.
- SDS §2.8.1 — the O1 budget itself.
