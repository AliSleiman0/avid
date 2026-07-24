# System Design Specification (SDS)
## AI Desktop Companion Robot — Codename: *Pico*

| Field | Value |
|---|---|
| Document | System Design Specification |
| Version | 0.1 (Draft) |
| Status | In Progress — Sections 1–3 complete |
| Owner | Ali (Lead Engineer / Architect) |
| Last updated | 2026-07-16 |

---

# Table of Contents

## 1. Introduction
1.1 Purpose of this document
1.2 Scope of the system
1.3 Intended audience
1.4 Definitions, acronyms, abbreviations
1.5 Reference documents
1.6 Document conventions
1.7 Revision history

## 2. System Overview
2.1 Product vision
2.2 Product goals
2.3 Explicit non-goals
2.4 Primary user personas
2.5 Key usage scenarios
2.6 System context diagram
2.7 Constraints
 2.7.1 Hardware constraints
 2.7.2 Cost constraints
 2.7.3 Network and connectivity constraints
 2.7.4 Regulatory / privacy constraints
2.8 Quality attributes and their measurable targets
 2.8.1 Latency budget
 2.8.2 Availability and recovery
 2.8.3 Maintainability
 2.8.4 Testability
 2.8.5 Portability
2.9 Assumptions and dependencies

## 3. Architecture
3.1 Architectural drivers
3.2 Architectural principles
3.3 Architecture decision summary (ADR index)
3.4 Logical view — layered architecture
 3.4.1 Layer definitions and dependency rule
 3.4.2 Domain layer
 3.4.3 Application layer (services)
 3.4.4 Adapter layer
 3.4.5 Infrastructure layer
3.5 The event-driven core
 3.5.1 Rationale for event-driven design
 3.5.2 Event bus contract
 3.5.3 Event taxonomy
 3.5.4 Delivery semantics and ordering guarantees
 3.5.5 Backpressure and queue policy
 3.5.6 Anti-patterns explicitly forbidden
3.6 Component view
 3.6.1 Component inventory
 3.6.2 Component interaction diagram
 3.6.3 Dependency graph and enforcement
 3.6.4 Face rendering — ADR-012
3.7 Runtime views (sequence diagrams)
 3.7.1 Cold boot to idle
 3.7.2 Wake → listen → respond turn
 3.7.3 Memory write during conversation
 3.7.4 Proactive trigger firing
 3.7.5 Presence detected → greeting
 3.7.6 Network loss → degraded mode → recovery
3.8 Concurrency model
 3.8.1 The asyncio event loop
 3.8.2 Thread-offload policy for blocking I/O
 3.8.3 Process isolation boundaries
 3.8.4 Shared state rules
3.9 Hardware Abstraction Layer (HAL)
 3.9.1 Port/adapter contracts
 3.9.2 Simulator adapters
 3.9.3 Capability negotiation
3.10 State management
 3.10.1 Global robot state machine
 3.10.2 Sub-state machines
 3.10.3 State transition table
3.11 Deployment view
 3.11.1 Development topology (PC)
 3.11.2 Target topology (Raspberry Pi 5)
 3.11.3 Process/systemd layout
3.12 Cross-cutting concerns
 3.12.1 Configuration
 3.12.2 Logging and observability
 3.12.3 Error handling taxonomy
 3.12.4 Security

## 4. Hardware Design
4.1 Bill of materials
4.2 Block diagram
4.3 Power budget and analysis
4.4 Thermal design
4.5 Interface allocation (GPIO / I2C / USB / CSI / HDMI)
4.6 Wiring diagram
4.7 Servo mechanical design
4.8 Enclosure and physical layout
4.9 Hardware bring-up procedure
4.10 Known hardware risks

## 5. Software Module Design
5.1 Repository layout
5.2 `core` — event bus, state manager, configuration, lifecycle
5.3 `ai` — realtime client, conversation manager, personality engine, tool registry
5.4 `audio` — capture, VAD, playback, device management
5.5 `vision` — camera pipeline, detectors, presence tracker
5.6 `display` — renderer, emotion sprites, frame scheduler
5.7 `motion` — servo controller, gesture engine, safety limits
5.8 `memory` — repositories, extraction, retrieval, consolidation
5.9 `behavior` — proactive scheduler, trigger evaluation, interruption policy
5.10 `main` — composition root and dependency wiring

## 6. AI Pipeline
6.1 Pipeline overview
6.2 OpenAI Realtime session lifecycle
6.3 Turn detection strategy
6.4 Instruction composition architecture
6.5 Personality engine
6.6 Tool (function) calling catalog
6.7 Memory injection strategy
6.8 Emotion inference from model output
6.9 Fallback and degraded paths
6.10 Cost model and token/audio budget
6.11 Prompt versioning and evaluation

## 7. Memory System Design
7.1 Memory tiers
7.2 Short-term (working) memory
7.3 Long-term factual memory
7.4 Semantic memory and embeddings
7.5 Episodic memory
7.6 Fact extraction pipeline
7.7 Retrieval and ranking
7.8 Conflict resolution and fact supersession
7.9 Decay, consolidation, and forgetting
7.10 Privacy, redaction, and user deletion rights

## 8. Data Design
8.1 Conceptual data model
8.2 SQLite physical schema (v1)
8.3 Indexing strategy
8.4 Migration strategy
8.5 Vector storage approach (v1 vs. future)
8.6 PostgreSQL + pgvector migration path
8.7 Backup and restore

## 9. Interfaces and APIs
9.1 Internal event catalog (normative reference)
9.2 Service interface definitions (Protocols)
9.3 HAL port definitions
9.4 External API dependencies
9.5 Local control API (HTTP/WebSocket)
9.6 Configuration file schema

## 10. Behavior and Proactivity Engine
10.1 Design goals
10.2 Trigger types
10.3 Scheduler design
10.4 Interruption and politeness policy
10.5 Do-not-disturb and quiet hours
10.6 Behavior authoring format

## 11. Performance Engineering
11.1 End-to-end latency budget
11.2 Resource budget (CPU, RAM, thermal)
11.3 Profiling methodology
11.4 Optimization backlog

## 12. Reliability and Failure Management
12.1 Failure mode and effects analysis (FMEA)
12.2 Degraded operating modes
12.3 Watchdog and supervision
12.4 Crash recovery
12.5 Data durability guarantees

## 13. Security and Privacy
13.1 Threat model
13.2 Secrets management
13.3 Data at rest
13.4 Data in transit
13.5 Camera and microphone privacy controls
13.6 Update integrity

## 14. Testing Strategy
14.1 Test pyramid
14.2 Unit testing conventions
14.3 Fakes vs. mocks policy
14.4 Contract tests for HAL ports
14.5 Simulation harness
14.6 Hardware-in-the-loop testing
14.7 AI evaluation harness
14.8 Coverage targets

## 15. Deployment and Operations
15.1 Pi image build
15.2 Application packaging
15.3 systemd unit design
15.4 Update strategy
15.5 Telemetry and remote diagnostics
15.6 Runbook

## 16. Appendices
A. Architecture Decision Records
B. Full event schema reference
C. Full database DDL
D. GPIO/pin allocation table
E. Glossary

---

# 1. Introduction

## 1.1 Purpose

This document is the authoritative technical description of the AI Desktop Companion Robot. It defines the architecture, module boundaries, interfaces, data model, and implementation constraints to a level sufficient that an engineer who has never seen the codebase can (a) understand why the system is shaped the way it is, and (b) implement or modify any single module without reading the others.

Where this document and the code disagree, the code is wrong or this document is stale. Both are bugs. Any pull request that changes a public interface, an event schema, or a database table **must** update the corresponding section here.

## 1.2 Scope

In scope: the on-device software system, its hardware platform, its cloud AI dependency, its persistence layer, and the development/test infrastructure required to build it.

Out of scope (for v1): companion mobile application, multi-robot coordination, cloud-hosted user account system, commercial manufacturing, certification (CE/FCC).

## 1.3 Intended audience

- **Engineers** implementing modules — Sections 3, 5, 6, 7, 8, 9.
- **Reviewers** assessing a PR — Sections 3.2, 3.5, 3.9, and the relevant module section.
- **Future me, in six months** — Section 3.3 (ADR index), which explains the decisions whose rationale will otherwise evaporate.

## 1.4 Definitions

| Term | Definition |
|---|---|
| **Turn** | One complete cycle of user speech → model response → robot output. |
| **Event** | An immutable message published to the event bus describing something that happened. Past tense, always. |
| **Command** | An intent to do something. Modelled as an event with imperative naming, but routed to exactly one handler. |
| **HAL** | Hardware Abstraction Layer. The set of Protocol definitions isolating domain logic from physical devices. |
| **Port** | An interface (Python `Protocol`) defined by the application, describing what it needs from the outside world. |
| **Adapter** | A concrete implementation of a Port. Every Port has ≥2: one real, one simulated. |
| **Affect** | The robot's current emotional state. Distinct from `RobotState`, which is operational. |
| **Fact** | An atomic, attributable, timestamped assertion about the user, stored in long-term memory. |
| **Trigger** | A condition which, when satisfied, causes the behavior engine to consider initiating a proactive interaction. |
| **Composition root** | The single place (`main.py`) where concrete adapters are instantiated and injected. Nowhere else constructs infrastructure. |

## 1.5 References

- OpenAI Realtime API documentation
- Raspberry Pi 5 datasheet and power requirements
- PCA9685 datasheet (NXP)
- MediaPipe Face Detection model card
- *Clean Architecture*, Robert C. Martin — for the dependency rule
- *Designing Data-Intensive Applications*, Kleppmann — for the memory/consistency reasoning in §7.8

---

# 2. System Overview

## 2.1 Product vision

A 20 cm desktop presence that a user talks to the way they'd talk to a pet that can answer. It is not a query interface. Its value is continuity — it remembers, it notices, and it initiates. A smart speaker is a function call; this is a relationship with state.

The design consequence of that sentence is the whole architecture: **the interesting engineering is not the AI call, it's everything that persists between AI calls.**

## 2.2 Product goals

| ID | Goal | Measurable target |
|---|---|---|
| G1 | Conversation feels immediate | P50 speech-end → first audio out ≤ 800 ms; P95 ≤ 1500 ms |
| G2 | The robot remembers | ≥95% recall of explicitly-stated facts at 30 days |
| G3 | The robot initiates appropriately | ≥1 useful proactive event/day; ≤1 user-rated "annoying" event/week |
| G4 | The robot is expressive | Affect change reflected on display within 150 ms of decision |
| G5 | The robot is always there | ≥99% uptime over 30 days; unattended recovery from any single crash |
| G6 | The system is buildable by one person | Any module developable and testable on PC with zero hardware attached |

## 2.3 Explicit non-goals

Naming these prevents scope creep from being mistaken for progress.

- **Not** a general-purpose smart home hub. Integrations are a Phase 10+ concern, behind one port.
- **Not** an offline system. v1 requires internet. Offline degradation is graceful, not featureful.
- **Not** locomotion. It sits. Movement is expressive, not translational.
- **Not** a multi-user identity system in v1. Face *recognition* is Phase 8 stretch; face *detection* is the v1 commitment.
- **Not** a wake-word system in v1. Turn detection is server-side VAD via the Realtime API. A local wake word is a cost optimization (see §6.10), deferred.

## 2.4 Primary persona

Single adult user, desk-based, uses the robot 5–20 minutes/day in short bursts across a 10-hour window. Technical enough to tolerate a bug, not tolerant of a robot that interrupts a meeting.

That last clause is a hard requirement and it lands in §10.4.

## 2.5 Key usage scenarios

| ID | Scenario | Exercises |
|---|---|---|
| UC-01 | User says "Hey, how's it going?" and chats for 2 minutes | Audio, AI, personality, display, affect |
| UC-02 | User says "I drink coffee every day at 8 AM" | Fact extraction, long-term memory, trigger registration |
| UC-03 | At 07:55 the robot says "Coffee time is coming soon" | Behavior engine, scheduler, TTS-initiated turn |
| UC-04 | User sits down; robot notices and greets | Vision, presence, behavior engine |
| UC-05 | User asks "what did I say about my sister?" | Semantic retrieval |
| UC-06 | Wi-Fi drops mid-sentence | Degraded mode, recovery |
| UC-07 | User says "forget that" | Memory deletion, privacy |

## 2.6 System context

```
                       ┌──────────────────────┐
                       │   OpenAI Realtime    │
                       │   + Embeddings API   │
                       └───────────▲──────────┘
                                   │ WSS / HTTPS
                                   │
┌──────────┐   speech    ┌─────────┴──────────┐   frames    ┌──────────┐
│          ├────────────►│                    ├────────────►│  3.5"    │
│   User   │             │   Companion Robot  │             │ Display  │
│          │◄────────────┤   (Raspberry Pi 5) ├────────────►│  Servo   │
└────┬─────┘   voice     │                    │   PWM       └──────────┘
     │                   └─────────┬──────────┘
     │  presence / face            │
     └────────────────────────────►│  SQLite (local disk)
                                   └──────────────────────►
```

Trust boundary: everything except the OpenAI box is local. Audio and, if enabled, image data cross that boundary. §13.5 governs.

## 2.7 Constraints

### 2.7.1 Hardware

| Constraint | Value | Architectural consequence |
|---|---|---|
| CPU | Cortex-A76 ×4 @ 2.4 GHz | Vision must not exceed 1 core; run detection at ≤5 fps, not 30 |
| RAM | 8 GB | Generous. Not a binding constraint. Do not optimize for it. |
| Display | 480×320 | Sprite-based rendering, not vector. Design for pixel grid. |
| Storage | microSD (A2) | **Binding.** Random write is slow and the card wears out. Batch DB writes, WAL mode, no chatty logging to card. |
| Servo | 1× SG90/MG90S | 1 DoF. Gesture vocabulary is nod *or* turn, not both. See §4.7. |
| Power | 27 W USB-C | Servo stall current can brown out the Pi. Separate servo rail. See §4.3. |

### 2.7.2 Cost

The OpenAI Realtime API is billed per audio minute in and out. An always-listening robot streaming continuously to the API is financially unviable. This is not a detail — it is an architectural driver. See §6.10 and ADR-007.

### 2.7.3 Connectivity

Home Wi-Fi. Assume 20–80 ms RTT to OpenAI, occasional 100% loss for tens of seconds. The system must never present a stack trace to the user because of this.

## 2.8 Quality attributes

### 2.8.1 Latency budget (G1)

This budget is normative. Any module that exceeds its allocation is a defect, not a tuning opportunity.

| Stage | Budget (P50) | Notes |
|---|---|---|
| Mic capture → frame available | 20 ms | ReSpeaker buffer |
| Local buffering + upload chunk | 30 ms | 20 ms Opus frames |
| Network → OpenAI | 60 ms | Uncontrollable |
| Model turn detection + first token | 400 ms | Uncontrollable |
| Network ← OpenAI | 60 ms | Uncontrollable |
| Decode → speaker | 40 ms | |
| **Controllable subtotal** | **150 ms** | This is our actual engineering surface |
| **Total P50** | **~610 ms** | Headroom to the 800 ms target: 190 ms |

The lesson: ~75% of the latency is not ours. Therefore we spend zero effort micro-optimizing our 150 ms and *all* our effort on not adding to it — no synchronous DB calls in the audio path, no blocking the loop, no per-turn embedding round-trips (§7.7).

### 2.8.4 Testability

Non-negotiable: `pytest` on a laptop with no hardware, no network, and no API key must exercise ≥90% of non-adapter code. This constraint alone justifies the HAL (§3.9) and the event bus (§3.5).

---

# 3. Architecture

## 3.1 Architectural drivers

Ranked. When two drivers conflict, the higher one wins, and the resolution is recorded as an ADR.

1. **Testability without hardware** (§2.8.4) — the project dies if every change requires the Pi on the desk.
2. **Latency** (G1) — a companion that lags is not a companion.
3. **Cost containment** (§2.7.2) — an architecture that can't be afforded won't run.
4. **Independent module evolution** — one person, eleven phases, months of elapsed time. Modules must not entangle.
5. **Graceful degradation** — the failure modes are frequent and must be boring.

## 3.2 Architectural principles

These are enforced, not aspirational. Each has a mechanism.

| # | Principle | Enforcement mechanism |
|---|---|---|
| P1 | **The dependency rule.** Source dependencies point inward only: infrastructure → adapters → application → domain. The domain imports nothing from the project. | `import-linter` contract in CI. Build fails on violation. |
| P2 | **Depend on Protocols, not classes.** No application code names a concrete adapter. | Code review + P1's linter. |
| P3 | **Composition root is singular.** Only `main.py` (and test fixtures) instantiate adapters. | Grep check in CI for `Adapter(` outside allowed paths. |
| P4 | **Events are facts, not requests.** An event says what happened. Publishers do not know or care who listens. | Naming convention `<noun>.<verb-past-tense>`, checked at registration. |
| P5 | **No module reaches through another.** `ai` may not import `motion`. Cross-module effect happens via the bus. | P1's linter defines a per-module contract. |
| P6 | **Every port has a fake.** A port without a working simulator adapter is an incomplete port. | Contract test suite (§14.4) runs against every registered adapter, real and fake. |
| P7 | **Configuration is injected, never read.** No module calls `os.environ` or reads a file. It receives a typed config object. | Grep check in CI. |
| P8 | **Blocking I/O never touches the event loop.** | `asyncio` debug mode in CI; slow-callback warnings fail the test run. |

### On P1, concretely

```
        ┌─────────────────────────────────────────────┐
        │              infrastructure                 │  serial, sockets,
        │  (openai_ws, sqlite, pygame, pca9685, cv2)  │  files, drivers
        └────────────────────┬────────────────────────┘
                             │ implements
        ┌────────────────────▼────────────────────────┐
        │                 adapters                    │  Port implementations
        │   RealCamera │ FakeCamera │ RealServo │ ...  │
        └────────────────────┬────────────────────────┘
                             │ satisfies
        ┌────────────────────▼────────────────────────┐
        │            application (services)           │  orchestration,
        │  ConversationSvc │ MemorySvc │ BehaviorSvc   │  use cases
        └────────────────────┬────────────────────────┘
                             │ uses
        ┌────────────────────▼────────────────────────┐
        │                  domain                     │  Fact, Affect,
        │      pure types, rules, state machines      │  RobotState, Event
        └─────────────────────────────────────────────┘
                    imports nothing. ever.
```

