# Codebase Rules — Avid / Pico

> AI Desktop Companion Robot, codename *Pico*. This file is the contract every
> change is held to. It is derived from, and subordinate to, `SDS.md` (System
> Design Specification) and `PMP.md` (Project Management Plan). Where this file
> and the SDS disagree, the SDS wins — and this file is wrong and should be fixed.

Companion documents:

- **`SDS.md`** — architecture, event catalog, ports, ADRs. The source of truth.
- **`PMP.md`** — plan, milestones, risk register, Definition of Ready/Done.
- **`CONTRIBUTING.md`** — workflow: DoR/DoD, WIP limits, commits, branches.
- **`SECURITY.md`** — secrets, network exposure, privacy, supply chain.
- **`deploy/PI_OPERATIONS.md`** — driving the Pi from the laptop, and every hardware trap the
  sealed gates paid for. **Read before touching the Pi.** The rule it exists to teach: *the machine
  is not the repo* — `/etc/robot/config.toml` and the systemd unit are copies that rot, and because
  missing config keys fall back to schema defaults, drift yields **silently wrong results**, not
  errors.

---

## 1. The eight principles (SDS §3.2)

These are **enforced, not aspirational**. Each has a mechanism; the mechanism is
the point. "I followed the principle" is not a claim you get to make — CI checks.

| # | Principle | Enforcement |
|---|---|---|
| **P1** | **The dependency rule.** Source dependencies point inward only: infrastructure → adapters → application → domain. The domain imports nothing from the project. | `import-linter` contract in CI. Build fails on violation. |
| **P2** | **Depend on Protocols, not classes.** No application code names a concrete adapter. | Code review + P1's linter. |
| **P3** | **Composition root is singular.** Only `main.py` (and test fixtures) instantiate adapters. | CI grep for `Real*(` / `Fake*(` outside allowed paths. |
| **P4** | **Events are facts, not requests.** An event says what *happened*. Publishers do not know or care who listens. | Naming `<domain>.<past_tense_verb>`, checked at registration. |
| **P5** | **No module reaches through another.** `ai` may not import `motion`. Cross-module effect happens via the bus. | Per-module `import-linter` contract. |
| **P6** | **Every port has a fake.** A port without a working simulator adapter is an incomplete port. | Contract-test suite runs against every adapter, real and fake. |
| **P7** | **Configuration is injected, never read.** No module calls `os.environ` or reads a file. It receives a typed config object. | CI grep for `os.environ`/`os.getenv` outside `core/config.py`. |
| **P8** | **Blocking I/O never touches the event loop.** | `PYTHONASYNCIODEBUG=1` in CI; slow-callback warning **> 50 ms** fails the run. |

If you find yourself fighting one of these, you have put logic in the wrong
layer. Move the logic, don't weaken the rule.

---

## 2. Layering (SDS §3.4, §3.6)

```
main.py            composition root — wires everything, the only place adapters are built
  └─ adapters/     Real*/Fake* implementations of ports (fakes ship here, NOT in tests/)
       └─ services/    async use-case orchestration; depends on domain + ports; knows nothing concrete
            └─ core/      EventBus, StateManager, Config, Lifecycle, ports.py (the Protocols)
                 └─ domain/   pure: no I/O, no async, no third-party runtime deps but pydantic
```

Directory layout (SDS §3.6): `domain/  core/  services/  adapters/  main.py`.

**Domain purity (P1):** `domain/` imports nothing from the project and nothing
third-party beyond the stdlib and `pydantic`. `numpy`, `pygame`, `sqlite3`,
`asyncio`, `openai`, `cv2` are forbidden imports there. If you want `import numpy`
in `domain/`, the logic belongs in a service or adapter.

**Services** subscribe to events, call ports, publish events. A service that does
anything else is misfiled. *Deciding* to be happy is domain logic with a unit
test; *getting a happy face onto glass* ends in an adapter carrying a device
dependency — keep `AffectService` and `ExpressionService` separate (SDS §3.6.1).
That split is exactly what P1 exists to protect. Note the face itself is composed
in the **stdlib** — `RGB888` bytes, no drawing library, runtime deps stay
pydantic-only (ADR-012, SDS §3.6.4); the display *backend* behind the port stays
free to change.

---

## 3. Design patterns (mandated)

- **Ports & Adapters / Hexagonal** (ADR-003). Every device is behind a `Protocol`
  in `core/ports.py`, defined by *what the application needs*, never by what the
  device offers. That inversion is the whole value.
