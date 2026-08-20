# #310 AC-1 — `set_affect` fires. The premise does not reproduce.

**Run:** 2026-08-20, laptop, no hardware. `tools/probe_tool_call_rate.py`, corpus
`tools/affect_probe_script.json` (12 turns, 4 per class, written down before the run).
Two live sessions, one per model. Raw evidence beside this file.

## What AC-1 asked

> Establish which of the above it is, cheaply, before writing any inference code. A single run
> comparing tool-call rates for `remember_fact` and `set_affect` in the same session separates
> "this tool" from "tools in speech-to-speech".

## What the run found

| | `set_affect` turns (4) | `remember_fact` turns (4) | neutral controls (4) |
|---|---|---|---|
| `set_affect` fired | **4** | 0 | 0 |
| `remember_fact` fired | 0 | **4** | 0 |

Identical on **both** models — `gpt-realtime-2025-08-28` (the deployed flagship) and
`gpt-realtime-mini-2025-12-15`, same corpus, same instructions, one variable changed.

Every turn was transcribed and answered (12/12, ~3.3 MB of assistant audio per session), so this
is not a zero hiding in a dead session — the distinction the probe exits non-zero to protect.

**The answer to AC-1 is "none of the three."**

1. **Not the modality.** `set_affect` fired in a genuine speech-to-speech session — audio in,
   audio out — four times out of four.
2. **Not the description.** It fired with the shipped description unchanged, constraint-leading
   clause and all.
3. **Not the enum.** Three values were enough; the model never declined for want of an option.

And it discriminates: **zero false fires** across the eight turns not written to invite it. The
tool is not merely reachable, it is well-behaved — which is more than the issue hoped for.

Both variant arms (`--arm wide-enum`, `--arm strong-description`) were therefore **left unrun**.
They exist to separate cause 2 from cause 3, and this result closes that question; spending a
live session on them would be buying an answer nobody now needs.

## So AC-2 is not authorised — and that is the point of the gate

§6.8's local inference was the contingency for a tool that does not work. This tool works. Adding
sentiment inference now would ship the failure mode §6.8 itself predicted (*"I'm sorry to hear
that"* rendering SAD when the right face is *concerned*) as a **replacement for a mechanism that
is behaving correctly**. AC-1 exists precisely to stop that, and it just did its job.

## What is still unexplained — and it is not a prompt

Six live runs on the rig produced zero. Two laptop sessions produce 8/8. The difference is
therefore in the **conditions**, not the tool, and the candidates are now, in order:

1. ⚠️ **The rig may not have been running the code the repo shows.** #309's clause fix landed in
   `bc05261` on **2026-08-16** — the same day #310 was filed. `git log` confirms the clause has
   not been touched since, so the string this run used is byte-identical to the one #310 reports
   as failing. *The machine is not the repo* (CLAUDE.md, `deploy/PI_OPERATIONS.md`): if
   `/opt/avid` had not been redeployed, the gate measured the pre-#309 clause and #310's "after
   #309" rows are mislabelled through no fault of their author. **One SSH command settles it**
   and it should be the first thing run when the Pi is back:

   ```sh
   ssh alisleiman0@<pi> 'cd /opt/avid && git log -1 --format="%h %ad %s" --date=short'
   ```

   If that SHA predates `bc05261`, #310 is explained and closes as *already fixed by #309*.

2. **Audio quality.** #283 — 50 Hz mains hum defeats the voice gate — cost M8's AC-7 conversation
   nine dropped turns. SAPI speech into a socket is far cleaner than a real mic on that desk, and
   a model reconstructing a fragmented utterance may well spend the turn answering rather than
   decorating. #283 is `prio:must` and open.

3. **Session shape.** These sessions were cold and short (12 turns, no §6.7 memory block); the
   gate's were ~30 turns with layer 4 injected. Cheap to test from a laptop if 1 and 2 come back
   clean.

## Honest limits of this run

- **n = 4 per class, per model.** Deliberately reported as "4 of 4" and never as a percentage.
  Four is enough to refute "never fires"; it is not enough to state a rate.
- **The stimulus is synthesized speech, not a person.** It is real audio through the real path,
  which is what makes it a speech-to-speech result — but it is clean, evenly paced, and free of
  the room. That is the whole content of candidate 2 above.
- **Each utterance is isolated and unambiguous.** Real emotional content arrives mid-conversation
  and hedged.
- **AC-4 is untouched and still `hardware-required`.** It grades this live, with a person, and
  this run does not stand in for it. #310 stays open.