The arrow direction is the whole point. `domain/` has no third-party imports beyond the stdlib and `pydantic`. If you find yourself wanting `import numpy` in `domain/`, you have put logic in the wrong layer.

## 3.3 Architecture Decision Record index

Full text in Appendix A. Summary:

| ADR | Decision | Status |
|---|---|---|
| ADR-001 | In-process async event bus, not a broker (no Redis/MQTT in v1) | Accepted |
| ADR-002 | Single OS process, asyncio-based, threads only for blocking device I/O | Accepted |
| ADR-003 | Hexagonal (ports & adapters) with an event bus for cross-module effects | Accepted |
| ADR-004 | Memory is owned by the application, not the model. The model gets tools. | Accepted |
| ADR-005 | SQLite with WAL for v1; vectors as BLOBs + brute-force cosine | Accepted |
| ADR-006 | Personality is composed instruction text + post-hoc affect mapping, not fine-tuning | Accepted |
| ADR-007 | Local VAD gate before opening a Realtime session (cost control) | **Accepted** — see §6.3, §6.10 |
| ADR-008 | Python 3.13 on PC, system Python 3.11 + `--system-site-packages` on Pi | **Proposed** — see §3.11 |
| ADR-009 | Pan+tilt (2 servo) target, 1-servo fallback; gesture engine is axis-agnostic | **Proposed** — needs your call |
| ADR-012 | Faces compose to RGB888 bytes in the stdlib; no drawing-library dependency | Accepted — see §3.6.4 |

## 3.4 Logical view — the layers

### 3.4.2 Domain layer (`robot/domain/`)

Pure. No I/O, no async, no third-party runtime deps. This is where the rules live that would still be true if we swapped every piece of hardware.

Contents:
- `Event` base type and the full event hierarchy
- `RobotState` enum + the transition table (§3.10)
- `Affect` enum + affect blending rules
- `Fact`, `FactKind`, `Confidence`, supersession rules (§7.8)
- `Gesture` — a declarative, hardware-independent motion description
- `Trigger` and trigger evaluation predicates

Why this matters: the transition table in §3.10 is testable as a pure function. Ninety percent of the bugs in a system like this are illegal state transitions, and here they're caught by a table-driven unit test that runs in 4 ms.

### 3.4.3 Application layer (`robot/services/`)

Use-case orchestration. Async. Depends on domain + ports. Knows nothing concrete.

Each service:
- subscribes to some events,
- calls ports,
- publishes other events.

That's the whole shape. A service that does something else is misfiled.

### 3.4.4 Adapter layer (`robot/adapters/`)

Two implementations of every port, minimum. Naming: `RealX` / `FakeX`. The fake is not a test double bolted on afterward — it is a first-class deliverable used by the simulator (§14.5) and it ships in the repo, not in `tests/`.

## 3.5 The event-driven core

### 3.5.1 Rationale

Why not just call methods? Because of UC-01: the AI produces a happy response, and *three* things must happen — display changes, servo nods, affect state updates. With direct calls, `ai` imports `display`, `motion`, and `state`. Add a fourth consumer (say, an LED) and you edit `ai`. That's the exact coupling that makes month-6 work slow.

With the bus, `ai` publishes `affect.changed` and stops caring. Adding the LED is a new subscriber and zero edits to existing modules. This is the Open/Closed principle with a mechanism attached.

The cost is real and must be acknowledged: **control flow becomes non-obvious.** You cannot read the code top-to-bottom and know what happens. Mitigations, all mandatory:
- The event catalog (§9.1) is normative and complete — one place lists every event and every subscriber.
- Every publish and every dispatch is traced with a correlation ID (§3.12.2), so a turn is reconstructible from logs.
- CI generates the subscriber graph from the registry and fails if it drifts from the documented catalog.

### 3.5.2 Event bus contract

```python
# robot/core/ports.py

@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: Event) -> None:
        """Fire-and-forget. Returns once the event is queued, not handled.
        Never raises due to a subscriber failure."""

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
    ) -> Subscription:
        """Register a handler. `name` is mandatory: it appears in the
        generated subscriber graph and in trace logs."""
```

Key contract points, each chosen deliberately:

- **`publish` is not awaited to completion.** If the audio service had to await the display renderer, the display would be in the latency path of speech. It isn't and must never be.
- **Subscriber exceptions are swallowed and logged**, then published as `system.handler_failed`. A crashing display renderer must not kill a conversation. This is the single most important reliability property in the system.
- **Handlers are dispatched concurrently**, not sequentially. Two subscribers to `affect.changed` (display, motion) run in parallel.
- **Subscription is static.** All subscriptions are registered at composition time in `main.py`. Dynamic subscription at runtime is forbidden — it makes the subscriber graph unknowable, which forfeits the mitigation above.

### 3.5.3 Event taxonomy

Naming: `<domain>.<past_tense_verb>`. Never `display.set_emotion` — that's a command wearing an event's clothes. Correct: `affect.changed`, and the display decides what to do about it.

| Domain | Events (v1) |
|---|---|
| `system` | `started`, `shutting_down`, `handler_failed`, `degraded_entered`, `degraded_exited` |
| `audio` | `speech_started`, `speech_ended`, `playback_started`, `playback_finished` |
| `conversation` | `turn_started`, `user_transcribed`, `assistant_responded`, `turn_ended`, `session_lost` |
| `affect` | `changed` |
| `state` | `transitioned` |
| `vision` | `presence_gained`, `presence_lost`, `face_detected` |
| `memory` | `fact_stored`, `fact_superseded`, `fact_deleted`, `recall_completed` |
| `behavior` | `trigger_fired`, `proactive_suppressed`, `proactive_delivered` |
| `motion` | `gesture_started`, `gesture_completed`, `gesture_preempted` |

Every event carries: `event_id`, `correlation_id`, `timestamp`, `source` (the publishing component name). These are on the `Event` base class and are not optional.

### 3.5.4 Delivery semantics

Stated plainly so nobody assumes otherwise:

- **At-most-once.** In-memory, no persistence. If the process dies, queued events are gone.
- **Ordered per publisher**, unordered globally.
- **No replay, no dead-letter queue.**

Is that acceptable? For everything except memory writes, yes — a lost `affect.changed` costs one frame of a wrong face. For memory writes it is **not** acceptable, and therefore **memory writes do not go through the bus.** `MemoryService.store_fact()` is a direct awaited call from the tool handler, durable before it returns, and it *then* publishes `memory.fact_stored` for anyone who cares. 

This is the key architectural subtlety of the whole design: **the bus carries notifications, not obligations.** If losing it would be a correctness bug, it's a direct call. If losing it is a cosmetic glitch, it's an event. Apply that test whenever adding an event.

### 3.5.5 Backpressure

Each subscriber has a bounded queue (default 100). On overflow, policy per subscriber:

| Policy | Used by | Behavior |
|---|---|---|
| `DROP_OLDEST` | display, motion | Latest affect wins; stale frames are worthless |
| `DROP_NEWEST` | logging | Preserve the beginning of an incident |
| `BLOCK` | *forbidden* | Would propagate backpressure into the audio path |

A queue hitting its bound publishes `system.handler_failed` with a `queue_overflow` reason and increments a counter. Silent drops are a debugging catastrophe; loud drops are a tuning signal.

## 3.6 Component view

```
┌───────────────────────────────────────────────────────────────────────┐
│                              main.py                                  │
│                        (composition root)                             │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ constructs & wires
    ┌───────────────────────────┼───────────────────────────┐
    │                           │                           │
┌───▼──────────┐    ┌───────────▼──────────┐    ┌───────────▼─────────┐
│  core        │    │     services         │    │     adapters        │
│              │    │                      │    │                     │
│  EventBus    │◄───┤ ConversationService  ├───►│ RealtimeClient      │
│  StateMgr    │◄───┤ MemoryService        ├───►│ SqliteFactRepo      │
│  Config      │◄───┤ AffectService        │    │ FramebufferDisplay  │
│  Lifecycle   │◄───┤ PresenceService      ├───►│ Picamera2Camera     │
│              │◄───┤ ExpressionService    ├───►│ Pca9685Servo        │
│              │◄───┤ BehaviorService      │    │ AlsaMicrophone      │
│              │◄───┤ AudioService         ├───►│ AlsaSpeaker         │
└──────────────┘    └──────────┬───────────┘    └─────────────────────┘
                               │ depends on
                    ┌──────────▼───────────┐
                    │       domain         │
                    │  Event │ Fact │ ...  │
                    └──────────────────────┘
```

### 3.6.1 Component inventory

| Component | Responsibility (one sentence — if it needs two, split it) | Subscribes | Publishes |
|---|---|---|---|
| `AudioService` | Move PCM between devices and the AI client without ever blocking. | — (assistant audio is a direct `TurnSink` call, §9.1.4) | `audio.*` |
| `ConversationService` | Own the Realtime session lifecycle and translate its events into ours. | `audio.speech_*`, `behavior.trigger_fired` | `conversation.*` |
| `MemoryService` | Be the sole writer and reader of persistent user knowledge. | — (direct calls) | `memory.*` |
| `AffectService` | Decide the robot's emotional state from all available signals. | `conversation.*`, `vision.*` | `affect.changed` |
| `ExpressionService` | Turn one affect into display frames. | `affect.changed`, `state.transitioned` | — |
| `MotionService` | Turn one affect or gesture request into servo movement, safely. | `affect.changed` | `motion.*` |
| `PresenceService` | Decide whether a human is present, with hysteresis. | — (polls camera port) | `vision.*` |
| `BehaviorService` | Decide when the robot should speak first. | `vision.*`, `memory.*`, clock | `behavior.*` |

Note `AffectService` and `ExpressionService` are separate. The temptation is to merge them. Don't: *deciding* to be happy is domain logic with unit tests; *getting a happy face onto glass* ends in an adapter, and adapters carry device dependencies — a framebuffer, a panel driver, whatever the hardware of the day demands. Merging them puts that dependency in the chain of your emotion tests. That's exactly the mistake P1 exists to prevent. The split holds whatever the display backend is; see §3.6.4.

### 3.6.4 Face rendering — ADR-012

**Faces compose to `RGB888` bytes in the stdlib.** No drawing-library dependency.

The `Display` port already draws the line this decision rests on: *"Show a **frame**, not a screen. The port never promised a framebuffer — only pixels"* (`core/ports.py`). `ExpressionService`'s job ends when an affect has become a buffer of pixels; carrying those pixels to glass is the adapter's problem. Composition is therefore backend-independent by construction, and the question "which drawing library?" is a question about *composition* only.

At 480×320 the answer is: none. Faces are sprites, not vector art (§2.4 — "design for the pixel grid"), and composing sprites into a byte buffer is array arithmetic. This codebase has twice chosen the stdlib over a dependency for exactly this kind of work and both have held: AVID-13's PNG encoder (`zlib` + `struct`) and AVID-55's `_to_xrgb8888` channel reorder (extended slices, no numpy, no per-pixel loop). Runtime dependencies stay **`pydantic` alone**.

Consequences, accepted:

- Sprites are hand-composed `bytes`. There is no blitter, no alpha compositor, and no font engine unless we write one — which bounds how ornate a face can get, deliberately.
- The domain-purity contract's `pygame` entry (`.importlinter`) becomes a standing rule rather than an aspiration.

**This is not a bet against HDMI.** The panel we have today is an SPI ILI9486 DRM device, which is why `FramebufferDisplay` pushes XRGB8888 at `/dev/fbN` (AVID-55) and why a pygame `flip()` would have nothing to drive — but that is *evidence*, not the premise. Plug in an HDMI screen later and exactly one thing changes: a new adapter beside `FakeDisplay` and `FramebufferDisplay`, a new value in the `[adapters] display` literal, a case in `main._build_display`. Sprites, `ExpressionService`, and `AffectService` are untouched. If that adapter wants pygame or SDL, it takes it as an adapter-local optional dependency, the way `picamera2` and `pyalsaaudio` already do (§3.11.2) — nothing here forbids it. What this ADR forbids is a drawing library in the *composition* path, where the port guarantees it buys nothing.

*Rejected: Pygame.* Three concrete costs, none of them paid for a feature we need. It would run **headless** (`SDL_VIDEODRIVER=dummy`) as a pure off-screen buffer against today's framebuffer target — the whole windowing and event layer, inert. It has **no honest home in the dependency groups**: not the `pi` extra, since the future simulator wants faces off-Pi too; not the main dependencies, since it is one backend's implementation detail. And it needs a **mypy override** to join the unstubbed-import block. A `zlib`-sized amount of stdlib code avoids all three.

## 3.7 Runtime views

### 3.7.2 Wake → listen → respond (UC-01)

```
User    Mic    AudioSvc   ConvSvc   Realtime   AffectSvc  ExprSvc  MotionSvc
 │       │         │         │          │          │         │         │
 │ speak │         │         │          │          │         │         │
 ├──────►│ frames  │         │          │          │         │         │
 │       ├────────►│         │          │          │         │         │
 │       │         │ local VAD: speech? │          │         │         │
 │       │         ├────────►│          │          │         │         │
 │       │         │  audio.speech_started         │         │         │
 │       │         │         │ ensure_session()    │         │         │
 │       │         │         ├─────────►│          │         │         │
 │       │         │  stream PCM (async, unawaited)│         │         │
 │       │         ├───────────────────►│          │         │         │
 │       │         │         │          │          │         │         │
 │       │         │         │  state.transitioned(LISTENING)│         │
 │       │         │         ├─────────────────────┼────────►│         │
 │       │         │         │          │          │  draw listening   │
 │       │         │         │          │          │         │         │
 │       │         │         │◄─ server VAD: turn end         │         │
 │       │         │         │  state.transitioned(THINKING) ►│         │
 │       │         │         │          │          │         │         │
 │       │         │         │◄─ audio delta stream            │        │
 │       │         │◄────────┤          │          │         │         │
 │◄──────┼─────────┤ playback│          │          │         │         │
 │       │         │         │ conversation.assistant_responded         │
 │       │         │         ├─────────►│          │         │         │
 │       │         │         │          │  infer affect       │         │
 │       │         │         │          │  affect.changed     │         │
 │       │         │         │          ├─────────┬─────────►│         │
 │       │         │         │          │         │  HAPPY   │  nod    │
 │       │         │         │          │         ├─────────►│────────►│
```

Read the two parallel arrows at the bottom: that fan-out is why the bus exists. Neither `ExprSvc` nor `MotionSvc` knows the other exists, and `ConvSvc` knows neither.

### 3.7.3 Memory write during conversation (UC-02)

```
Realtime          ConvSvc          MemoryService        SQLite         Bus
   │                 │                   │                │            │
   │ tool_call:      │                   │                │            │
   │ remember_fact(  │                   │                │            │
   │  "coffee 8am")  │                   │                │            │
   ├────────────────►│                   │                │            │
   │                 │ await store_fact()│                │            │
   │                 ├──────────────────►│                │            │
   │                 │                   │ normalize      │            │
   │                 │                   │ dedupe check   │            │
   │                 │                   ├───────────────►│            │
   │                 │                   │ supersede old? │            │
   │                 │                   ├───────────────►│            │
   │                 │                   │ INSERT (WAL)   │            │
   │                 │                   ├───────────────►│            │
   │                 │                   │◄─ committed    │            │
   │                 │◄─ FactId ─────────┤                │            │
   │                 │                   │ memory.fact_stored          │
   │                 │                   ├────────────────┼───────────►│
   │◄ tool_result ───┤                   │                │            │
   │  {"ok": true,   │                   │                │   BehaviorSvc
   │   "id": 42}     │                   │                │   registers
   │                 │                   │                │   trigger
```

Note the ordering: the fact is **committed before the tool result returns**. The model is told "yes, I remembered" only when that is true. And `BehaviorService` learns about the new recurring fact by subscribing to `memory.fact_stored` — `MemoryService` has no idea the behavior engine exists. That's how UC-02 becomes UC-03 with zero coupling between them.

### 3.7.6 Network loss → degraded → recovery (UC-06)

```
ConvSvc          Realtime WS        Bus              ExprSvc      AudioSvc
   │                  │              │                  │             │
   │◄─ WS closed ─────┤              │                  │             │
   │                  │              │                  │             │
   │ conversation.session_lost       │                  │             │
   ├─────────────────────────────────┤                  │             │
   │ system.degraded_entered         │                  │             │
   ├─────────────────────────────────┼─────────────────►│             │
   │                  │              │   draw CONFUSED  │             │
   │                  │              │                  │             │
   │ play canned "one sec, I lost my connection" (local WAV)          │
   ├─────────────────────────────────┼──────────────────┼────────────►│
   │                  │              │                  │             │
   │ reconnect w/ exponential backoff + jitter (1s→30s cap)           │
   ├─────────────────►│              │                  │             │
   │◄─ connected ─────┤              │                  │             │
   │ restore session instructions + memory context                    │
   ├─────────────────►│              │                  │             │
   │ system.degraded_exited          │                  │             │
   ├─────────────────────────────────┼─────────────────►│             │
```

Three things this diagram commits us to, all of which must exist in code:
1. **A local WAV bank** for degraded speech. Small, unglamorous, essential — the robot must be able to say *something* with zero network.
2. **Session restoration is a first-class operation**, not reconnect-and-hope. The instruction block + injected memory must be re-sent (§6.7).
3. **Degraded mode is a visible state**, not a silent one. The user sees CONFUSED and knows.

## 3.8 Concurrency model

### 3.8.1 One loop

Single process, single `asyncio` event loop, per ADR-002. Rationale: the workload is I/O-bound (sockets, serial, files) with one CPU-bound exception (vision). Multiprocessing would buy little and cost a lot in IPC complexity and debuggability, for one developer.