- **`typing.Protocol`, structural typing** — `EventBus`, `Camera`, `Servo`,
  `Display`, `Microphone`, `Speaker`, `Clock`, `Embedder`,
  `VoiceActivityDetector`, and the M5/M7 ports `RealtimeClient`, `TurnSink`,
  `FactRepository`, `Retriever`, `TextModel`, `MemoryTools`, `EpisodeStore`
  (SDS §3.9.1/§9.3). `@runtime_checkable` where negotiated.
- **Single composition root / dependency injection** (P3). Services *declare*
  their subscriptions (`subscriptions()`); `main.py` *registers* them. Subscription
  is static — registered at composition time only; runtime subscription raises.
- **Frozen, slotted, kw-only dataclasses** for the `Event` envelope and all
  domain values; **frozen dict** for the transition table; **frozen pydantic
  model** for config. Immutability isn't stylistic — handlers dispatch
  concurrently, so a mutable event is a data race with extra steps.
- **Pure functions** for domain logic — `next_state()`, the policy gate, scoring,
  instruction assembly. No I/O, no clock, no globals. Tier-1 unit tested.
- **Fakes over mocks.** `unittest.mock` is **banned outside `tests/adapters/`**
  (SDS §14.3). Fakes are first-class implementations that ship in `adapters/` and
  *are* the simulator (SDS §3.9.2) — which is why the simulator can never drift
  from the real system: it *is* the real system.

### The vendor boundary — model/provider changes are config + one adapter, never domain

No vendor (OpenAI, its Realtime API, a voice, a model version) is ever named or
imported outside `adapters/`. The domain and services are provider-agnostic; a
service works in terms of domain events, never a vendor's message shapes.

- **Swapping model or voice is a config edit**, not code — `[ai] model`, `[ai]
  voice`, `[adapters] realtime = "openai" | "replay"` (SDS §9.6). Model names are
  pinned to dated snapshots (currently `gpt-realtime-2025-08-28`, SDS §6.10.5)
  because the Realtime family churns fast (SDS §6.10 volatility warning). The
  payoff is real: M5 swapped mini → flagship for a **latency** reason in one
  config line, and only the cost meter's rate table moved with it.
- **Measure the model before you tune around it.** M5 was about to amend a
  normative latency budget on the assumption that the flagship would be slower;
  `tools/probe_first_token.py` measured four snapshots in two minutes with no
  hardware and found it **200 ms faster**. Because the vendor lives behind one
  adapter, that experiment was cheap — which is the point of the boundary, not a
  side effect of it.
- **The adapter is the blast radius.** If OpenAI changes the Realtime API, exactly
  one adapter changes (risk R-10, PMP §9.2). The `RealtimeClient` adapter →
  `ConversationService` translates the vendor's stream into our `conversation.*`
  domain events; `avid/domain/` stays sealed (the domain-purity test enforces it).
- A new model reaches the domain only if it introduces a genuinely new *fact* —
  that's a new `Event` subclass, and even then the envelope base is untouched.

---

## 4. The event bus (SDS §3.5, §9.1)

- `publish()` is **fire-and-forget** — returns once queued, not once handled;
  never raises due to a subscriber failure.
- Handlers dispatch **concurrently**, not sequentially.
- **A raising subscriber is logged, swallowed, and republished as
  `system.handler_failed`.** A crashing display renderer must not kill a
  conversation. This is the single most important reliability property in the
  system — write its test first.
- `subscribe(event_type, handler, *, name=...)` — `name` is **mandatory** so the
  drift check can see every subscriber (anonymous lambdas are invisible).
- Per-subscriber **bounded queue** (default 100). Overflow policies `DROP_OLDEST`
  / `DROP_NEWEST`; **`BLOCK` is forbidden** (it would push backpressure into the
  audio path). Overflow publishes `system.handler_failed` with
  `reason="queue_overflow"`. Silent drops are a debugging catastrophe; loud drops
  are a tuning signal.
- Delivery is **at-most-once**, in-memory, no replay, no dead-letter queue. The
  bus carries **notifications, not obligations**: if losing it would be a
  correctness bug, it's a direct awaited call, not an event (e.g. memory writes,
  deletions, renders, servo moves).

### The `Event` envelope (SDS §9.1.1)

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    event_id:       UUID
    correlation_id: UUID   # the TURN this belongs to
    timestamp_ms:   int    # epoch ms, wall clock — for logs/humans
    monotonic_ns:   int    # time.monotonic_ns() — for latency math
    source:         str