### 3.8.2 Thread-offload policy

Blocking calls get a thread. Non-negotiable list:

| Work | Why it blocks | Mechanism |
|---|---|---|
| `cv2` / MediaPipe inference | CPU-bound, releases GIL | `run_in_executor`, dedicated single-thread pool, ≤5 fps |
| Picamera2 capture | Blocking C call | Same pool as above |
| Framebuffer write (`FramebufferDisplay`) | Blocking device write | `asyncio.to_thread` per `render`; XRGB8888 straight to `/dev/fbN` (AVID-55). *Composition is not on this list — `ExpressionService` builds frames in pure stdlib byte work (ADR-012, §3.6.4), so only the panel push offloads. The offload question is per-adapter: this panel is a DRM framebuffer with no `flip()` vsync to wait on, where an HDMI/SDL adapter would bring its own blocking present and earn its own row.* |
| SQLite writes | Disk fsync on a slow SD card | `asyncio.to_thread`, serialized through one writer |
| I2C to PCA9685 | Blocking syscall, sub-ms | `asyncio.to_thread`; short enough to be uncontroversial |
| ALSA read/write | Blocking | Dedicated capture and playback threads with ring buffers |

Enforcement (P8): CI runs the whole test suite with `PYTHONASYNCIODEBUG=1` and treats any slow-callback warning >50 ms as a failure. This catches the accidental blocking call *at the moment it's introduced*, which is the only time it's cheap to fix. One narrow carve-out (AVID-57): on the Pi, a real device's one-time `start()`/`stop()` (libcamera pipeline init, ALSA card open) is offloaded via `asyncio.to_thread`, so the loop runs no blocking code — but the fixture's own trivial resume callback is measured with inflated wall-clock time while that multi-threaded init contends for the Pi's cores. The gate exempts *only* that fixture setup/teardown boundary, *only* on a real-hardware run; the identical `start`/`stop`/`capture` also run in gated test bodies, so a genuinely loop-blocking adapter still fails, and fake/CI runs stay fully strict (`tests/conftest.py`).

### 3.8.4 Shared state rules

- Services own their state privately. No service reads another's attributes.
- The only shared mutable object is `StateManager`, and it is guarded by an `asyncio.Lock` and mutated exclusively through `transition()`.
- Anything crossing a thread boundary goes through `janus`-style queues or `loop.call_soon_threadsafe`. Never a bare shared dict.

## 3.9 Hardware Abstraction Layer

### 3.9.1 Port contracts

The HAL is the reason G6 is achievable. Every physical device is behind a `Protocol` in `robot/core/ports.py`. The Protocol is defined by *what the application needs*, never by what the device offers — that inversion is the entire value.

> As implemented (AVID-11), every HAL port is decorated `@runtime_checkable` — the composition root and the contract suite assert an object is port-shaped. `isinstance` verifies member *presence* only; the type checker enforces the signatures below.

```python
class Camera(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def capture(self) -> Frame:
        """Latest frame. Never blocks the loop. May return a repeat frame."""
    @property
    def capabilities(self) -> CameraCaps: ...


class Servo(Protocol):
    async def move_to(self, channel: int, angle_deg: float, *, duration_ms: int) -> None:
        """Move smoothly. MUST clamp to configured safe limits internally.
        MUST be cancellable — a preempting gesture cancels this task."""
    async def relax(self, channel: int) -> None:
        """De-energize. Prevents servo buzz and heat when idle."""
    @property
    def axes(self) -> tuple[Axis, ...]: ...


class Display(Protocol):
    async def render(self, frame: DisplayFrame) -> None: ...
    @property
    def resolution(self) -> tuple[int, int]: ...


class Microphone(Protocol):
    def stream(self) -> AsyncIterator[AudioChunk]: ...


class Speaker(Protocol):
    async def play(self, chunk: AudioChunk) -> None: ...
    async def play_file(self, path: Path) -> None: ...   # degraded-mode WAV bank
    async def stop(self) -> None: ...                     # barge-in


class VoiceActivityDetector(Protocol):   # §6.3 / ADR-007 — the session gate
    def is_speech(self, frame: AudioChunk) -> bool: ...  # sync; MUST return <5 ms (§9.3)


class RealtimeClient(Protocol):          # the vendor boundary (§6.2, R-10)
    async def open(self) -> None: ...                          # cold session; no resumption (§6.2.3)
    async def aclose(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ... # mic PCM up
    def events(self) -> AsyncIterator[RealtimeEvent]: ...      # neutral, typed, vendor-free
    async def truncate(self, item_id: str, audio_end_ms: int) -> None: ...  # barge-in (§6.2.4)
    async def cancel(self) -> None: ...                        # response.cancel


class TurnSink(Protocol):                # the ConvSvc↔AudioSvc audio seam (§9.1.4)
    def mic(self) -> AsyncIterator[AudioChunk]: ...            # captured PCM up
    async def play(self, chunk: AudioChunk, *, item_id: str) -> None: ...  # assistant PCM down
    async def end_response(self) -> None: ...  # normal completion → playback_finished, SPEAKING→IDLE
    async def interrupt(self) -> int: ...  # barge-in; returns played_ms ACTUALLY emitted (§6.2.4)
```

`RealtimeClient` and `TurnSink` are the two M5 ports (AVID-100). `RealtimeClient` is the vendor blast radius: `ConversationService` depends only on it, the `openai`/`replay` adapters implement it, and it traffics in the neutral `RealtimeEvent` union (`UserTranscript` / `AssistantAudioChunk` / `AssistantTranscript` / `TurnDone(usage: TokenUsage)` / `SessionClosed`, defined in `core/realtime.py`) so no Realtime message shape ever crosses — if OpenAI changes the API, exactly one adapter changes (R-10). `TurnSink` is how a turn's PCM crosses `ConvSvc ↔ AudioSvc` as a **direct call, never a bus event** (§9.1.4).

Three details worth defending:

**`Servo.move_to` clamps internally.** You could argue clamping belongs in the service. It doesn't: the safety limit is a property of the physical linkage, and if it lives in the service, a future second caller bypasses it. Safety invariants belong at the lowest layer that can enforce them universally.

**`Servo.axes` and `Camera.capabilities` exist** because of §3.9.3 — the system must run on a 1-servo rig, a 2-servo rig, and a simulator with 6, without conditionals scattered through the services.

**`TurnSink.interrupt()` returns `played_ms`, not `None`.** Barge-in (§6.2.4) needs `audio_end_ms` to be *what the speaker actually emitted*, and only the sink at the bottom of the playback path knows that — it differs from what we received by the entire buffer depth. Returning it from `interrupt()` puts the honest figure at the one layer that can measure it, exactly as `Servo.move_to` clamps at the one layer that owns the limit. Get it wrong and the model believes it said things the user never heard, which then poisons the conversation context. The method is named `interrupt`, not `stop`, because the real sink (`AudioService`, AVID-103) is also a `Service`, whose `stop()` unwinds the mic loop — a lifecycle shutdown is a different act from cutting a turn's playback, and the two must not collide. Its sibling `end_response()` is the *normal* end (no barge-in): the service calls it on `response.done`, and the sink then publishes `audio.playback_finished(truncated=False)` and drives `SPEAKING → IDLE`.

### 3.9.2 Simulator adapters

`FakeCamera` replays a video file or generates synthetic frames with a scriptable "person present" flag. `FakeServo` records a movement trace, assertable in tests. `FakeDisplay` renders to a PNG sequence you can eyeball in CI artifacts. `FakeMicrophone` streams a WAV. `FakeSpeaker` writes a WAV.

Together these are the simulator (§14.5). Which means the simulator is not a separate program — it's `main.py` with a different config file. That's a design decision with a big payoff: the simulator can never drift from the real system, because it *is* the real system.

### 3.9.3 Capability negotiation

At startup, each adapter reports capabilities. `MotionService` asks: do I have a tilt axis? If yes, `nod` is a tilt gesture. If no, `nod` degrades to a small pan wiggle. The *gesture* stays in the domain layer as an intent; the *realization* is negotiated by the adapter's capabilities. This is what lets ADR-009 stay open without blocking Phase 3–8 work.

## 3.10 State management

### 3.10.1 Global state machine

`RobotState` is operational. It is **not** emotion — that's `Affect`, and they are orthogonal. The robot can be SLEEPING-and-content or LISTENING-and-confused. Conflating them is the most common design error in this class of project and it produces an unmaintainable combinatorial mess.

```
                 ┌─────────┐
      ┌─────────►│ BOOTING │
      │          └────┬────┘
      │               │ all adapters ready
      │          ┌────▼────┐  no presence 10 min   ┌──────────┐
      │          │  IDLE   ├──────────────────────►│ SLEEPING │
      │          │         │◄──────────────────────┤          │
      │          └──┬───▲──┘  presence | speech    └──────────┘
      │             │   │
      │  speech     │   │ turn ended
      │  detected   │   │
      │          ┌──▼───┴───┐
      │          │LISTENING │
      │          └────┬─────┘
      │               │ turn end detected
      │          ┌────▼─────┐
      │          │ THINKING │
      │          └────┬─────┘
      │               │ first audio delta
      │          ┌────▼─────┐
      │          │ SPEAKING │
      │          └────┬─────┘
      │               │ playback finished
      │               └──────────► IDLE
      │
      │          ┌──────────┐
      └──────────┤ DEGRADED │◄──── from any state on session_lost
                 └──────────┘
```

### 3.10.3 Transition table

Normative. Implemented as a frozen dict in `domain/state.py`, tested exhaustively as a pure function. Any transition not in this table raises `IllegalTransition` — loudly, in tests; logged-and-ignored in production.

| From | Event | To | Guard |
|---|---|---|---|
| BOOTING | `system.started` | IDLE | all required adapters healthy |
| IDLE | `audio.speech_started` | LISTENING | — |
| IDLE | `behavior.trigger_fired` | THINKING | not quiet hours; §10.4 passes |
| IDLE | `vision.presence_lost` + 10 min | SLEEPING | — |
| SLEEPING | `vision.presence_gained` | IDLE | — |
| SLEEPING | `audio.speech_started` | LISTENING | — |
| LISTENING | `conversation.user_transcribed` | THINKING | — |
| LISTENING | timeout 30 s | IDLE | — |
| THINKING | `audio.playback_started` | SPEAKING | — |
| THINKING | timeout 10 s | DEGRADED | — |
| SPEAKING | `audio.playback_finished` | IDLE | — |
| SPEAKING | `audio.speech_started` | LISTENING | barge-in: stop playback first |
| *any* | `conversation.session_lost` | DEGRADED | — |
| DEGRADED | `system.degraded_exited` | IDLE | — |

The barge-in row is the one that will bite you. The user interrupting the robot mid-sentence is *the* interaction that separates a companion from a kiosk, and it requires `Speaker.stop()` to be genuinely immediate — which is why it's on the port (§3.9.1) rather than being someone's afterthought.

## 3.11 Deployment view

### 3.11.1 Development topology (PC)

Python 3.13, UV-managed venv, all Fake adapters, no hardware, no network required (Realtime client also has a fake that replays recorded sessions). `uv run avid --config config/sim.toml` gives a running robot on your laptop, with `FakeDisplay` writing each frame as a PNG under `[display] frames_dir` in place of a panel (§3.9.2) — the face is a sequence of files you can flip through, not a window.

### 3.11.2 Target topology (Pi 5) — ADR-008

This is the sharp edge flagged at the top of this document.

The problem: `picamera2` is an apt package (`python3-picamera2`) built against the system interpreter and linking `libcamera` bindings that are not reliably pip-installable. A clean UV-managed Python 3.13 venv on the Pi will not see it.

The decision:

| Target | Interpreter | Env |
|---|---|---|
| PC (dev, test, CI) | 3.13 via UV | Pure UV venv, all fakes |
| Pi (integration, prod) | System 3.11 | `uv venv --system-site-packages`, pinned via a Pi-specific lock |

Consequences, accepted:
- `pyproject.toml` declares `requires-python = ">=3.11"`, not `>=3.13`. No 3.12+ syntax in shipped code.
- CI runs the suite on **both** 3.11 and 3.13. A 3.13-only feature breaking the Pi build is caught in CI, not on the desk.
- `picamera2` is an optional dependency group, imported *only* inside `RealPicamera2Camera`. The import is inside the adapter module, which is imported only by the composition root, which only imports it when config says to. Nothing else in the codebase can accidentally depend on it.

That last point is the payoff of P1 and P3 in a concrete, unglamorous, load-bearing way: an ugly platform constraint is contained inside one file instead of infecting the project's Python version globally.

*Alternative considered:* run camera capture in a separate system-Python process, IPC over a Unix socket, keeping the main app on 3.13. Rejected for v1 — real complexity to buy a language-version preference. Revisit if 3.13-only features become compelling. Recorded as ADR-008.

### 3.11.3 Process layout

```
systemd
  └── robot.service        Type=notify, Restart=always, RestartSec=5
        └── python -m robot --config /etc/robot/config.toml
              ├── main loop (asyncio)
              ├── thread: vision executor
              ├── thread: framebuffer write (display)
              ├── thread: audio capture
              ├── thread: audio playback
              └── thread: sqlite writer
```

`Type=notify` with an `sd_notify` watchdog ping from the loop: if the event loop wedges, systemd restarts us. Cheap insurance against the exact failure that would otherwise mean a dead robot until you notice.

Supervision is a port, not a bare syscall: the run loop announces `ready()` on reaching IDLE, pings `watchdog()` on `[systemd] watchdog_interval_s`, and announces `stopping()` on shutdown, all through the `ServiceNotifier` protocol (`core/ports.py`). The real adapter (`SystemdNotifier`) writes `READY=1`/`WATCHDOG=1`/`STOPPING=1` to `$NOTIFY_SOCKET`; the fake records the calls and *is* the simulator (P6), so the laptop profile — `notifier = "fake"`, no supervisor — runs the identical sequence. CI proves the whole handshake end-to-end against a stand-in socket (`tests/e2e/test_supervision.py`); the physical restart-on-kill is a Pi demo (M1 gate).

## 3.12 Cross-cutting concerns

### 3.12.2 Observability

Every event carries a `correlation_id`, minted at the head of a turn (`audio.speech_started` or `behavior.trigger_fired`) and propagated to every downstream event. One `grep` on a correlation ID reconstructs the entire turn across all seven services. Given the control-flow opacity that §3.5.1 admits the bus creates, this isn't a nice-to-have — it's the mitigation that makes the bus affordable.

Logging: structured JSON via `structlog`, to journald, **not to the SD card** (§2.7.1 — the card is the constraint). `journald` with `Storage=volatile` and a memory cap; ship to a host over the network if you want history.

Metrics: an in-process registry exposed on the local control API (§9.5). Turn latency histogram, API cost counter, event queue depths, servo duty cycle, frame rate, SD write bytes. If you can't see cost per day on a dashboard, §2.7.2 will surprise you at the end of the month.

### 3.12.3 Error taxonomy

| Class | Example | Policy |
|---|---|---|
| **Transient external** | WS drop, API 429 | Retry with backoff; enter DEGRADED after N; never surface a trace |
| **Permanent external** | 401 bad key | Fail fast at boot; refuse to start; loud log |
| **Hardware absent** | No camera at boot | Degrade capability; log; continue. A missing camera must not stop conversation. |
| **Hardware fault** | I2C NACK mid-gesture | Abort gesture, relax servo, publish `motion.gesture_preempted`, continue |
| **Programmer error** | Illegal state transition | Raise in dev/test; log + swallow in prod |
| **Resource** | Queue overflow | Drop per policy, count, log |

The through-line: **nothing except a bad API key at boot is allowed to stop the robot.** A companion that crashes is worse than a companion that's briefly stupid.

---

---

# 6. AI Pipeline

> **Volatility warning.** Every price and model name in this section was verified against OpenAI's live documentation and pricing pages in **mid-July 2026**. The Realtime family renamed itself from `gpt-4o-realtime` → `gpt-realtime` (GA, Aug 2025) → `gpt-realtime-2` → `gpt-realtime-2.1` (Jul 2026) inside twelve months. **Pin dated snapshots in production.** Re-verify §6.10 before relying on it for a budget decision. The design in §6.1–§6.9 is intended to survive this churn; the numbers are not.

## 6.1 Pipeline overview

```
  ┌──────────┐   PCM16 24k   ┌──────────┐  speech?  ┌─────────────┐
  │   Mic    ├──────────────►│  Silero  ├──────────►│  Session    │
  │ (USB UAC)│   20ms frames │   VAD    │           │   Gate      │
  └──────────┘               └──────────┘           └──────┬──────┘
                                  ▲                        │ open, with
                                  │ local, ~1ms/frame      │ pre-injected
                                  │ zero cost              │ memory
                                  │                        ▼
  ┌──────────┐               ┌──────────┐           ┌─────────────┐
  │ Speaker  │◄──────────────┤ Playback │◄──────────┤  Realtime   │
  │(MAX98357)│   PCM16 24k   │  Buffer  │  deltas   │   Session   │
  └──────────┘               └────┬─────┘           │  (WSS)      │
                                  │                 └──┬───────┬──┘
                          barge-in│ truncate()         │       │
                                  ▼                    │       │
                            conversation.item     tool │       │ transcript
                              .truncate           calls│       │ deltas
                                                       ▼       ▼
                                            ┌──────────────┐ ┌──────────┐
                                            │ Tool Router  │ │  Affect  │
                                            │ remember/    │ │ Inference│
                                            │ recall/forget│ └──────────┘
                                            └──────┬───────┘
                                                   ▼
                                            MemoryService
```

Read the diagram for what it *doesn't* contain. There is no wake-word engine. There is no local LLM. There is no STT→LLM→TTS chain. The model does speech-to-speech in one hop, and everything we build sits at the two edges: **the gate in front of it** (§6.3, which is a cost mechanism) and **the tools behind it** (§6.6, which is where memory lives per ADR-004).

## 6.2 OpenAI Realtime session lifecycle

### 6.2.1 Transport — ADR-010

**WebSocket.** Not WebRTC.

OpenAI's docs position WebRTC for browser/mobile clients on unreliable networks, and WebSocket for server-to-server backends with consistent low-latency links. The Pi is a trusted device we fully control on a home LAN — that's the server case. We use a server-side API key directly and skip the ephemeral client-secret dance entirely.

What WebSocket costs us, stated plainly so it isn't a surprise in M5: **with WebRTC the server tracks played audio and truncates automatically on interruption; with WebSocket we must do it ourselves** (§6.2.4). That is real work and it is the single fiddliest part of Phase 5. We accept it because the alternative is NAT traversal and ICE on an embedded device to solve a problem we don't have.

*Rejected: SIP.* Phone/PSTN transport. Irrelevant.

### 6.2.2 Session parameters

```python
# adapters/realtime/session.py — the shape, not the code

SESSION_CONFIG = {
    "model": "gpt-realtime-mini-2025-12-15",   # PINNED. See §6.10.
    "instructions": composed_instruction_block,     # §6.4 — STATIC for session life
    "audio": {
        "input": {
            "format": "pcm16",                      # 24 kHz mono 16-bit
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
                "interrupt_response": True,
            },
        },
        "output": {"format": "pcm16", "voice": "cedar"},
    },
    "tools": TOOL_SCHEMA,                            # §6.6 — STATIC for session life
    "max_output_tokens": 512,                        # §6.10 — a cost guardrail, not a style choice
    "truncation": "auto",
}
```

Notes on the non-obvious choices:

- **`model` and `voice` are fixed at connect time.** Everything else is updatable via `session.update`. This matters for §6.5: you cannot A/B two voices inside one session.
- **`instructions` and `tools` are static for the session's life.** This is not laziness — it is the entire cost strategy (§6.10). They form the cacheable prefix. Mutating them mid-session throws away the ~98.75% caching discount on every subsequent turn.
- **`voice: cedar`** — Cedar and Marin are GA voices exclusive to the Realtime API.
- **No `temperature`.** GA removed it. Any tutorial you find that sets it predates August 2025.
- **`max_output_tokens: 512`** ≈ 25 seconds of speech. A companion that monologues for 25 seconds is already broken; this is a backstop against a pathological response, not a target.

### 6.2.3 Lifecycle and the 60-minute wall

Two published limits shape this design:

| Limit | Value | Consequence |
|---|---|---|
| Max session duration | **60 minutes** | Sessions must be rolled over |
| Session resumption | **Does not exist** | A dropped connection means a *new* session |
| Ephemeral token TTL | 1 minute | N/A for us — we use a server key (§6.2.1) |

"Sessions cannot be resumed" is the load-bearing fact. There is no reconnect-and-continue. Every reconnection is a cold session that must be re-seeded with instructions + tools + injected memory (§6.7). The §3.7.6 diagram's "restore session instructions + memory context" step is therefore not a nicety — **it is the only way the robot doesn't get amnesia every time the Wi-Fi hiccups.**

Because we gate sessions (§6.3), the 60-minute wall is mostly theoretical for us: a session opens when you speak and closes ~30 s after you stop. A session reaching 60 minutes means you've been talking continuously for an hour, at which point rolling over mid-conversation is the least of anyone's problems. We implement rollover anyway (open new session at T+55min, re-seed, swap, close old) because the failure mode otherwise is silent and confusing.

### 6.2.4 Barge-in — the hard part

Per §3.10.3, `SPEAKING + audio.speech_started → LISTENING` is the transition that makes this feel like a companion rather than a kiosk. On WebSocket, we own it:

```
1. Local VAD fires while state == SPEAKING
2. Speaker.stop()                    ← immediate; must be on the port (§3.9.1)
3. Compute audio_end_ms = how much of the assistant item ACTUALLY played
   (bytes written to ALSA ÷ 48 bytes/ms at 24kHz mono 16-bit)
4. Send conversation.item.truncate(item_id, content_index, audio_end_ms)
5. Send response.cancel
6. Mute inbound deltas for item_id until the next assistant item begins
```

Three traps, all of which will cost you an afternoon each if you meet them cold:

- **Step 6 is not optional.** Audio deltas already in flight keep arriving *after* truncation. Without muting by item ID you will hear the robot's cancelled sentence resume for ~200 ms after it should have stopped.
- **Step 3 must measure what the speaker played, not what we received.** Those differ by the entire playback buffer depth. Getting this wrong makes the model believe it said things the user never heard — which then poisons the conversation context.
- **`conversation.item.truncate` also drops the transcript for the unplayed portion.** Audio/transcript alignment is imprecise, so the transcript you keep for memory extraction (§7.6) is approximate at the truncation boundary. Don't build anything that assumes it's exact.

## 6.3 The session gate — ADR-007, accepted

**Decision:** No Realtime session is opened until local VAD detects speech. There is no persistent connection.

This was "Proposed" in §3.3. The numbers in §6.10 promote it to Accepted, and they are not close.

**Mechanism:** Silero VAD, ~1 MB ONNX model, sub-millisecond per 30 ms frame on a Cortex-A76, running on every frame from the `Microphone` port. Zero API cost. Zero extra hardware. It discriminates *speech* from noise — which a loudness threshold cannot do, and which is the whole point: a door slam that opens a billable session is worse than useless.

```
IDLE:  mic streaming → Silero → not speech → drop frame. Cost: $0.
       Silero → SPEECH → open session (~200ms) → replay the 300ms
       pre-speech ring buffer → stream live. Cost: begins now.
IDLE after 30s of no speech: close session. Cost: $0 again.
```

**The ring buffer is why this works.** We keep the last 300 ms of audio at all times. When VAD fires, we've already missed the first phoneme or two — so we replay the buffer into the session before the live stream. Without it, every utterance loses its first word and the gate is unusable.

**Cost of the gate:** ~200 ms of session-open latency on the first utterance of a conversation, paid once per conversation rather than per turn (the session stays open between turns). Against the §2.8.1 budget this pushes first-turn P50 to ~810 ms — marginally over the 800 ms target, in range on subsequent turns. §6.9 covers this with the thinking-cue mitigation.

**Value of the gate:** see §6.10. It is the difference between $4/month and $108/month.

## 6.4 Instruction composition architecture

The instruction block is assembled once per session, at open, from four layers. It is then **frozen** for the session's life (§6.2.2).

```
┌─────────────────────────────────────────┐
│  1. IDENTITY          (static, ~150 tok)│  who the robot is
├─────────────────────────────────────────┤
│  2. PERSONALITY       (config, ~250 tok)│  §6.5 — from TOML
├─────────────────────────────────────────┤
│  3. CAPABILITIES      (static, ~200 tok)│  what tools exist & when to use them
├─────────────────────────────────────────┤
│  4. INJECTED MEMORY   (dynamic, ~600tok)│  §6.7 — top-k facts, per session
└─────────────────────────────────────────┘
   Total ≈ 1,200 tokens. Cached after turn 1 at $0.30/1M.
```

Ordering is deliberate and load-bearing: **most static first, most dynamic last.** Prompt caching works on prefixes. Layers 1–3 are identical across every session on a given build, so they cache across sessions. Layer 4 changes per session, so it must come last or it invalidates everything behind it.

The instructions + tools budget is generous — OpenAI permits up to 16,384 tokens — but we spend ~1,600. That's a choice, not a limit: every token here is billed as input on *every turn* of the session. Cheap at the cached rate, not free.

## 6.5 Personality engine — ADR-006

**Decision:** Personality is (a) composed instruction text and (b) a post-hoc affect mapping. It is not fine-tuning, not a separate model, and not a wrapper around responses.

Why this belongs in a document rather than being obvious: the brief says "the robot personality should be separated from the AI model," and there are three ways to read that. Only one survives contact with the latency budget.

| Approach | Verdict |
|---|---|
| Fine-tune a model on personality data | Rejected. No fine-tuning for Realtime; would defeat model-swapping; enormous effort for a config change. |
| Post-process the model's text through a "personality filter" LLM | Rejected. Adds a full round trip inside the turn path (§2.8.1). Non-starter. |
| **Compose personality into instructions; map output → affect locally** | **Accepted.** Zero added latency. Personality becomes a config file. |

```toml
# config/personality/default.toml
name = "Pico"
traits = ["friendly", "lightly funny", "curious", "not sycophantic"]
verbosity = "brief"          # → "Keep replies to 1-3 sentences unless asked."
formality = "casual"
humor_frequency = "occasional"   # → "A light joke maybe once every few exchanges."
proactivity_tone = "gentle"

forbidden = [
    "Do not compliment the user on their questions.",
    "Do not say 'I'm just an AI' or apologise for having limits.",
    "Do not use more than one exclamation mark per reply.",
]
```

The `forbidden` list earns its place. Positive instructions ("be friendly") are weakly followed; negative constraints ("never open with 'Great question!'") are strongly followed. Most of what makes an assistant feel annoying rather than companionable is a *behaviour to suppress*, not one to add. This is where G3's "≤1 user-rated annoying event/week" is actually won.

Testability (O6): `PersonalityEngine.compose(config) -> str` is a pure function. Unit-testable with zero network. The M6 gate — "same question, two personality configs, recognisably different responses" — is an eval-suite test (§14.7), not a vibe check.

## 6.6 Tool calling catalog

Per ADR-004, **the model does not own memory. It gets tools.** This is the mechanism.

| Tool | Signature | Sync? | Notes |
|---|---|---|---|
| `remember_fact` | `(text, kind, importance)` → `{ok, fact_id}` | **Durable before return** | §3.7.3. The model is told "remembered" only when it's true. |
| `recall` | `(query, k=5)` → `{facts: [...]}` | Async-safe | §7.7. The long-tail retrieval path. |
| `forget` | `(query)` → `{deleted: n}` | **Durable before return** | UC-07. Hard delete, not supersession. §7.10. |
| `set_affect` | `(affect)` → `{ok}` | Async, fire-and-forget | §6.8 |