```

- **Never subtract `timestamp_ms`.** Wall-clock jumps (NTP steps, Pi clock
  correction at boot) yield negative latencies that poison the metric the project
  is graded on. Wall for humans, monotonic for arithmetic.
- Event names are `<domain>.<past_tense_verb>` (P4). `affect.changed` is right;
  `display.set_emotion` is a command wearing an event's clothes — wrong.
- `correlation_id` is minted only at a turn's origin (`audio.speech_started` or
  `behavior.trigger_fired`) and propagated to every downstream event. One `grep`
  on it reconstructs the whole turn.

---

## 5. State & affect (SDS §3.10)

- `RobotState` (operational): BOOTING, IDLE, LISTENING, THINKING, SPEAKING,
  SLEEPING, DEGRADED. Transitions live in **one normative frozen table**
  (`domain/state.py`); `next_state()` is a **pure function**. Any transition not
  in the table raises `IllegalTransition` — loudly in tests, logged-and-ignored in
  production. ~90% of bugs in a system like this are illegal transitions; the
  exhaustive `test_no_undocumented_transitions` cashes that for ~4 ms.
- `Affect` (emotional) is **orthogonal** to `RobotState`. The robot can be
  SLEEPING-and-content or LISTENING-and-confused. Nothing in `domain/` may couple
  the two — conflating them is the most common design error in this class of
  project.

---

## 6. Concurrency & Python version

- **ADR-002:** single OS process, asyncio-based; **threads only for blocking
  device I/O**.
- **P8:** no blocking I/O on the loop; CI runs `PYTHONASYNCIODEBUG=1` and fails on
  slow-callback warnings > 50 ms.
- **ADR-008:** `pyproject.toml` declares `requires-python = ">=3.11"` — **not**
  `>=3.13`. Dev is 3.13; the Pi runs system Python 3.11 (so `picamera2`/
  `libcamera` import). **No 3.12+ syntax ships.** CI runs the suite on **3.11 and
  3.13**. `picamera2` is an optional dependency group imported only inside its one
  adapter.

---

## 7. Definition of Done (summary — full text in `CONTRIBUTING.md` §DoD)

Every change, no exceptions: merged via PR; `ruff check`/`ruff format` clean;
`mypy --strict` clean on touched modules; `import-linter` passes; new non-adapter
code **≥ 90% covered**; contract tests updated if a port changed; tests pass under
`PYTHONASYNCIODEBUG=1`; CI green on **3.11 and 3.13**; SDS updated if an interface/
event/schema changed; new failure paths log with a correlation ID; no new
`# type: ignore`/`# noqa` without an inline reason.

### 7.1 Gates and harnesses are held to the same standard as the robot

**A gate that can pass on silence is not a gate** (M4's lesson, `docs/demos/`).
M5 added the corollaries, and it added them the expensive way — of the defects
its bench runs surfaced, *most were in the measuring, not the robot*: a network
cut that never closed a socket, a reporter that returned on its first failure and
hid a criterion that had passed, a summary printing `abs()` of a quantity the
check grades one-sided, a banner quoting a config value the run no longer used,
and a "P95" over five samples that is arithmetically the maximum.

So, for anything that produces a number a milestone turns on:

- **Report the quantity you grade.** A summary line describing something the
  check does not test is a bug report filed against nothing.
- **Name the statistic honestly.** Nearest-rank P95 below n=20 *is* the maximum;
  printing it as a percentile misleads in both directions.
- **Every criterion reports before any verdict is decided.** Hiding a passing
  criterion behind an unrelated failure is the sibling of passing on silence.
- **A stimulus the harness induces is not a measurement of the robot** — a run
  that cuts the network cannot also claim a latency result.
- **Read config, never restate it.** A literal in a banner or docstring is drift
  with a delay fuse.
- **Prove the fix bites**: commit, then neuter the specific guard and confirm an
  *assertion* fails. A red for the wrong reason is not a proof.

Widening a budget to fit a measurement is a last resort, taken only with the
diagnosis attached — and the original target stays visible in the report, or it
quietly becomes whatever was last achieved.

---

## 8. Security (summary — full rules in `SECURITY.md`)

`OPENAI_API_KEY` is read **once**, in `main.py`, as pydantic `SecretStr`; never in
a config file, log, or repr. Config is injected, never read (P7). The local
control API binds `127.0.0.1` — localhost binding is the authentication; `0.0.0.0`
is a security bug, asserted at startup. Data at rest is on-device SQLite, never
synced; "forget" is a hard cascading DELETE. See `SECURITY.md`.