Declaration is at session level in `session.update`, as JSON Schema. Static for the session (§6.2.2 — they're part of the cached prefix).

**The call flow, which is easy to get wrong:**

```
1. Arguments stream in:  response.function_call_arguments.delta
2. Finalized:            response.output_item.done  (item type: function_call)
3. We execute.
4. We return:            conversation.item.create(type="function_call_output",
                                                  call_id=<matching>, output=...)
5. We MUST then send:    response.create
```

Step 5 is the trap. Returning the tool output does **not** cause the model to speak. Without an explicit `response.create` the robot silently swallows the turn and just sits there. This is the number-one "why is it doing nothing" bug in Realtime tool integration.

**Async function calling is native to `gpt-realtime`.** Long-running calls no longer block the conversation — the model emits tuned placeholders ("still looking that up") rather than dead air. This is why `recall` can afford to do real work (§7.7's ~50 ms budget has headroom) without the user hearing a stall.

## 6.7 Memory injection strategy

Two paths, deliberately. This is the §6.1 diagram's "pre-injected memory" arrow and the `recall` tool, and the split is the interesting design decision.

### Path 1 — Pre-session injection (the common case)

At session open, *before the first byte of user audio is streamed*, `MemoryService` returns the top-k facts. They land in instruction layer 4 (§6.4).

```
Silero fires → [parallel] ──┬─→ open WSS connection      (~150ms)
                            └─→ MemoryService.top_facts() (~30ms, local)
                                        ↓
                            session.update with composed instructions
                                        ↓
                            replay ring buffer → stream live audio
```

Retrieval runs **concurrently with connection setup**, so it costs zero wall-clock time. It is off the turn path entirely (§2.8.1's "no per-turn embedding round-trips"). This is the payoff of the gate: because we open a session on speech detection, we have a natural ~150 ms window in which to do memory work for free.

What goes in: identity facts, active routines, recent high-importance facts. ~600 tokens, ~10-15 facts. Covers UC-01, UC-02, UC-03 without a single `recall` call.

### Path 2 — `recall` tool (the long tail)

UC-05 ("what did I say about my sister?") cannot be pre-injected — the space of possible queries is unbounded. The model calls `recall(query)`, we do semantic retrieval (§7.7), we return facts, async function calling covers the latency.

**Why both?** Pre-injection is cached, therefore nearly free, and covers ~90% of turns. Pure `recall` would be correct but adds a tool round trip to every memory-touching turn. Pure pre-injection would be cheap but fails UC-05. The split puts the cost where the value is.

## 6.8 Affect inference — two tiers

O4 requires affect visible within 150 ms of decision. A tool round trip is ~400 ms+. These are irreconcilable, so we don't try.

| Tier | Source | Latency | Drives |
|---|---|---|---|
| **1 — Operational** | Local `RobotState` transitions | **<20 ms** | LISTENING / THINKING / SPEALKING / IDLE faces |
| **2 — Semantic** | Model calls `set_affect(...)` | ~400 ms, doesn't matter | HAPPY / SAD / CONFUSED overlays |

Tier 1 is pure local state machine — the face is *already correct* before the model has an opinion. Tier 2 refines it when it arrives. Because the baseline is never wrong, Tier 2's latency is invisible.

`AffectService` blends the two and publishes a single `affect.changed`. Neither `ai` nor `display` knows the other exists (§3.6.1). This is exactly the fan-out that justified the event bus in §3.5.1: `set_affect` arrives, one event publishes, the face changes and the servo nods, and `ConversationService` never learned that a servo exists.

*Alternative considered:* infer affect locally from streaming transcript deltas with a sentiment heuristic. Zero cost, zero latency. **Rejected for v1** — sentiment of the robot's own words is a poor proxy for its intended expression ("I'm sorry to hear that" is textually negative but the correct face is *concerned*, not *sad*). Revisit if `set_affect` proves unreliable; the port boundary makes it a one-file change.

## 6.9 Fallback and degraded paths

| Failure | Detection | Response |
|---|---|---|
| WSS drop mid-turn | close frame / timeout | §3.7.6: DEGRADED, canned WAV, backoff reconnect, **re-seed session** (§6.2.3) |
| 429 rate limit | HTTP status | Exponential backoff + jitter, 1s→30s cap |
| 401 | HTTP status | **Fail fast at boot.** Refuse to start. §3.12.3. |
| Slow first token (>10 s) | THINKING timeout | → DEGRADED |
| Tool handler raises | exception | Return `{ok: false, error}` to the model — *don't* crash the turn. The model will apologise gracefully, which is the correct behaviour. |

**The thinking cue.** §2.8.1 concedes ~75% of latency isn't ours, and §6.3's gate adds ~200 ms to the first turn. R-01's contingency is that **perceived latency is designable even when actual latency isn't**: THINKING state renders instantly (Tier 1 affect, <20 ms), and if first audio hasn't arrived by 600 ms we play a short local "hmm" from the WAV bank. A robot that visibly and audibly thinks feels responsive at 1200 ms. A robot that sits silently feels broken at 800 ms.

That WAV bank — degraded speech, thinking cues, boot chime — is perhaps 20 short files. It is the least glamorous deliverable in the project and it does more for perceived quality than any optimisation in §11.

**How the bank is structured (AVID-80).** The ~20 clips are named, not pathed: `domain/cues.py` holds a pure `Cue` enum (the vocabulary — boot chime, "hmm" cues, connection/comprehension/error/farewell phrases), and `services/cue_bank.py` holds the `CUE_FILES` manifest binding each `Cue` to a shipped WAV filename plus `CueBank`, which resolves a cue to a file under a **config-injected** base directory (`[cues] dir`, wired in AVID-81) and plays it through the existing `Speaker.play_file()` port. So a caller asks for `Cue.ONE_MOMENT`, never a filesystem path, and the `ai`/`conversation` layers stay free of asset paths. Playing a cue is a best-effort **direct awaited call**, not an event (§3.5.1) — losing it is a perceived-quality miss, not a correctness bug — so a missing directory or file is logged with the turn's correlation id and swallowed, never allowed to crash a turn. All clips are 24 kHz mono 16-bit to match the §6.2.4 playback format. *When* each cue plays (the 600 ms thinking-cue timer above, the boot chime at boot, a degraded phrase on a dropped session) is `ConversationService`'s call in M5; M4 only proves the bank plays on demand with zero network.

## 6.10 Cost model — SPK-1's target

**All figures verified mid-July 2026. Re-verify before use.**

### 6.10.1 Published rates (per 1M tokens)

| Model | Audio in | Audio **cached** in | Audio out |
|---|---|---|---|
| `gpt-realtime-2.1` | $32.00 | **$0.40** | $64.00 |
| `gpt-realtime-2.1-mini` | $10.00 | **$0.30** | $20.00 |

Audio token accounting: **user audio ≈ 1 token / 100 ms (600 tok/min); assistant audio ≈ 1 token / 50 ms (1,200 tok/min).**

### 6.10.2 The three facts that determine the architecture

**Fact 1 — Cached input is ~98.75% cheaper than uncached.** ($0.40 vs $32.00 on flagship.) This is why §6.4 freezes the instruction block and orders it static-first. It is the single highest-leverage decision in this document.

**Fact 2 — Audio output is a floor you cannot cache away.** Every minute the assistant speaks costs 1,200 × $64/1M = **$0.077/min flagship, $0.024/min mini**. No architecture avoids this. The only lever is *saying less*, which is why `verbosity = "brief"` in §6.5 is a cost control wearing a personality costume.

**Fact 3 — Context is re-billed as input on every turn.** A 20-turn conversation doesn't bill its history once; it bills it 20 times, growing. This is the thing that silently destroys budgets.

### 6.10.3 Modelled: 10 min/day, ~20 turns

Assume each turn ≈ 20 s user + 10 s assistant → 200 tok user + 200 tok assistant, context growing ~400 tok/turn. Static prefix ~1,600 tok.

| Component | Tokens/day | Mini | Flagship |
|---|---|---|---|
| New user audio (uncached) | 4,000 | $0.04 | $0.13 |
| Re-billed context (**cached**) | ~76,000 | $0.02 | $0.03 |
| Assistant audio out | 4,000 | $0.08 | $0.26 |
| **Per day** | | **$0.14** | **$0.42** |
| **Per month** | | **$4.30** | **$12.60** |

Both meet O7 (≤$25/month). **But now break caching** — mutate instructions per turn, or inject memory late in the block:

| Scenario | Mini/month | Flagship/month |
|---|---|---|
| Caching working | $4.30 | $12.60 |
| **Caching broken** | $27.00 | **$84.60** |

Caching is the difference between $12/month and $85/month on flagship. It is not a tuning pass. It is §6.4's entire reason for existing.

### 6.10.4 And now the gate (ADR-007, vindicated)

Suppose we skipped §6.3 and held a session open across a 10-hour day, streaming mic audio continuously. User audio alone: 600 tok/min × 600 min = 360,000 tok/day.

| | Per day | **Per month** |
|---|---|---|
| Always-on, mini | $3.60 | **$108** |
| Always-on, flagship | $11.52 | **$345** |

Against a $25/month target. **The gate is not an optimisation; it is the difference between the product existing and not existing.** A ~1 MB ONNX model is load-bearing infrastructure.

### 6.10.5 Model choice

**Default: `gpt-realtime-2.1-mini`.** ~3× cheaper, $4.30/month against a $25 budget, and for a desk companion the quality delta is not worth 3× on a personal project. Escalate to flagship only if M6's eval suite shows the mini failing personality adherence or tool-calling reliability.

Anthropic's cheaper-chained-pipeline alternative (STT → text LLM → TTS) is genuinely cheaper per token but reintroduces multi-hop latency and discards vocal nuance — the exact things speech-to-speech exists to solve. **Not considered for v1.** Revisit only if usage grows ~1000×, which for a desk robot it will not.

### 6.10.6 Mandatory instrumentation

Per §3.12.2, the metrics dashboard exposes **cost-per-day** from the `response.done` usage payload, broken out by cached/uncached/output. Rationale: every failure mode above is silent. Caching breaking looks exactly like caching working, until the invoice. The counter is the smoke detector.

---

# 7. Memory System Design

This section is the one that makes the product different from a smart speaker (§2.1: *"the interesting engineering is not the AI call, it's everything that persists between AI calls"*). It is also 13 IED at **L confidence** (PMP §5.2, M7), which is an honest way of saying parts of it will be wrong.

## 7.1 Memory tiers

| Tier | Lives in | Lifetime | Owner |
|---|---|---|---|
| **Working** | Realtime session context | One session | OpenAI (server-side) |
| **Long-term factual** | SQLite `facts` | Forever | **Us** |
| **Semantic** | `facts.embedding` + retrieval | Forever | **Us** |
| **Episodic** | SQLite `episodes` | 90 days, then pruned | **Us** |

The first row is a deliberate concession. Within a session, the model keeps its own history and we let it — fighting that would mean re-sending context every turn, which §6.10.2's Fact 3 shows is exactly what we don't want. **Everything that must outlive a session is ours.** That boundary is ADR-004.

## 7.2 Working memory

Not our code. Two constraints leak into our design anyway:

- **32,768-token context**, 4,096 max response. At ~400 tok/turn, ~75 turns before truncation.
- **Auto-truncation drops oldest messages.** We could set `"truncation": "disabled"` to get an error instead. We don't — silent forgetting of the start of a 40-minute conversation is better than a hard failure mid-sentence.

The mitigation for auto-truncation isn't to fight it. It's §7.6: anything important gets extracted to durable memory *while it's said*, so losing it from context is inconsequential.

## 7.3 Long-term factual memory

### The shape of a fact

A `Fact` is atomic, attributable, timestamped, and stated in natural language.

**Why natural language and not `(subject, predicate, object)` triples?** LLM-extracted triples are brittle: "I drink coffee every day at 8 AM" wants to be `(user, drinks, coffee)` + `(user, coffee_time, 08:00)` + `(user, frequency, daily)` and the schema fights you forever. Generative Agents and Mem0 both store natural-language records with metadata, and both work. We store `text` and add structured columns *only* where a downstream consumer genuinely needs machine-readable values — which is exactly one case: routines, because §10 needs a time to schedule.

```python
class FactKind(StrEnum):
    IDENTITY     = "identity"      # "The user's name is Ali."
    PREFERENCE   = "preference"    # "The user prefers tea to coffee."
    ROUTINE      = "routine"       # "The user drinks coffee at 08:00 daily."
    RELATIONSHIP = "relationship"  # "The user's sister is called Maya."
    EVENT        = "event"         # "The user had a job interview on 12 July."
    OTHER        = "other"
```

## 7.4 Semantic memory and embeddings — ADR-011

**Decision: local `all-MiniLM-L6-v2` via ONNX Runtime, 384 dimensions.**

| Option | Dims | Cost | MTEB | Verdict |
|---|---|---|---|---|
| **all-MiniLM-L6-v2 (local)** | 384 | **$0** | ~56 | **Accepted** |
| text-embedding-3-small | 1536 (truncatable 256–1536) | $0.02/1M | 62.3 | Fallback |
| text-embedding-3-large | 3072 (truncatable) | $0.13/1M | 64.6 | Overkill |

Reasoning:

1. **Privacy.** §13 — memories are the most sensitive data in the system. Not shipping them to a third party for embedding is the right default for a device that lives on your desk and listens.
2. **Cost is nil, but that's not the argument.** At our volume even 3-large is pennies. The argument is privacy and offline capability.
3. **384 dims keeps brute-force search fast** (§7.7). Dimension count drives scan cost linearly.
4. **Quality is sufficient.** MTEB ~56 vs ~62 matters for web-scale retrieval over millions of documents. We are retrieving from a few thousand facts about one person, where the discriminative signal is enormous.
5. **It's ~80–90 MB and 22M params.** Fits the §2.7.1 budget with room to spare.

**Honest caveat:** no published Pi 5 benchmark for MiniLM exists. Extrapolating from Pi 5 ONNX vision workloads, tens of ms per sentence is plausible — *plausible*, not measured. Since embedding happens on fact-write (rare) and session-open (concurrent with connection setup, §6.7), even 200 ms would be acceptable. Low risk, but it is an estimate and is labelled as one.

Both OpenAI v3 models support Matryoshka truncation — a 256-dim 3-large vector still beats a full 1536-dim ada-002. If we fall back to the API, truncate to 512 and keep §7.7 fast.

**Escape hatch:** `Embedder` is a port. `LocalMiniLmEmbedder` / `OpenAiEmbedder` / `FakeEmbedder`. If R-07 fires (retrieval feels senile), swapping is one config line and a re-embed migration.

## 7.5 Episodic memory

Raw transcripts, `correlation_id`-keyed, 90-day retention. Not retrieved during conversation — it exists for debugging (§3.12.2), for reflection (§7.9), and for the M7 eval set. Pruned on a schedule because §2.7.1 says the SD card is the binding constraint.

## 7.6 Fact extraction pipeline

**Decision: the model extracts, via `remember_fact`. We do not run a background extraction pass.**

The alternative — post-process every transcript with a cheap LLM to mine facts — is what several published systems do, and it's rejected here for one reason: **the model already knows what mattered.** It was in the conversation. Asking a second model to re-derive that from a transcript is strictly less informed and strictly more expensive.

The cost is that extraction quality depends on instruction-following. §6.4's capability layer earns its tokens:

```
When the user tells you something durable about themselves — their name,
preferences, routines, relationships, or significant events — call
remember_fact. Do this silently and continue the conversation naturally;
do not announce that you are remembering. Rate importance 1-10, where
1 is trivia and 10 is core identity. Do not store passing remarks,
questions, or anything you inferred rather than were told.
```

"Do not store anything you inferred rather than were told" is the important line. Without it, the model confabulates facts from context and your memory fills with plausible fiction — which is far worse than an empty memory, because it's confidently wrong.

## 7.7 Retrieval and ranking

**Adopted: the Generative Agents scoring model** (Park et al., UIST 2023, arXiv:2304.03442), which remains the reference design.

```
score = α·recency + β·importance + γ·relevance
```

Each component min-max normalised to [0,1]; the paper uses **equal weights (α=β=γ=1)**. We start there — deviating from a published baseline before measuring is how you end up tuning noise.

| Component | Our implementation |
|---|---|
| **Recency** | Exponential decay on `last_accessed_at`. Park used 0.995/sandbox-hour; that's simulation time. We use a **14-day half-life**: `0.5 ** (Δdays / 14)`. |
| **Importance** | LLM-rated 1–10 at write time (§7.6). Stored, never recomputed. |
| **Relevance** | Cosine similarity, query embedding vs. fact embedding. |

**Top-k = 5.** Both the Generative Agents implementer guidance and Mem0's results point at small k. More retrieved facts is not better — it dilutes the instruction block and costs cached tokens every turn.

### Hybrid retrieval

Vector search alone fails on proper nouns — "Maya" embeds to something generic and won't reliably retrieve the sister fact. So: **SQLite FTS5 keyword search ∪ vector search**, merged, then scored. Mem0's stack fuses semantic + keyword + entity matching in parallel passes and reports **92.5 on LoCoMo / 94.4 on LongMemEval at <7,000 tokens per retrieval** — an order of magnitude under full-context stuffing. The lesson we take is not their numbers; it's that *hybrid beats pure-vector, and small retrieval beats large*.

### Vector storage — ADR-005, confirmed

**v1: embeddings as SQLite BLOBs + pre-normalised numpy brute-force cosine (single matmul).**

Not sqlite-vec. The research is clear on why:

- sqlite-vec is **pre-1.0 alpha** (stable v0.1.9, March 2026); breaking SQL/storage changes are expected before 1.0.
- Its search is **brute-force O(n) anyway** — the same thing numpy does, in someone else's alpha C extension. ANN indexes exist only in alpha pre-releases.
- **The ARM64 wheel has a documented history of shipping a 32-bit ARM binary** (v0.1.6, issue #211) which won't load on 64-bit Pi OS. v0.1.9 publishes a correct aarch64 wheel, but this is precisely the class of problem you don't want between you and M7.

Numpy brute-force over pre-normalised 384-dim vectors is a two-line function with no dependency risk. **Adopt sqlite-vec when it hits 1.0 and we have >50k facts**, i.e. probably never for a single-user robot.

**Scale thresholds (SPK-3 measures these on-device):**

| Facts | Expected | Action |
|---|---|---|
| < 10k | comfortable | numpy brute force |
| 10k–50k | acceptable | numpy; monitor |
| 50k–100k | tight at 384 dims | int8 quantise, or sqlite-vec |
| > 100k | too slow | ANN (vectorlite/HNSW) |

Realistic ceiling for one user over years: **low thousands.** We are architecting for a problem we will not have — deliberately, because the cheap option is also the correct one at our scale, and §7.4's escape hatch covers the rest.

**Latency budget: <50 ms on the Pi.** Retrieval is off the turn path (§6.7) so this has slack, but if it exceeds ~150 ms the `recall` tool starts to be noticeable even with async function calling.

## 7.8 Conflict resolution and supersession

This is the subtlest part of the system and the most likely source of R-07.

**The problem:** at T1 the user says "I drink coffee at 8 AM." At T2, six months later, "I've switched to tea." Both are true statements about the past. Only one is true now.

**Rejected: overwrite.** Destroys temporal reasoning. "What did I used to drink?" becomes unanswerable, and worse, you can never audit what the robot thinks it knows.

**Rejected: keep both, let scoring sort it out.** Recency helps but doesn't *resolve* — the model gets two contradictory facts and picks one, unpredictably. Contradictory context is how a companion becomes unsettling.

**Accepted: soft supersession on write.**

```
store_fact(F_new):
  1. embed(F_new)
  2. candidates = retrieve_similar(F_new, cosine > 0.85, k=5)
  3. if candidates:
       LLM call (cheap text model, off the turn path):
         "Does F_new update or contradict any of these? Return ids."
  4. for each superseded id:
       UPDATE facts SET superseded_by = F_new.id WHERE id = ?
  5. INSERT F_new
  6. COMMIT                    ← durable before the tool returns (§3.7.3)
  7. publish memory.fact_stored
```

Retrieval defaults to `WHERE superseded_by IS NULL`. History remains queryable but never contaminates the present.

Step 3 is a **synchronous LLM call inside a tool handler**. Isn't that a latency violation? No — §6.6's async function calling means the model handles the wait gracefully, and this only fires on fact-writes with a near-duplicate, which is rare. But it is the highest-latency path in the system and it must be measured at M7, not assumed.

**The gap-year problem** — the same structure as LACPA's predecessor-acknowledgement rule, as it happens. If the user says "I drink coffee at 8" (T1), goes quiet for a year, then "I've switched to tea" (T2), what was true in between? We record the supersession timestamp, not a retroactive claim. The robot knows tea is current and coffee was previous; it does not invent a switch date. **Do not let the extraction model guess at this.** Unknown is a valid answer and a confabulated date is a bug.

## 7.9 Decay, consolidation, and forgetting

**Nothing is deleted by decay.** Storage at our scale is free; the recency term already sinks stale facts below the top-k cut. Deletion on decay would be destroying data to save bytes we have in abundance.

**Reflection** (Park et al.): periodically, synthesise higher-level insights from recent observations. Nightly, over facts since the last reflection:

> "The user mentions their sister frequently and always warmly." — derived from five separate episodes, itself stored as a fact with `kind=other`, high importance, and a `derived_from` link.

Reflection is what separates a fact database from a memory. It also runs at 03:00 when nobody's talking, on a cheap text model, entirely off any latency path. **Phase 10 work, not M7** — it's the last thing to build and the first thing to cut.

## 7.10 Privacy, redaction, and deletion

| Requirement | Mechanism |
|---|---|
| UC-07 "forget that" | `forget(query)` tool → **hard `DELETE`**, not supersession. Cascades to embeddings and episodes. |
| Data at rest | SQLite on-device. **Never synced.** Never leaves. |
| Embeddings | Local (§7.4) — fact text never goes to a third party for indexing |
| What OpenAI sees | Live audio, and the top-k injected facts in instructions. Nothing else. |
| Audit | "What do you know about me?" → a supported query, returns all non-superseded facts |

The `forget` / supersession asymmetry is deliberate and worth stating explicitly: **supersession is an epistemics feature (what's true now); deletion is a rights feature (what may be retained).** They look similar and must never be conflated. When the user says "forget that," they are not saying "that stopped being true" — they are withdrawing consent, and the row goes away.

---

---

# 8. Data Design

This section is the DDL that §7 implies. It is written *before* M7 starts because a schema is the most expensive thing in the system to change once there's data in it — and because "we'll design the tables as we go" is how you end up with three columns that all mean *approximately* "when".

## 8.1 Conceptual model

```
      ┌───────────┐  supersedes   ┌──────────┐
      │  facts    │◄──────────────┤  facts   │   self-referential
      │           │  (soft)       └──────────┘
      └─────┬─────┘
            │ 1
            │
      ┌─────┴──────┬──────────────┐
      │ 0..1       │ 0..*         │ 0..*
┌─────▼─────┐ ┌────▼─────┐  ┌─────▼──────┐
│ routines  │ │ triggers │  │ facts_fts  │  (FTS5 shadow)
└───────────┘ └────┬─────┘  └────────────┘
                   │ 1
                   │ 0..*
             ┌─────▼─────────┐        ┌──────────┐
             │ proactive_log │        │ episodes │  (independent)
             └───────────────┘        └──────────┘
```

## 8.2 Conventions, decided once

Three choices that would otherwise be re-litigated in every table:

**Time is `INTEGER` Unix epoch seconds, UTC. Always.**

Not ISO-8601 TEXT. §7.7 computes `0.5 ** ((now - last_accessed_at) / (14*86400))` across every candidate fact on every retrieval. With INTEGER that's raw arithmetic the query planner can index. With TEXT you parse thousands of strings per query, on a Pi, inside a latency budget. Human-readability is not worth that, and `datetime(x, 'unixepoch')` exists for when you're debugging.

*Local* time appears in exactly one place — `routines.local_time` — because a coffee routine is anchored to wall-clock, not to UTC, and must survive a DST shift. It is TEXT `'HH:MM'` plus an IANA timezone, and it is converted to UTC at schedule time, never stored as UTC.

**Embeddings are `BLOB`: 384 × float32, little-endian, pre-normalised.**

1,536 bytes per fact. Pre-normalised at write means cosine similarity *is* a dot product, so §7.7's ranking is one matmul with no per-query normalisation pass.

**IDs are `INTEGER PRIMARY KEY`** — i.e. rowid aliases. No UUIDs. There is one device, one writer, no distributed anything. A UUID here would be cargo-culted cost: 16 bytes and a lost rowid optimisation to solve a merge problem we will never have.

## 8.3 Physical schema (v1)

```sql
-- ─────────────────────────────────────────────────────────────
-- 0001_initial.sql
-- ─────────────────────────────────────────────────────────────

CREATE TABLE facts (
    id                    INTEGER PRIMARY KEY,
    text                  TEXT    NOT NULL,
    kind                  TEXT    NOT NULL
                            CHECK (kind IN ('identity','preference','routine',
                                            'relationship','event','other')),
    importance            INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
    confidence            REAL    NOT NULL DEFAULT 1.0
                            CHECK (confidence BETWEEN 0.0 AND 1.0),

    embedding             BLOB,           -- 384 × float32 LE, pre-normalised

    created_at            INTEGER NOT NULL,
    last_accessed_at      INTEGER NOT NULL,
    access_count          INTEGER NOT NULL DEFAULT 0,

    superseded_by         INTEGER REFERENCES facts(id) ON DELETE SET NULL,
    superseded_at         INTEGER,

    derived_from          TEXT,           -- JSON array of fact ids (reflections, §7.9)
    source_correlation_id TEXT,           -- ties back to episodes / logs (§3.12.2)

    CHECK ((superseded_by IS NULL) = (superseded_at IS NULL))
);

-- THE hot path. §7.7 always filters superseded_by IS NULL; a partial index
-- means the index contains only live facts and stays small forever, however
-- much history accumulates behind it.
CREATE INDEX idx_facts_live
    ON facts(last_accessed_at DESC)
    WHERE superseded_by IS NULL;

CREATE INDEX idx_facts_kind_live
    ON facts(kind)
    WHERE superseded_by IS NULL;

CREATE INDEX idx_facts_superseded_by ON facts(superseded_by);


-- Structured specialisation. Exists ONLY because §10 needs a machine-readable
-- time to schedule. Everything else about a routine lives in facts.text.
CREATE TABLE routines (
    fact_id       INTEGER PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
    rrule         TEXT    NOT NULL,   -- RFC 5545 RRULE, e.g. 'FREQ=DAILY'
    local_time    TEXT    NOT NULL,   -- 'HH:MM' wall-clock
    timezone      TEXT    NOT NULL,   -- IANA, e.g. 'Asia/Beirut'
    lead_time_s   INTEGER NOT NULL DEFAULT 300   -- fire N seconds early
);


CREATE TABLE triggers (
    id             INTEGER PRIMARY KEY,
    fact_id        INTEGER REFERENCES facts(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL
                     CHECK (kind IN ('schedule','presence','condition')),
    enabled        INTEGER NOT NULL DEFAULT 1,

    next_fire_at   INTEGER,
    last_fired_at  INTEGER,
    fire_count     INTEGER NOT NULL DEFAULT 0,
    ignore_streak  INTEGER NOT NULL DEFAULT 0,   -- §10.5 backoff
    cooldown_s     INTEGER NOT NULL DEFAULT 3600
);

-- The scheduler's only query (§10.3). Partial: disabled and unscheduled
-- triggers never enter the index.
CREATE INDEX idx_triggers_due
    ON triggers(next_fire_at)
    WHERE enabled = 1 AND next_fire_at IS NOT NULL;


-- R-08's instrument. Every proactive DECISION is logged, delivered or not.
CREATE TABLE proactive_log (
    id             INTEGER PRIMARY KEY,
    trigger_id     INTEGER REFERENCES triggers(id) ON DELETE CASCADE,
    considered_at  INTEGER NOT NULL,
    outcome        TEXT    NOT NULL
                     CHECK (outcome IN ('delivered','suppressed')),
    reason         TEXT,        -- which policy rule vetoed (§10.4)
    utterance      TEXT,        -- what it said, or would have said
    user_reaction  TEXT         -- 'engaged'|'ignored'|NULL (unknown yet)
                     CHECK (user_reaction IN ('engaged','ignored') OR user_reaction IS NULL)
);

CREATE INDEX idx_proactive_log_time ON proactive_log(considered_at DESC);


CREATE TABLE episodes (
    id              INTEGER PRIMARY KEY,
    correlation_id  TEXT    NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    transcript      TEXT
);

CREATE INDEX idx_episodes_started ON episodes(started_at);   -- pruning (§7.5)
CREATE INDEX idx_episodes_corr    ON episodes(correlation_id);


-- FTS5 shadow of facts.text. §7.7's hybrid retrieval: vector search alone
-- cannot reliably retrieve "Maya" — proper nouns embed to mush.
CREATE VIRTUAL TABLE facts_fts USING fts5(
    text,
    content='facts',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER facts_fts_au AFTER UPDATE OF text ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;


CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL,
    checksum    TEXT    NOT NULL
);
```

Note `remove_diacritics 2` on the tokeniser: this robot lives in Lebanon and will meet Arabic and French names. The default tokeniser handles them badly.

## 8.4 Connection PRAGMAs

```sql
PRAGMA journal_mode   = WAL;      -- §2.7.1: fewer fsyncs, concurrent reads
PRAGMA synchronous    = NORMAL;   -- WAL + NORMAL is the durable-enough sweet spot
PRAGMA foreign_keys   = ON;       -- OFF by default in SQLite. A trap.
PRAGMA busy_timeout   = 5000;
PRAGMA temp_store     = MEMORY;   -- never spill temp b-trees to the SD card
PRAGMA cache_size     = -16000;   -- 16 MB. We have 4 GB; the card is the constraint.
PRAGMA wal_autocheckpoint = 1000; -- ~4 MB between checkpoints
PRAGMA mmap_size      = 268435456;
```

Every one of these is an SD-card decision (R-05), not a performance decision. `temp_store = MEMORY` in particular: the default spills to disk, and a sort that touches the card is a write you didn't know you were making.

**`foreign_keys = ON` must be set per-connection.** SQLite defaults it off for backwards compatibility. Every `ON DELETE CASCADE` above is decorative without it — and `forget()` (§7.10) silently orphaning embeddings and triggers is a privacy bug, not a tidiness bug.

## 8.5 The in-memory index

The schema above is the durable store. It is **not** the query path.

§7.7 does brute-force cosine over every live fact. Deserialising N BLOBs per query would make retrieval O(N) in *Python*, which is a different and much worse constant than O(N) in numpy. So:

```
        write                          read
  ┌──────────────┐              ┌──────────────┐
  │ store_fact() │              │  retrieve()  │
  └──────┬───────┘              └──────┬───────┘
         │ 1. INSERT (durable)         │
         │ 2. append row to            │  matrix @ query_vec
         │    in-memory matrix         │  → scores, no I/O
         ▼                             ▼
  ┌─────────────┐   rebuilt on   ┌─────────────────┐
  │   SQLite    │───────────────►│  numpy float32  │
  │  (truth)    │     boot       │   (N × 384)     │
  └─────────────┘                └─────────────────┘
```

Write-through, rebuilt on boot. At 5,000 facts the matrix is **7.7 MB** — invisible against 4 GB (§2.7.1: *"RAM is generous. Do not optimize for it."*). Even at the 50k threshold from §7.7 it's 77 MB.

The invariant: **SQLite is truth, the matrix is an index.** Any divergence is a bug, and the boot-time rebuild is the reconciliation. `forget()` must remove from both, in that order.

## 8.6 Migrations

**Plain numbered SQL files, applied in order, checksummed. No Alembic.**

```
migrations/
  0001_initial.sql
  0002_add_reflections.sql
```

The runner is ~50 lines: read `schema_migrations`, find unapplied versions, verify the checksum of everything already applied hasn't changed, apply the rest in one transaction each. It's testable, it has no dependencies, and you can read the entire schema history with `cat`.

Alembic is excellent and it is for teams with a shared database and an ORM. We have neither. Adding it here buys autogeneration we don't want (§8.2's conventions are deliberate; a generator would fight them) at the price of a dependency and a mental model.

**Rule:** migrations are append-only and never edited after merge. The checksum column enforces this — editing an applied migration fails the boot, loudly, rather than silently diverging your dev DB from the Pi's.

## 8.7 pgvector migration path

The schema is deliberately portable. §2.2's "future: PostgreSQL + pgvector" costs exactly four changes:

| SQLite | PostgreSQL |
|---|---|
| `embedding BLOB` | `embedding vector(384)` |
| `facts_fts` (FTS5) | `tsvector` + GIN index |
| numpy matmul (§8.5) | `ORDER BY embedding <=> $1 LIMIT 5` |
| `INTEGER` epoch | `BIGINT` epoch (keep it — don't "improve" to `timestamptz`) |

Everything else — the tables, the partial indexes, the supersession semantics, the CHECK constraints — transfers unchanged. That is not luck; it's why §8.2 avoided SQLite-specific types.

**We will not do this.** A single-user desktop robot will never outgrow SQLite. It's documented because ADR-005 said "future: pgvector" and an unexamined future commitment is how people justify premature complexity today. Writing down how cheap the migration *would* be is what licenses us to not do it now.

## 8.8 Backup

`VACUUM INTO '/backup/robot-YYYYMMDD.db'` nightly at 03:00, alongside reflection (§7.9). Consistent snapshot without stopping writers, unlike copying the file.

Keep 7. Write to USB storage if present, and if it isn't — **say so in the logs at boot**. A backup written to the same SD card that R-05 says is going to die is not a backup; it's a rehearsal.

---

# 10. Behavior and Proactivity Engine

M10. 13 IED, **L confidence**, on the critical path, and home to R-08 — *"proactive robot is annoying → user disables it → project's core value dies."*

This section is where the product is won or lost. UC-03 is the whole pitch. It is also two lines of scheduling logic wrapped in a great deal of judgement about when to shut up.

## 10.1 Design goals

| | |
|---|---|
| G3 | ≥1 useful proactive event/day; **≤1 user-rated "annoying" event/week** |
| §2.4 | *"not tolerant of a robot that interrupts a meeting"* — a hard requirement |
| Principle | **Default to under-firing.** A robot that misses a coffee reminder is disappointing. A robot that talks over your call gets unplugged, and an unplugged robot has a recall rate of zero. |

That asymmetry is the whole design. The costs of the two error types are not remotely symmetric, so the thresholds should not be either.

## 10.2 The central separation

**A trigger does not decide to speak. It decides to *propose* speaking.**

```
  clock/vision/memory          ┌────────────┐
         │                     │   Policy   │  §10.4
         ▼                     │    Gate    │  pure function
   ┌───────────┐  Proposal     │            │  6 rules, first veto wins
   │  Trigger  ├──────────────►│            │
   └───────────┘               └─────┬──────┘
                                     │
                        ┌────────────┴────────────┐
                        ▼                         ▼
              behavior.proactive_delivered   behavior.proactive_suppressed
                        │                         │
                        ▼                         ▼
               open session, speak         log reason, do nothing
                                            (but LOG IT — §10.6)
```

This split is what makes R-08 tractable. The policy gate is **one pure function over a `PolicyContext` value object** — no I/O, no clock access, no globals. Which means the entire "is the robot annoying" question is a table-driven unit test that runs in milliseconds on a laptop, and every future "it interrupted me during X" bug becomes a new row in that table rather than an archaeology expedition through three services.

If you take one thing from this section into the code: **the judgement lives in a pure function, and the pure function is where the tests are.**

## 10.3 Scheduler

One asyncio task. A min-heap of `(next_fire_at, trigger_id)`. It sleeps until the earliest deadline.

Not cron. Not a task per trigger. Not polling every second.

```python
async def run(self) -> None:
    while True:
        nxt = self._heap.peek()
        delay = 3600 if nxt is None else max(0, nxt.at - self._clock.now())
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
            self._wake.clear()          # heap changed; recompute
            continue
        except TimeoutError:
            pass
        await self._fire_due()
```

The `_wake` event is what makes it correct: when §3.7.3's `memory.fact_stored` arrives and `BehaviorService` registers a new trigger, the sleeping task must recompute rather than snoozing until the old deadline.

**Schedule triggers use RFC 5545 RRULEs**, via `dateutil.rrule`. `FREQ=DAILY`, `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR`, `FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO`. Do not invent a schedule DSL — recurrence rules are a solved problem with a standard and a library, and every DSL anyone has ever invented for this has been re-derived, badly, up to about `BYSETPOS`.

Wall-clock anchoring is mandatory: compute the next occurrence in `routines.timezone`, then convert to UTC. Storing "08:00 = epoch X, +86400 each day" breaks on the DST boundary and delivers your coffee reminder at 07:00 for six months.

The clock is injected (`Clock` port). M10's gate criterion — *"the coffee scenario, end to end, unprompted"* — is testable in 40 ms with a `FakeClock`, and separately once for real. Waiting until 07:55 to test the 07:55 code path is not a testing strategy.

## 10.4 The interruption policy

Rules, in order. **First veto wins.** All state arrives in a `PolicyContext`; the function touches nothing else.

| # | Rule | Veto when | Rationale |
|---|---|---|---|
| 1 | **Quiet hours** | `now` within configured quiet window | Hard. Non-negotiable. Default 22:00–07:30. |
| 2 | **State** | state ∉ {IDLE, SLEEPING} | Never interrupt a conversation in progress. §3.10.1. |
| 3 | **Presence** | no presence within last 5 min | **See below.** |
| 4 | **Ambient speech** | >60 s of VAD speech in last 5 min that opened no session | **See below.** |
| 5 | **Cooldown** | trigger fired < `cooldown_s` ago, *or* any proactive < 15 min ago | Global cooldown, not just per-trigger. |
| 6 | **Daily budget** | ≥5 proactive delivered today | Ceiling, not a target. G3 asks for ≥1. |

### Rule 3 — presence

A robot talking to an empty room is not proactive; it's a robot talking to itself, and it will do so 200 times before you notice. Requires §8's `vision.presence_gained` within a 5-minute window.

**This is the dependency that makes cutting M8 (PMP §7.3, cut #3) more expensive than its 8 IED suggests.** Without vision, rule 3 degrades to "was there a conversation recently," which is a much weaker proxy. Worth knowing before the cut looks free in month eight.

### Rule 4 — ambient speech suppression

§2.4's meeting requirement, and the one piece of genuine cleverness in this section.

We cannot detect a meeting. But we're already running Silero VAD continuously for §6.3's gate — it costs nothing extra, and it produces a signal we haven't used:

> **Sustained speech that never opens a session is speech not directed at the robot.**

Someone on a call, in a meeting, talking to a colleague — the VAD fires constantly, no conversation ever starts. That pattern is *exactly* "the human is busy talking to someone who isn't me." So: if VAD detected >60 s of speech in the last 5 minutes and no session opened, suppress.

Free, because the infrastructure already exists for a different reason. It's the kind of signal you only find by asking what the components you already have are incidentally measuring.

*Known false positive:* the user watching a video or playing music. Suppressing then is a small loss — arguably correct anyway.

### Manual override

A `set_quiet(duration)` tool (§6.6, added at M10). "Leave me alone for an hour" is a thing people say to companions, and it must work the first time, without configuration, or rule 1's static window carries the whole load.

## 10.5 Ignore backoff

Rules 1–6 are static. This is the part that adapts, and it's R-08's mitigation with teeth:

```
proactive delivered → wait 30 s
  user responded?  → ignore_streak = 0
  silence?         → ignore_streak += 1
                     cooldown_s *= 2
  ignore_streak >= 3 → enabled = 0, log it
```

Without this, a badly-conceived trigger annoys forever at a fixed rate. With it, the robot notices it's being ignored and stops. That is the single behaviour most likely to keep this thing switched on in month nine.

Disabling is logged loudly (`behavior.trigger_disabled`), never silent. A trigger that turned itself off is diagnostic information about the design, and if you don't surface it you'll never learn which of your ideas were bad.

## 10.6 Log every decision

Per §8.3's `proactive_log`: **every considered proposal is logged, delivered or not**, with the vetoing rule and the utterance it *would* have made.

This is not an audit trail. It's the only way to tune §10.4 without guessing. In month three you will ask "is it under-firing?" and the answer is a SQL query:

```sql
SELECT reason, COUNT(*) FROM proactive_log
WHERE outcome = 'suppressed' AND considered_at > unixepoch() - 604800
GROUP BY reason ORDER BY 2 DESC;
```

If `ambient_speech` vetoed 40 times last week, rule 4 is too aggressive. If nothing was ever suppressed, the rules are decorative. **Without this table, both look identical from the outside** — which is precisely why R-08 is scored 15 and why "log every suppression so you can see what it would have said" was in the mitigation from the start.

## 10.7 Proactive turn initiation

The mechanism for speaking with no user audio. §3.10.3's `IDLE + behavior.trigger_fired → THINKING`.

```
1. Policy passes
2. Open Realtime session (§6.2) — instructions + injected memory (§6.7)
   + the proactive context block
3. Send response.create directly.  ← no input_audio_buffer at all
4. Model speaks. state → SPEAKING.
5. Session held open 30 s for a reply.
6. Reply → normal turn. No reply → close, ignore_streak += 1 (§10.5)
```

Step 3 is the whole trick: `response.create` with no user turn. The Realtime API doesn't care that nobody spoke.

**Cost:** ~10 s of audio out ≈ 200 tokens ≈ **$0.004** on the mini. Five a day is **$0.60/month**. Against §6.10.3's $4.30, proactivity is a rounding error. The expensive thing about proactivity is never the tokens.

## 10.8 What it says

No template DSL. No canned strings. We give the model context and let §6.5's personality do the work:

```
It is 07:55 on Tuesday. The user is present and has not spoken to you yet
today. You know: "The user drinks coffee every day at 08:00."

Greet them briefly and mention this naturally, in one sentence.
Do not sound like an alarm or a reminder app. Do not ask a question
unless it would be natural. If they don't reply, that's fine.
```

That is the entire authoring format. Everything that would go into a template — tone, brevity, not being annoying — already lives in the personality layer, and duplicating it here would give you two places to change and one to forget.

"Do not sound like an alarm or a reminder app" is doing more work than it looks. The failure mode for UC-03 isn't wrong timing; it's correct timing delivered like a calendar notification. The gap between *"Good morning! Coffee time is coming soon ☕"* and *"Reminder: coffee at 08:00"* is the entire product.

---

---

# 9. Interfaces and APIs

**This section is normative.** §3.5.1 conceded that the event bus buys decoupling at the price of control-flow opacity — you cannot read the code top-to-bottom and know what happens. This catalog is the promised mitigation. It is the one place that answers "if I publish this, who wakes up?"

An event not in §9.1 does not exist. A subscriber not in §9.1 is undocumented behaviour. Both fail CI (§9.1.5).

## 9.1 Internal event catalog

### 9.1.1 The envelope

Every event carries these. They are on the base class, they are not optional, and no event may redefine them.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    event_id:       UUID   # unique, this occurrence
    correlation_id: UUID   # the TURN this belongs to. §3.12.2.
    timestamp_ms:   int    # epoch milliseconds, wall clock — for logs
    monotonic_ns:   int    # time.monotonic_ns() — for latency math
    source:         str    # publishing component name, e.g. "AudioService"
```

**Why two clocks.** `timestamp_ms` is wall-clock and can jump — NTP steps, and §6.2's RTC discussion means the Pi may correct its clock seconds after boot. §2.8.1's P50/P95 latency histogram is computed by subtracting two event times. Do that with wall-clock and an NTP step mid-turn yields a negative latency, which silently poisons the metric that G1 is measured by.

So: **wall-clock for humans, monotonic for arithmetic.** Never subtract `timestamp_ms`. This is 8 bytes to eliminate a bug class that is genuinely miserable to diagnose, because it only fires occasionally and the data looks merely *weird* rather than obviously wrong.

Events are frozen dataclasses. Immutability is not stylistic — handlers are dispatched **concurrently** (§3.5.2), so a mutable event is a data race with extra steps.

### 9.1.2 Naming

`<domain>.<verb-past-tense>`. Enforced at registration (P4).

`affect.changed` — correct. Something happened; the display decides what to do about it.
`display.set_emotion` — **wrong.** That's a command wearing an event's clothes, and it re-couples the publisher to a specific consumer.

The test: if you can't name it in the past tense without contorting, you are trying to tell someone to do something. Call their method directly, or reconsider the design.

### 9.1.3 The catalog

Queue policy per §3.5.5. `DROP_OLDEST` = latest wins, stale is worthless. `DROP_NEWEST` = preserve the start of an incident.

#### `system`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `system.started` | `adapters: dict[str, bool]` | Lifecycle | StateManager, BehaviorService | DROP_NEWEST |
| `system.shutting_down` | `reason: str` | Lifecycle | *all services* | DROP_NEWEST |
| `system.handler_failed` | `handler: str`, `event_type: str`, `reason: str`, `exc: str \| None` | EventBus | Observability | DROP_NEWEST |
| `system.degraded_entered` | `cause: str` | ConversationService | ExpressionService, BehaviorService, Observability | DROP_NEWEST |
| `system.degraded_exited` | `downtime_s: float` | ConversationService | ExpressionService, BehaviorService, Observability | DROP_NEWEST |

`system.handler_failed` is published *by the bus itself* when a subscriber raises (§3.5.2) or overflows its queue (§3.5.5, `reason="queue_overflow"`). It is the bus's only self-referential event and it must never be published from a handler *of* `system.handler_failed` — that's an infinite loop, and it is guarded explicitly.

#### `audio`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `audio.speech_started` | `ring_buffer_ms: int` | AudioService | StateManager, ConversationService | DROP_OLDEST |
| `audio.speech_ended` | `duration_ms: int` | AudioService | StateManager, BehaviorService | DROP_OLDEST |
| `audio.playback_started` | `item_id: str` | AudioService | StateManager | DROP_OLDEST |
| `audio.playback_finished` | `item_id: str`, `played_ms: int`, `truncated: bool` | AudioService | StateManager, ConversationService | DROP_OLDEST |

`audio.speech_started` mints the `correlation_id` for a user-initiated turn. It is one of exactly two turn origins; `behavior.trigger_fired` is the other.

`played_ms` on `audio.playback_finished` is §6.2.4's barge-in measurement — what the speaker actually emitted, not what we received.

#### `conversation`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `conversation.turn_started` | `initiator: "user" \| "proactive"` | ConversationService | Observability | DROP_NEWEST |
| `conversation.user_transcribed` | `text: str`, `is_approximate: bool` | ConversationService | StateManager, EpisodeRecorder | DROP_NEWEST |
| `conversation.assistant_responded` | `text: str`, `item_id: str` | ConversationService | EpisodeRecorder, Observability | DROP_NEWEST |
| `conversation.turn_ended` | `duration_ms: int`, `usage: TokenUsage` | ConversationService | Observability (cost meter, §6.10.6) | DROP_NEWEST |
| `conversation.session_lost` | `cause: str`, `was_mid_turn: bool` | ConversationService | StateManager, ExpressionService | DROP_NEWEST |

`is_approximate` is not a hedge — it's §6.2.4's truncation consequence made explicit. `conversation.item.truncate` drops the transcript for unplayed audio, and audio/transcript alignment is imprecise, so a barge-in leaves the tail of the transcript unreliable. Anything downstream that treats this text as ground truth (fact extraction, episode recording) must consult the flag.

`conversation.turn_ended` carries the `usage` payload from `response.done`. This is the sole feed for §6.10.6's cost meter — the smoke detector for the $12-vs-$85 failure.

#### `affect` / `state`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `affect.changed` | `affect: Affect`, `tier: 1 \| 2`, `previous: Affect` | AffectService | **ExpressionService** ✅, MotionService (M9) | **DROP_OLDEST** |
| `state.transitioned` | `from_: RobotState`, `to: RobotState`, `trigger: Trigger` | StateManager | **ExpressionService** ✅, **AffectService** ✅, BehaviorService (M6), Observability (M10) | DROP_OLDEST |

✅ marks a subscriber that is **registered in the composition root today** (AVID-73); the rest are planned, with the milestone that lands them. The distinction matters for §9.1.5: a drift check written now would flag every unmarked entry as missing, which is a gap in the *schedule*, not a defect in the code — so the generator must diff against live registrations, not against the full aspirational catalog. This is the only table in §9.1.3 annotated so far, because it is the only one whose subscribers have started to exist.

Note also the naming: this catalog names **services**, while `subscribe(..., name=...)` takes the *subscription* name, which is `<Service>.<event>` — `AffectService.state_transitioned`, `ExpressionService.affect_changed`, `ExpressionService.state_transitioned`. One service appears once per event it subscribes to. §9.1.5's generator therefore maps a service to a *set* of dotted names, and must not expect a 1:1 match against this column.

`affect.changed` is the fan-out that justified the bus in the first place (§3.5.1): one publish, the face changes *and* the servo nods, and `AffectService` has never heard of either. Adding an LED at Phase 11 is a new subscriber and zero edits upstream.

`tier` distinguishes §6.8's two-speed affect — Tier 1 (local state, <20 ms) vs Tier 2 (model's `set_affect`, ~400 ms). Subscribers may use it to decide whether a transition should be animated or instant.

#### `vision`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `vision.presence_gained` | `confidence: float` | PresenceService | StateManager, BehaviorService, AffectService | DROP_OLDEST |
| `vision.presence_lost` | `absent_for_s: float` | PresenceService | StateManager, BehaviorService | DROP_OLDEST |
| `vision.face_detected` | `count: int`, `largest_bbox: BBox` | PresenceService | AffectService | DROP_OLDEST |

Presence events are **hysteresis-filtered inside PresenceService**, not raw detections. M8's gate is "no flapping over a 1-hour desk recording," and if raw per-frame detections reach the bus you get hundreds of events an hour and rule 3 of §10.4 becomes noise. The debouncing is the service's job; the bus sees decisions.

#### `memory`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `memory.fact_stored` | `fact_id: int`, `kind: FactKind`, `importance: int` | MemoryService | BehaviorService, Observability | DROP_NEWEST |
| `memory.fact_superseded` | `old_id: int`, `new_id: int` | MemoryService | BehaviorService, Observability | DROP_NEWEST |
| `memory.fact_deleted` | `fact_id: int` | MemoryService | BehaviorService, Observability | DROP_NEWEST |
| `memory.recall_completed` | `query: str`, `n_returned: int`, `latency_ms: float` | MemoryService | Observability | DROP_NEWEST |

**These are published *after* the write is durable, never before.** §3.7.3's diagram: commit, then publish. They are notifications that something already happened — which is the only reason it's safe to put them on an at-most-once bus (§9.1.4).

`memory.fact_stored` → `BehaviorService` is how UC-02 becomes UC-03 with zero coupling. `MemoryService` does not know the behaviour engine exists.

#### `behavior`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `behavior.trigger_fired` | `trigger_id: int`, `fact_id: int \| None` | BehaviorService | StateManager, ConversationService | DROP_NEWEST |
| `behavior.proactive_delivered` | `trigger_id: int`, `utterance: str` | BehaviorService | Observability | DROP_NEWEST |
| `behavior.proactive_suppressed` | `trigger_id: int`, `rule: str`, `would_have_said: str \| None` | BehaviorService | Observability | DROP_NEWEST |
| **`behavior.trigger_disabled`** | `trigger_id: int`, `ignore_streak: int` | BehaviorService | Observability | DROP_NEWEST |

> **Catalog drift, caught.** `behavior.trigger_disabled` was not in §3.5.3's taxonomy — it was invented in §10.5 when the ignore-backoff design needed it, and §10.5 says it must be "logged loudly, never silent." Writing this catalog is what surfaced the gap. **This is the process working**, and it's the argument for §9.1.5's CI check: four sections referenced this catalog before it existed, and exactly one event had already drifted.

`behavior.trigger_fired` mints a `correlation_id` — the second and last turn origin.

#### `motion`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `motion.gesture_started` | `gesture: str`, `axes: tuple[Axis, ...]` | MotionService | Observability | DROP_OLDEST |
| `motion.gesture_completed` | `gesture: str`, `duration_ms: int` | MotionService | Observability | DROP_OLDEST |
| `motion.gesture_preempted` | `gesture: str`, `by: str \| None` | MotionService | Observability | DROP_OLDEST |

`gesture_preempted` covers both a newer gesture interrupting an older one (`by="nod"`) and §3.12.3's I²C-fault abort (`by=None`).

### 9.1.4 What is deliberately NOT an event

The catalog above is incomplete without its complement. §3.5.4's rule:

> **The bus carries notifications, not obligations.** If losing it would be a correctness bug, it's a direct awaited call. If losing it is a cosmetic glitch, it's an event.

The bus is in-memory, at-most-once, no persistence, no replay, no dead-letter queue. Therefore:

| Operation | Mechanism | Why not an event |
|---|---|---|
| `MemoryService.store_fact()` | direct await, durable before return | Losing a fact is a correctness bug. §3.7.3. |
| `MemoryService.forget()` | direct await, durable before return | Losing a deletion is a **privacy** bug. §7.10. |
| `MemoryService.retrieve()` | direct await | A request, not a fact. Has a return value. |
| `Display.render()` | direct port call | ExpressionService owns the port. §3.6.1. |
| `Servo.move_to()` | direct port call | MotionService owns the port. |
| Policy gate evaluation | pure function | §10.2. No I/O by construction. |

Apply this test to every event anyone proposes adding. The failure mode is seductive: everything *can* be an event, the bus makes it easy, and then one day a fact is silently lost because a queue overflowed under load and nobody notices for a month.

### 9.1.5 Drift enforcement

The catalog is normative; the check is mechanical.

```
CI job: event-catalog-drift
  1. Import the composition root, walk the subscription registry
  2. Emit (event_type, subscriber_name) pairs from live code
  3. Parse §9.1.3's tables from this document
  4. Diff. Any difference fails the build.
```

`subscribe(..., name=...)` is mandatory (§3.5.2) for exactly this reason — an anonymous lambda subscriber is invisible to the generator and would silently pass.

**Static subscription (§3.5.2) is what makes this possible.** Runtime subscription would make the graph unknowable, which would forfeit the mitigation, which would leave the bus's opacity cost unpaid. The restriction and the check are one design.

## 9.2 Service interfaces

Every service has the same shape (§3.4.3): subscribe to some events, call some ports, publish other events.

```python
@runtime_checkable
class Service(Protocol):
    name: str
    async def start(self) -> None: ...
    async def stop(self) -> None:
        """Idempotent. Must complete within 5s — systemd's stop timeout."""
    def subscriptions(self) -> Sequence[Subscription]:
        """Declared, not registered. main.py registers them.
        This is what §9.1.5's generator walks."""
```

`subscriptions()` returns declarations rather than performing registration. The service says what it wants; the composition root decides. That inversion is what keeps the subscriber graph static and knowable.

Public methods beyond this Protocol exist only where §9.1.4 requires a direct call — `MemoryService.store_fact/retrieve/forget`, `StateManager.transition`, `AffectService.set_affect`. Everything else is bus-mediated.

## 9.3 HAL ports

Defined normatively in **§3.9.1**. Not restated here — a duplicated interface definition is a divergence waiting to happen.

Additions since:

```python
class Clock(Protocol):
    def now(self) -> int:            # epoch seconds, §8.2
        ...
    def monotonic_ns(self) -> int:   # §9.1.1
        ...
    async def sleep(self, seconds: float) -> None: ...

class Embedder(Protocol):            # ADR-011, §7.4
    async def embed(self, text: str) -> NDArray[np.float32]:
        """Returns a PRE-NORMALISED vector. §8.2."""
    @property
    def dimensions(self) -> int: ...

class VoiceActivityDetector(Protocol):   # §6.3
    def is_speech(self, frame: AudioChunk) -> bool:
        """MUST return in <5ms — called on every 30ms frame."""
```

`Clock` is a port for one reason: M10's gate is "the coffee scenario, unprompted." With `FakeClock` that test runs in 40 ms. Without it, you wait until 07:55. Injecting the clock is the difference between a test suite you run on every commit and one you run once, nervously, in the morning.

## 9.4 External dependencies

| Dependency | Protocol | Behind port | Blast radius if it changes |
|---|---|---|---|
| OpenAI Realtime | WSS | `RealtimeClient` | One adapter. R-10. |
| OpenAI text (supersession, reflection) | HTTPS | `TextModel` | One adapter. |
| NTP | UDP 123 | system | Clock skew at boot; §6.2 RTC mitigates |

That's the entire external surface. Embeddings are local (§7.4), so they aren't on this list — which is the point of ADR-011 restated as a dependency count.

## 9.5 Local control API

`127.0.0.1:8787`. **Localhost binding is the authentication.** There is no auth, no TLS, no accounts — anyone who can reach this socket already has a shell on the robot, at which point the API is not your problem. Binding to `0.0.0.0` here would be a security bug; it is asserted at startup.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | systemd + watchdog. Returns 200 iff loop is live. |
| `GET` | `/metrics` | §3.12.2. Latency histogram, **cost/day** (§6.10.6), queue depths, frame rate, SD writes. |
| `GET` | `/state` | Current `RobotState`, `Affect`, session status |
| `GET` | `/facts` | **§7.10's audit.** All non-superseded facts. "What do you know about me?" |
| `GET` | `/facts?include_superseded=1` | Full history, for debugging §7.8 |
| `POST` | `/quiet` | `{duration_s}` — §10.4's manual override, also reachable via `set_quiet` tool |
| `GET` | `/events/stream` | **SSE tap. Live event feed.** |

`/events/stream` deserves its place. §3.5.1 admits you can't read the code and know what happens. Correlation IDs let you reconstruct a turn *afterwards*, from logs. This lets you watch it happen, live, while you talk to the robot:

```
$ curl -N localhost:8787/events/stream | jq -c '{t:.source,e:.type}'
{"t":"AudioService","e":"audio.speech_started"}
{"t":"StateManager","e":"state.transitioned"}
{"t":"ConversationService","e":"conversation.turn_started"}
...
```

Ten lines of code. It will save you hours in Phase 5 and 10, and it is the sort of thing that never gets built if it isn't written into the spec.

## 9.6 Configuration schema

P7: **configuration is injected, never read.** No module calls `os.environ`. Everything is parsed once, at the composition root, into a frozen pydantic model, and passed down. CI greps for violations.

```toml
# config/pi.toml

[adapters]                      # ← the entire sim/real switch (§3.9.2, §3.11.1)
camera     = "picamera2"        # | "fake"
servo      = "pca9685"          # | "fake"
display    = "framebuffer"      # | "fake" | "png_sequence"  (AVID-55: raw XRGB8888 to /dev/fbN)
microphone = "alsa"             # | "fake"
speaker    = "alsa"             # | "fake"
vad        = "silero"           # | "fake" — local Silero VAD gate (AVID-77)
realtime   = "openai"           # | "replay"
notifier   = "systemd"          # | "fake" — sd_notify supervision (§3.11.3)

[ai]
model            = "gpt-realtime-mini-2025-12-15"   # PINNED. §6.10.
voice            = "cedar"
max_output_tokens = 512                                  # §6.10 guardrail
personality      = "config/personality/default.toml"     # §6.5

[ai.turn_detection]
type                = "server_vad"
threshold           = 0.5
prefix_padding_ms   = 300
silence_duration_ms = 500

[realtime]                      # Realtime session adapter params (AVID-102); only "replay" reads it
session_dir = "assets/sessions/two_turn"   # recorded session ReplayRealtimeClient plays back (§14.3, #101)

[gate]                          # local VAD gate — §6.3 / ADR-007 (NOT [ai.turn_detection], the server VAD)
vad_model          = "silero_v5"
threshold          = 0.5        # Silero speech-probability cutoff (local gate)
ring_buffer_ms     = 300        # pre-roll replayed at a turn's start
silence_hold_ms    = 500        # silence run before a turn is declared over (AudioService debounce)
session_idle_close_s = 30

[cues]                          # degraded-mode WAV cue bank base dir (§3.6.4, AVID-80)
dir = "assets/cues"             # committed 24 kHz mono clips; CueBank resolves dir/<cue>.wav

[memory]
db_path        = "/var/lib/robot/robot.db"
embedder       = "local_minilm"     # | "openai"
dimensions     = 384
top_k          = 5
recency_half_life_days = 14.0       # §7.7
weights = { recency = 1.0, importance = 1.0, relevance = 1.0 }   # Park et al.

[behavior]                      # §10.4
quiet_hours      = { start = "22:00", end = "07:30" }
timezone         = "Asia/Beirut"
global_cooldown_s = 900
daily_budget     = 5
presence_window_s = 300
ambient_speech_threshold_s = 60
ignore_streak_limit = 3

[vision]
fps        = 5                  # §2.7.1: ≤1 core
[motion]
axes       = ["pan"]            # ADR-009 — 1 or 2. Capability-negotiated (§3.9.3).

[api]
bind = "127.0.0.1"              # asserted at startup. §9.5.
port = 8787

[systemd]                       # §3.11.3 — supervision knobs (Type=notify)
watchdog_interval_s = 15.0      # WATCHDOG=1 ping cadence; ≈ WatchdogSec / 2
```

Secrets are **not here**. `OPENAI_API_KEY` is read from the environment exactly once, in `core/config.py`, and injected as a value. It never appears in a config file, a log line, or a repr. Pydantic `SecretStr`, so an accidental `print(config)` prints `**********`. systemd's `$NOTIFY_SOCKET` handoff is injected the same way — read once alongside the key, `None` off a `Type=notify` unit — because it too is a runtime value, not an authored setting.

The `[adapters]` block is the whole of §3.11.1's development story. `uv run robot --config config/sim.toml` is a running robot on a laptop, no hardware, no network. Same binary, same code path, different six lines of TOML. That is what G6 means and it is why the HAL exists.

---

---

# 14. Testing Strategy

M0's gate is this section made real. It is also where this document settles four debts: §9.1.5's drift check, §10.2's policy table, P6's contract suite, and P8's async-debug gate — each promised, none specified.

The constraint that generates everything below is §2.8.4, and it is worth restating because it is stronger than it looks:

> `pytest` on a laptop with **no hardware, no network, and no API key** must exercise **≥90% of non-adapter code.**

Not "should." The HAL (§3.9), the event bus (§3.5), the injected `Clock` (§9.3), and the pure policy gate (§10.2) all exist substantially because of this one line. It is also R-03's most important mitigation: a project where every change needs the Pi on the desk is a project you stop touching in month five.

## 14.1 The pyramid

Five tiers, not three. Pretending otherwise would misrepresent where the work is.

```
                    ┌──────────────┐
                    │  5. Eval     │  nightly, needs key, NON-BLOCKING
                    │  ~30 cases   │  scored, not pass/fail
                    ├──────────────┤
                    │  4. HIL      │  on the Pi, Phase 11, NON-BLOCKING
                    │  ~40 cases   │  real adapters, same contracts
                 ┌──┴──────────────┴──┐
                 │  3. Scenario       │  whole system, all fakes
                 │  ~25 cases, <5s    │  UC-01..UC-07
              ┌──┴────────────────────┴──┐
              │  2. Contract             │  every port × {real, fake}
              │  ~60 cases               │  P6
        ┌─────┴──────────────────────────┴─────┐
        │  1. Unit — pure                      │  domain, policy, scoring,
        │  ~400 cases, whole tier < 2s         │  state table, personality
        └──────────────────────────────────────┘
```

Tiers 1–3 are **blocking and hardware-free**. Tiers 4–5 are informational (PMP §10.4). That split is deliberate: a red build must always mean *you broke something*, never *the Pi is unplugged* or *the model had an off day*. A CI signal you learn to ignore is worse than no CI signal.

## 14.2 Unit tests — where the bugs actually are

Tier 1 is pure functions over `robot/domain/` and the pure cores of services. No async, no I/O, no clock. Milliseconds.

**The state transition table (§3.10.3).**

```python
@pytest.mark.parametrize("frm,event,expected", TRANSITION_CASES)
def test_transition_table(frm, event, expected):
    assert next_state(frm, event) == expected

def test_no_undocumented_transitions():
    """Every (state, event) pair not in §3.10.3 raises IllegalTransition."""
    for state, event in product(RobotState, EVENT_TYPES):
        if (state, event) not in TRANSITION_TABLE:
            with pytest.raises(IllegalTransition):
                next_state(state, event)
```

That second test is the valuable one. §3.4.2 claimed *"ninety percent of the bugs in a system like this are illegal state transitions"* — this is the test that cashes it, and it's exhaustive over the cross product in about 4 ms.

**The policy gate (§10.2).** The single highest-value test file in the project:

```python
POLICY_CASES = [
    # id                      ctx overrides                      expect
    ("quiet_hours_blocks",    {"now": at("23:30")},              veto("quiet_hours")),
    ("mid_conversation",      {"state": SPEAKING},               veto("state")),
    ("empty_room",            {"presence_age_s": 900},           veto("presence")),
    ("user_on_a_call",        {"ambient_speech_s": 90},          veto("ambient_speech")),
    ("global_cooldown",       {"last_proactive_s": 300},         veto("cooldown")),
    ("budget_spent",          {"delivered_today": 5},            veto("daily_budget")),
    ("coffee_morning",        {"now": at("07:55")},              deliver()),
    ("quiet_beats_everything",{"now": at("23:30"), "presence_age_s": 1}, veto("quiet_hours")),
]
```

R-08 — "proactive robot is annoying, user disables it, core value dies" — scores 15 and is the risk this project is least equipped to notice in the field. Because §10.2 made the gate a **pure function over a `PolicyContext`**, the entire question collapses to this table. Every future "it interrupted me during X" becomes one new row, not an investigation across three services.

**Also tier 1:** §7.7's scoring (recency decay at known Δt, min-max normalisation, top-k ordering), §6.5's `PersonalityEngine.compose`, §7.8's supersession decision logic (with a `FakeTextModel`), §6.4's instruction assembly and its cache-prefix ordering.

## 14.3 Fakes, not mocks

**Policy: `unittest.mock` is banned outside `tests/adapters/`.** This is not taste.

A mock asserts *"`servo.move_to` was called with 45°."* That couples the test to the implementation — refactor `MotionService` to use a gesture queue and the test breaks while the behaviour is unchanged. Do this a hundred times and your suite becomes a tax on refactoring, at which point you stop refactoring, and the design rots. That's how P6 and R-03 meet.

A fake is a working implementation. `FakeServo` records a movement trace; the test asserts *"the head ended up at 45°."* Refactor freely — the test only breaks if the robot stops nodding.

**The fakes live in `robot/adapters/`, not `tests/`.** They ship. §3.9.2: they *are* the simulator. Which means:

- They are first-class code, reviewed like production code, and they have **their own tests**.
- A fake that drifts from its real counterpart is a defect — caught by §14.4.
- P6 is enforceable: *"a port without a working simulator adapter is an incomplete port."*

The one permitted mock boundary is the OpenAI WSS, and even there we prefer `ReplayRealtimeClient` — a recorded session played back deterministically. Recorded once, replayed forever, with no key and no network. Tier 3 depends on it.

**Session fixture format (AVID-101).** A recorded session is a **directory** under `assets/sessions/<name>/` — a shipped runtime asset, reusing the `assets/cues/` / `CUE_FILES` convention (§6.9), because `ReplayRealtimeClient` is a real simulator mode (`realtime = "replay"`, §9.6) and not merely a test double. Each directory holds one `session.json` manifest plus the small WAV clips it references:

```json
{
  "format": 1,
  "events": [
    {"delay_ms": 0,   "type": "user_transcript",       "text": "...", "is_approximate": false},
    {"delay_ms": 300, "type": "assistant_transcript",  "text": "...", "item_id": "item_0"},
    {"delay_ms": 20,  "type": "assistant_audio_chunk", "item_id": "item_0", "wav": "turn0_a.wav"},
    {"delay_ms": 200, "type": "turn_done",             "usage": {"input_tokens": 320, "cached_input_tokens": 256, "output_tokens": 48}},
    {"delay_ms": 40,  "type": "session_closed",        "cause": "network"}
  ]
}
```

`type` selects a neutral `RealtimeEvent` member (`core/realtime.py`); its remaining keys are that member's fields. `delay_ms` is the gap *before* the event, replayed on the injected `Clock` (never wall time), so a `FakeClock` steps the whole timeline instantly and deterministically. `assistant_audio_chunk.wav` is a filename resolved beside the manifest and loaded as one `AudioChunk` (24 kHz mono S16_LE, the §6.2.4 playback format). Three fixtures ship — a normal two-turn conversation, a barge-in (approximate user transcript mid-reply plus a post-truncation delta), and a mid-turn session loss. The openai adapter's `--capture` mode (AVID-105) writes this exact format from a live session, so the fixtures cannot drift from real API behaviour.

## 14.4 Contract tests (P6)

One test suite per port. It runs against **every** adapter — real and fake — and they must be indistinguishable through the interface. This is what makes the HAL an abstraction rather than an aspiration.

```python
@pytest.fixture(params=["fake", "real"])
def servo(request) -> Servo:
    if request.param == "real":
        if not on_pi():
            pytest.skip("hardware")
        return Pca9685Servo(config)
    return FakeServo()


class TestServoContract:
    async def test_clamps_beyond_limits(self, servo):
        """§3.9.1: clamping is the ADAPTER's job, not the caller's."""
        await servo.move_to(0, angle_deg=400, duration_ms=100)
        assert servo.position(0) <= servo.limits(0).max_deg

    async def test_move_is_cancellable(self, servo):
        """§3.9.1: a preempting gesture must cancel an in-flight move."""
        task = asyncio.create_task(servo.move_to(0, 90, duration_ms=2000))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_relax_deenergises(self, servo):
        await servo.move_to(0, 45, duration_ms=100)
        await servo.relax(0)
        assert not servo.is_energised(0)     # M9's gate: no buzz when idle
```

In CI: fakes only, and the `real` params skip. On the Pi: both. **Same file, same assertions.** When M2's gate says *"every real adapter passes the identical contract test suite as its fake,"* this is literally that sentence in code.

The subtle payoff: writing the contract forces you to decide what the port actually promises. `test_clamps_beyond_limits` is where §3.9.1's argument — that safety invariants belong at the lowest layer that can enforce them universally — stops being prose and becomes enforceable.

## 14.5 Scenario tests — the important tier

§3.9.2: the simulator is not a separate program. It is `main.py` with `config/sim.toml`. So the harness isn't a simulator; it's a **scenario DSL** over the real composition root.

```python
async def test_uc03_coffee(sim: Sim):
    await sim.user_says("I drink coffee every day at 8 AM")
    await sim.expect_fact(kind=FactKind.ROUTINE, matching=r"coffee")
    await sim.expect_event("memory.fact_stored")

    await sim.restart()                    # ← process restart. §7's whole point.
    await sim.set_presence(True)
    await sim.advance_clock_to("07:55")

    await sim.expect_event("behavior.trigger_fired")
    await sim.expect_utterance(matching=r"coffee")
```

That is UC-03 — the entire product pitch — as a test that runs in about **40 milliseconds** on a laptop with no hardware, no network and no API key.

Three things make it possible, and each was a decision made hundreds of lines earlier for reasons that looked local at the time:

- **`Clock` is a port** (§9.3) → `advance_clock_to` instead of waiting until morning.
- **`ReplayRealtimeClient`** (§14.3) → deterministic model responses, no key.
- **All adapters are config-switched** (§9.6) → same binary, six lines of TOML.

M10's gate criterion reads *"the coffee scenario, end to end, unprompted."* This test is that criterion, mechanised, runnable on every commit from **week one** — months before the servo exists, before the Pi arrives, before there is a face.

`sim.restart()` earns special mention. It tears down and rebuilds the composition root against the same SQLite file. Everything that doesn't survive it isn't memory — it's a cache. That single line is what M7's gate ("tell it 20 facts, restart the process, recall all 20") reduces to.

The scenario tier covers UC-01 through UC-07, including the ugly ones: §3.7.6's network-loss-and-recover (`sim.drop_connection()`), §6.2.4's barge-in (`sim.user_interrupts_at(ms=400)`), and §7.8's supersession (coffee → six months → tea).

## 14.6 Hardware-in-the-loop

**Honest position: automate this at Phase 11, not before.**

The instinct is a self-hosted GitHub runner on the Pi doing nightly `main`. It's the right long-term answer and it's in PMP §10.4's table as non-blocking. But standing it up in month two means maintaining CI infrastructure on a device that is itself the thing under test, for a solo part-time project, before there's anything worth running nightly. That's R-03 wearing a DevOps hat.

Until Phase 11, the load is carried by the PR template's **"hardware verified y/n"** checkbox (PMP §11.3) and by DoD's *"if it's a hardware feature: verified on the actual Pi, not just simulated"* (PMP §10.2). Manual, honest, sufficient.

When it does go in, it runs exactly §14.4's contract suite with the `real` params active, plus three things only hardware can answer: thermals under sustained vision+audio (R-09), SD write volume over 24h (R-05, SPK-2), and servo brown-out under stall (R-04, SPK-4).

## 14.7 AI evaluation

Tier 5. **Nightly, needs a key, never blocking**, because these are non-deterministic and a flaky red build teaches you to ignore red builds.

Scored and tracked over time rather than pass/fail:

| Suite | Measures | Guards |
|---|---|---|
| **Retrieval quality** | 50 fact/query pairs → recall@5 | R-07 |
| **Extraction quality** | 30 transcripts → did `remember_fact` fire correctly? | O2, §7.6 |
| **Supersession** | 15 contradiction pairs → correct fact wins? | §7.8 |
| **Personality adherence** | Same 20 prompts × 2 configs → LLM-as-judge scores separation | M6 gate |
| **Cost regression** | Tokens/turn on a fixed transcript | **§6.10.6** |

Two of these deserve emphasis.

**The retrieval eval set is built at M7's *start*, not after.** R-07's mitigation says so explicitly. Fifty fact/query pairs written before the retrieval code exists — because written afterwards, you unconsciously write the queries your implementation already answers. Retrieval quality is measurable; treat it as a metric, not a vibe.

**The cost regression test is §6.10.6's smoke detector, as a test.** Fixed transcript, assert tokens-per-turn hasn't grown, assert the cached-input ratio hasn't collapsed. §6.10.3's whole argument was that broken caching looks identical to working caching until the invoice — $12/month vs $85/month, silent. This is the tripwire, and it fires in a nightly report instead of a monthly bill.

`ReplayRealtimeClient` covers everything else, so tiers 1–3 stay key-free.

## 14.8 Coverage, and what it means

**≥90% of non-adapter code** — DoD, blocking (PMP §10.4).

"Non-adapter" is the operative word. Adapters are thin I/O wrappers whose correctness is established by §14.4's contract suite running against real hardware; line-covering `Pca9685Servo` in CI with a fake I²C bus measures nothing. Coverage is therefore computed with `robot/adapters/**` excluded, and that exclusion is a design statement: **the adapters are where we don't have confidence from unit tests, which is exactly why the contract suite exists.**

100% is not the target and would be a bad one — it buys tests written to satisfy a number, which is how you get a suite that's expensive to maintain and catches nothing.

### What is deliberately not tested

Naming this prevents someone (you, in month six) from feeling guilty about it:

| Not tested | Why |
|---|---|
| Whether `gpt-realtime` gives good answers | Vendor's problem. We test *our* handling of its output. |
| Pixel-exact rendering | `FakeDisplay` emits a PNG sequence as a CI artifact. **Eyeball it.** A human glance beats an image-diff test that fails on every intentional change. |
| Actual servo travel | Physics. HIL, or your eyes. |
| Silero's VAD accuracy | Vendor's. We test that our gate *acts* on its verdict. |

## 14.9 The automated gates (PMP §10.4, specified)

Each principle in §3.2 that lacks a mechanism will be violated by you, at 1 a.m., in month six, for a good reason. These are the mechanisms.

| Gate | Enforces | How |
|---|---|---|
| `import-linter` | **P1, P5** | Layer contracts: `domain` imports nothing; `ai` may not import `motion`; etc. |
| composition-root grep | **P3** | `Adapter(` outside `main.py` / fixtures → fail |
| config grep | **P7** | `os.environ` outside `core/config.py` → fail |
| `PYTHONASYNCIODEBUG=1` | **P8** | Slow-callback >50 ms → fail. Carve-out: one-time real-hardware device init/teardown (fixture boundary, on-Pi only) is threaded, so exempt — see §3.8.3, `tests/conftest.py` (AVID-57) |
| `event-catalog-drift` | **§9.1.5** | Registry vs §9.1.3's tables → diff → fail |
| `pytest` on 3.11 **and** 3.13 | **ADR-008** | A 3.13-only feature breaking the Pi caught in CI, not on the desk |

P8's gate is the one that pays for itself fastest. It catches the accidental blocking call *at the commit that introduces it* — the only moment it's cheap to fix. Found in month five, "something in the audio path stutters occasionally" is a week.

## 14.10 M0, in order

M0's gate: *"An event published in a test travels through the bus to a fake display, which asserts a frame. `pytest` green on a laptop. `import-linter` fails a deliberate violation."*

Eight IED. **No hardware.** Buildable tonight, on a train, on the laptop you already own, while the Pi is out of stock in Beirut.

1. Repo, `uv`, `ruff`, `mypy --strict`, `pytest`, `pyproject.toml` with `requires-python = ">=3.11"` (ADR-008).
2. `domain/`: `Event` envelope (§9.1.1, **both clocks**), `RobotState`, `Affect`, the transition table (§3.10.3).
3. Tier-1 tests for the transition table, including `test_no_undocumented_transitions`. *Suite is now green and worth running.*
4. `core/event_bus.py`: publish, subscribe-with-name, concurrent dispatch, swallow-and-report handler failures (§3.5.2), bounded queues with per-subscriber policy (§3.5.5).
5. Bus tests: **a raising subscriber must not kill the publisher.** This is the single most important reliability property in the system (§3.5.2) — write the test that proves it before anything depends on it.
6. `core/ports.py`: the Protocols (§3.9.1, §9.3).
7. `FakeDisplay` → PNG sequence. `FakeClock`. `FakeEventBus` isn't needed — the real one is already fast and pure.
8. `import-linter` contracts + **a deliberate violation, confirmed failing, then reverted.** The gate says "fails a deliberate violation," not "is configured." Prove the mechanism, don't trust it.
9. `main.py` composition root wiring the fakes. `uv run robot --config config/sim.toml` reaches IDLE and exits cleanly.
10. Record the 60-second gate demo (PMP §5.1). Commit it to `docs/demos/`.

Step 10 is not ceremony. PMP §9.3 puts R-03 — motivation decay — at the top of the register, scoring 20, above every technical risk. In month eight, `docs/demos/m0.mp4` is the artifact that proves this was real.

---

*Sections 4, 5, 11–13, 15, 16 to follow. Next drafting priority: §4 (Hardware Design) once the board arrives — the BOM has moved (Pi 5 4GB, USB mic, MAX98357A) and §4.3's power budget is SPK-4's input; and §11 (Performance Engineering), which formalises the latency budget §2.8.1 currently carries alone.*
