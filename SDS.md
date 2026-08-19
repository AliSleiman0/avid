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
 3.6.5 Person detection — ADR-013
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
10.2 The central separation
10.3 Scheduler
10.3.1 A booking is a claim about a moment
10.3.2 The clock that books is not the clock that waits
10.4 The interruption policy
10.5 Ignore backoff
10.6 Log every decision
10.7 Proactive turn initiation
10.8 What it says

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
- YuNet face-detection model card (OpenCV Zoo, `face_detection_yunet_2026may`) — ADR-013
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
| ADR-010 | WebSocket transport for the Realtime session, not WebRTC | Accepted — see §6.2.1 |
| ADR-011 | Semantic memory on local `all-MiniLM-L6-v2` via ONNX Runtime, 384-d | Accepted — see §7.4 |
| ADR-012 | Faces compose to RGB888 bytes in the stdlib; no drawing-library dependency | Accepted — see §3.6.4 |
| ADR-013 | Person detection is YuNet face detection on the ONNX Runtime we already ship | Accepted — see §3.6.5 |

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
| `behavior` | `trigger_fired`, `proactive_suppressed`, `proactive_delivered`, `trigger_disabled` |
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
| `BehaviorService` | Decide when the robot should speak first. | `system.started`, `system.degraded_*`, `state.transitioned`, `audio.speech_*`, `conversation.user_transcribed`, `vision.presence_*`, `memory.fact_*`, clock | `behavior.*` |

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

### 3.6.5 Person detection — ADR-013

**Person detection is face detection, running on ONNX Runtime — the inference runtime this project already ships.** The model is pinned, the way `[ai] model` is pinned to a dated snapshot:

| | |
|---|---|
| Model | **YuNet**, `face_detection_yunet_2026may.onnx` (OpenCV Zoo, libfacedetection), pinned at revision `26cc381e` |
| Licence | MIT (Shiqi Yu et al.) |
| Size | ~230 KB — small enough to be an afterthought, large enough that it is **not committed** (§14.4's rule; `tools/fetch_face_model.py` provisions it to `/var/lib/robot/models/`) |
| Input | `[1, 3, height, width]` NCHW float32, 0–255, **no mean subtraction and no scaling**, channel order **BGR**. Both dimensions must be multiples of 32 |
| Output | 12 tensors — `cls_`/`obj_`/`bbox_`/`kps_` at strides 8, 16 and 32, each `[1, anchors, C]`. We consume the first three and discard keypoints |
| Measured cost | **157.3 ms** end-to-end per frame at 640×480, one intra-op thread, on the Pi (#221, revised by #277 — 320×256 costs 42.6 ms but detects nobody) |

**⚠️ The `2026may` export specifically, and the distinction is not cosmetic.** The `2023mar` files sitting beside it in the same directory — including the int8 variants — are statically shaped `[1, 3, 640, 640]` and **reject any other geometry outright** (measured: `INVALID_ARGUMENT` on 640×480, 320×320 and 256×320 alike). Only `2026may` declares dynamic spatial axes. An earlier draft of this ADR pinned `2023mar` and argued that 640×480 needs no resize because it is already stride-aligned; that was true of the geometry and false of the artifact, and #221 found it by loading the file rather than by reading about it.

Against §2.7.1's budget — *"Vision must not exceed 1 core; run detection at ≤5 fps, not 30"* — the 5 fps period is 200 ms, and the measured numbers are:

| input | inference | end-to-end `detect()` | share of a 200 ms period | detects a seated person |
|---|---|---|---|---|
| 640×480 (native, **shipped**, `detector_scale = 1`) | 152.1 ms | 157.3 ms | 79% | **84% of frames ≥ 0.6** |
| 320×256 (`detector_scale = 2`) | 30.2 ms | 42.6 ms | 21% | **0% — never** |
| 224×160 | 13.7 ms | 18.9 ms | 9% | not retested |

⚠️ **The final column was added by #277, and it reverses this ADR's original conclusion.** `detector_scale = 2` shipped on the strength of the cost columns alone. Measured against a person actually sitting at the desk — well framed at 93×116 px, ordinary office light — it detected them in **0 of ~75 frames across three runs**, never once clearing even the adapter's own 0.3 floor, while `scale = 1` on the identical frames scored 0.65 mean / 0.80 peak. The earlier claim that *"detection quality is flat across the three rows at desk distance (peak confidence 0.93 for a 120 px face)"* was quoted in **model-input** pixels: 120 px at the model input is ~240 px at full resolution, roughly 2.5× closer than anyone sits. It was prose in a table, never an assertion, which is why it survived being wrong for a milestone.

**The decimation method was not the cause, and #221's finding on it stands.** Re-tested on live frames through identical downstream code, a 2×2 box filter scored **0.054** against subsampling's **0.053** — indistinguishable, exactly as measured. What fails at half resolution is the *absolute* face size reaching the model, not the detail lost in decimating.

**Capture is no longer the larger term.** Re-measured at `scale = 1`: capture **26.8 ms**, detect **157.3 ms**, combined **184 ms median / 247 ms max**. (The 81.9 ms capture figure was taken at 5 fps under a different duty cycle; it should be re-confirmed rather than carried forward.) 247 ms does not fit a 200 ms period, so **`[vision] fps` drops to 3** — a 333 ms period the worst case fills to 74%. §2.7.1 budgets "≤1 core at **≤5 fps**", so sampling slower stays inside the budget; what it costs is reaction time, which `gain_window_s` absorbs (1.2 s, still 3–4 frames).

**The lesson this ADR now carries.** Every automated M8 criterion — CPU, fps, thermals, event counts — is satisfiable by a robot that detects nobody, and the resource run behind them was recorded in an empty room. A cost table with no accuracy column is a measurement of the wrong thing, and `tests/core/test_vision_config_is_usable.py` now asserts the relationship rather than describing it.

**Threads: one.** Measured at 320×256: 1 thread → 30.2 ms at 1.00 core; 2 threads → 25.0 ms at 1.70 cores. Seventeen percent of latency for seventy percent more CPU is a bad trade against a one-core budget with 85% of the period idle. `SileroVad` also uses 1 and `LocalMiniLmEmbedder` uses 2 — the rule is that each adapter measures its own number, never that the numbers differ.

**Why ONNX Runtime.** It adds **no new heavy dependency**: `onnxruntime` is already in the `pi` extra for `SileroVad` (§6.3) and `LocalMiniLmEmbedder` (§7.4, ADR-011). `tools/fetch_minilm.py` is an existing model-fetch pattern to mirror rather than invent — pinned revision, SHA-256 verified, atomic replace. And `SileroVad` already demonstrates the `SessionOptions` treatment the ≤1-core budget demands (§3.8.2), which matters more here than anywhere: the default is one *spin-waiting* thread per core, and this project has paid for that default twice.

**The preprocessing is three steps and every one of them was measured rather than reasoned.** YuNet's strides are 8/16/32, so both input dimensions must be divisible by 32 — and the rig's negotiated capture geometry (`[camera] 640×480`, ov5647, contract-proven at M2) already is, which is why the *decimation factor is an integer* and the inverse coordinate map is a single multiply with no rounding, no interpolation kernel and no half-pixel convention to get wrong. Padding to the stride goes on the right and bottom only, never centred, for the same reason: no offset to forget.

Two of the three cost more than they looked:

- **The downscale is plain subsampling, not a box filter.** The obvious implementation — a 2×2 box mean, three lines of numpy — costs **49.6 ms on the Pi, more than the inference it feeds**, against **1.3 ms** for subsampling. Scored across five face sizes the detector could not tell them apart (peak confidence within ±0.005): faces are low-frequency structure, so the aliasing a box filter suppresses is not aliasing this model was reading. **Independently re-confirmed by #277** on live frames with a person present — box filter 0.054 vs subsampling 0.053 — which is what cleared decimation as the cause of that defect. At `detector_scale = 1` the step is a no-op anyway; it applies whenever a future rig negotiates a larger capture geometry.
- **The channel swap is the highest-risk line in the milestone.** YuNet trains through OpenCV's `blobFromImage` on BGR with `scalefactor=1.0`, so the swap *is* the normalisation. Fed the identical image with the channels reversed the model returns **8 detections against 57**, at entirely plausible confidences — it does not fail, it quietly loses most of its eyesight. That is exactly the failure mode this milestone fears most, where the detector sees almost nothing and every downstream test still passes, so the order is pinned by an assertion on the tensor rather than by a comment.

**Rejected alternatives.** A rejection nobody wrote down gets re-proposed in month eight:

| Option | Verdict |
|---|---|
| **MediaPipe Face Detection** | Rejected. Good out of the box, and it is what §1.5 used to reference — but it is a large new ARM dependency that duplicates an inference runtime we already ship, and it brings **its own threading** to tame against §2.7.1. Two thread pools to reason about instead of one, bought with a second model-distribution story. |
| **OpenCV Haar cascade** | Rejected. Cheapest to wire and needs no model fetch, but it is unstable on non-frontal faces — and **instability is precisely what the gate forbids**. The hysteresis filter (§9.1.3) exists to absorb *reality*: occlusion, someone leaning out of frame. Spending it instead on a weak detector means the filter is compensating for us rather than for the room, and the windows would have to widen until the robot notices you slowly. |
| **Motion / background subtraction** | Rejected. No ML, lightest possible, and wrong: it detects *movement*, not people. A curtain fires it and a still person does not — which is the exact population this robot faces, someone sitting at a desk reading. It also **cannot populate `vision.face_detected {count, largest_bbox}`**, which is normative in §9.1.3. |
| **A full face *recognition* model** | Out of scope, and deliberately so — see below. |

**This is presence, not identity.** The robot learns that *a person* is there, never *which* person. Face recognition is a §7.2 "Could" with materially different privacy consequences (§13), and the boundary is stated here so nothing later drifts across it by accident: no embeddings of faces are computed, none are stored, and the only thing that reaches the database is what the conversation put there. The committed artefacts of this milestone are bounding boxes and confidences — **never images** (§13, `assets/vision/README.md`).

**What M8 delivers, and what it does not.** UC-04 (§2.5) reads *"user sits down; robot notices **and greets**."* Greeting means speaking first, which is `BehaviorService`, quiet hours and the interruption policy — **all of them M10** (§10); §3.7.5 was a table-of-contents entry for exactly that reason, and M10 wrote it. M8 delivers the *notices* half, demonstrated by the `SLEEPING → IDLE` wake (§3.10.1). PMP §5.2's register line is amended to match, so this milestone is neither dragging a slice of M10 forward nor sealed against a criterion it knowingly does not meet.

**A note on where `BBox` lives.** §3.9.1 lists it in the HAL vocabulary and `avid.core.hal.BBox` is the spelling every port, adapter and service uses — but the type is *defined* in `avid/domain/vision.py` and re-exported from `core/hal.py`. The reason is P1, mechanically: the layers contract puts `core` above `domain`, `vision.face_detected` is a domain event that must name `BBox` to type its payload, and defining it in `core` would make that event the first `domain → core` import in the project. Re-exporting costs nothing and keeps the dependency rule at zero exceptions — the same move `StateTransitioned` makes for a different reason (`domain/state.py`).

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
 │       │         │  state.transitioned(LISTENING)│         │         │
 │       │         ├─────────┼──────────┼──────────┼────────►│         │
 │       │         │         │          │          │  draw listening   │
 │       │         │         │          │          │         │         │
 │       │         │ local VAD: turn end (silence_hold_ms)    │         │
 │       │         │  audio.speech_ended → transitioned(THINKING)       │
 │       │         ├─────────┼──────────┼──────────┼────────►│         │
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

### 3.7.4 Proactive trigger firing (UC-03)

```
Clock        Scheduler      BehaviorSvc      PolicyGate     TriggerRepo    Bus        ConvSvc
  │              │               │               │               │          │            │
  │ 07:55        │               │               │               │          │            │
  ├─────────────►│               │               │               │          │            │
  │              │ heap pop: due(trigger 7)      │               │          │            │
  │              ├──────────────►│               │               │          │            │
  │              │               │ build PolicyContext           │          │            │
  │              │               │  now / state / presence_age_s │          │            │
  │              │               │  ambient_speech_s / cooldown  │          │            │
  │              │               ├──────────────►│               │          │            │
  │              │               │               │ 6 rules, first veto wins │            │
  │              │               │◄─ Delivered ──┤               │          │            │
  │              │               │               │               │          │            │
  │              │               │ mint correlation_id (§9.1.1)  │          │            │
  │              │               │ proactive_log(outcome=delivered)         │            │
  │              │               ├──────────────────────────────►│          │            │
  │              │               │ behavior.trigger_fired{trigger_id, fact_id}           │
  │              │               ├─────────────────────────────────────────►│            │
  │              │               │ state.transition(BEHAVIOR_TRIGGER_FIRED) │            │
  │              │               ├─────────────────────────────────────────►│            │
  │              │               │               │               │  IDLE → THINKING       │
  │              │               │               │               │          ├───────────►│
  │              │               │               │               │          │  §10.7 opens
  │              │               │               │               │          │  the session
  │              │               │               │               │          │            │
  │              │        ── OR, if any rule vetoed ──           │          │            │
  │              │               │◄─ Suppressed("quiet_hours") ──┤          │            │
  │              │               │ proactive_log(outcome=suppressed,        │            │
  │              │               │   reason="quiet_hours", utterance=NULL)  │            │
  │              │               ├──────────────────────────────►│          │            │
  │              │               │ behavior.proactive_suppressed{rule}      │            │
  │              │               ├─────────────────────────────────────────►│            │
  │              │               │  (no session, no audio, but LOGGED)      │            │
```

Three things this diagram commits the code to. **The gate is consulted before anything observable
happens** — no session is opened, no state moves, and nothing reaches a speaker until
`evaluate_policy` has returned `Delivered`; a suppressed proposal is indistinguishable from silence
to everyone except `proactive_log`. **Both arms write a row.** The suppressed arm is not an early
return: §10.6's whole argument is that "it never fired" and "it fired and was vetoed forty times"
look identical from outside the database, and only one of them means rule 4 is too aggressive.
**`BehaviorService` both publishes the event and drives the transition**, exactly as `AudioService`
does at the other turn origin — `StateManager` subscribes to nothing (§9.2), so the publisher of a
fact is always the caller of `transition()`.

### 3.7.5 Presence detected → greeting (UC-04)

The half of UC-04 M8 deliberately left undone: M8 delivers *notices*, M10 delivers *greets*.

```
Camera     PresenceSvc     StateMgr      BehaviorSvc     PolicyGate     ConvSvc
  │             │              │              │               │            │
  │ frames      │              │              │               │            │
  ├────────────►│              │              │               │            │
  │             │ hysteresis: gain_window_s of detections     │            │
  │             │ vision.presence_gained{confidence}          │            │
  │             ├──────────────┬─────────────►│               │            │
  │             │              │              │ last_present_at = now      │
  │             │ transition(VISION_PRESENCE_GAINED)          │            │
  │             ├─────────────►│              │               │            │
  │             │              │ SLEEPING → IDLE (§3.10.3)    │            │
  │             │              │ state.transitioned           │            │
  │             │              ├─────────────►│               │            │
  │             │              │              │ state = IDLE  │            │
  │             │              │              │               │            │
  │             │              │  presence-kind trigger due?  │            │
  │             │              │              ├──────────────►│            │
  │             │              │              │  rule 2 now passes (IDLE)  │
  │             │              │              │  rule 3 now passes (age 0) │
  │             │              │              │◄─ Delivered ──┤            │
  │             │              │              │ behavior.trigger_fired     │
  │             │              │              ├───────────────────────────►│
  │             │              │              │               │  greet, §10.7
```

The ordering is the point, and it is why §10.4's rule 2 vetoes anything that is not `IDLE`. Presence
is what *wakes* the robot — `(SLEEPING, VISION_PRESENCE_GAINED) → IDLE` is a shipped row — so by the
time a presence-kind trigger is evaluated the machine has already left SLEEPING. A trigger never
needs to wake anything, which is why there is no `(SLEEPING, BEHAVIOR_TRIGGER_FIRED)` row and why
adding one would be adding a lie: rule 3 requires presence newer than `presence_window_s` (300 s),
and `IDLE → SLEEPING` requires ten minutes *without* presence, so "asleep **and** recently seen"
is not a reachable state.

Note also what M10 does **not** add here: nothing distinguishes *who* was detected. §8's presence
signal is "someone is there", per-person recognition was ruled a non-goal at M8, and §10 needs
nothing more than that.

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
| ONNX face-detection inference (`OnnxFaceDetector`, ADR-013) | CPU-bound, releases GIL | `run_in_executor`, dedicated single-thread pool, ≤5 fps |
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
    async def play(self, chunk: AudioChunk) -> int: ...  # ms ACCEPTED by the device; honours chunk.sample_rate
    async def play_file(self, path: Path) -> int: ...    # degraded-mode WAV bank; ms accepted
    async def stop(self) -> None: ...                     # barge-in


class VoiceActivityDetector(Protocol):   # §6.3 / ADR-007 — the session gate
    def is_speech(self, frame: AudioChunk) -> bool: ...  # sync; MUST return <5 ms (§9.3)


class FaceDetector(Protocol):            # §3.6.5 / ADR-013 — one frame in, detections out
    async def detect(self, frame: Frame) -> Sequence[Detection]: ...
    # async, not sync: inference is tens of ms, so the adapter offloads (cf. Embedder, not VAD).
    # It answers "what did this ONE frame contain", never "is a person present" — presence is a
    # decision over TIME and belongs to the pure hysteresis filter (§9.1.3, domain/vision.py).


class RealtimeClient(Protocol):          # the vendor boundary (§6.2, R-10)
    async def open(self, *, memory: Awaitable[str] | None = None) -> None: ...  # cold session (§6.2.3); memory = §6.7-path-1 layer-4 block, gathered with the connect (#126)
    async def aclose(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ... # mic PCM up
    def events(self) -> AsyncIterator[RealtimeEvent]: ...      # neutral, typed, vendor-free
    async def truncate(self, item_id: str, audio_end_ms: int) -> None: ...  # barge-in (§6.2.4)
    async def cancel(self) -> None: ...                        # response.cancel
    async def send_tool_output(self, call_id: str, output: str) -> None: ...  # §6.6 return leg; then response.create


class TurnSink(Protocol):                # the ConvSvc↔AudioSvc audio seam (§9.1.4)
    def mic(self) -> AsyncIterator[AudioChunk]: ...            # captured PCM up
    async def play(self, chunk: AudioChunk, *, item_id: str) -> None: ...  # assistant PCM down
    async def end_response(self) -> None: ...  # normal completion → playback_finished, SPEAKING→IDLE
    async def interrupt(self) -> int: ...  # barge-in; returns played_ms the device ACCEPTED (§6.2.4)


class MemoryTools(Protocol):             # the §6.6 tool surface ConvSvc dispatches to (#125, ADR-004)
    async def remember_fact(self, text: str, kind: str, importance: int,
                            *, correlation_id: UUID | None = None) -> int: ...  # §7.6, durable-before-return
    async def recall(self, query: str, *, k: int = 5,
                     correlation_id: UUID | None = None) -> Sequence[Fact]: ...  # §6.7 path 2
    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int: ...  # §7.10 hard delete


class EpisodeStore(Protocol):            # the §7.5 raw-transcript tier EpisodeRecorder writes (#123)
    async def start_episode(self, correlation_id: UUID, *, at: int) -> None: ...  # ensure a row, started_at
    async def append(self, correlation_id: UUID, line: str, *, at: int) -> None: ...  # accumulate transcript, ended_at
    async def end_turn(self, correlation_id: UUID, *, at: int) -> None: ...       # turn_count++, ended_at
    async def prune(self, *, older_than: int, limit: int) -> int: ...             # bounded 90-day retention delete
    async def aclose(self) -> None: ...


class FactRepository(Protocol):          # durable fact storage, as MemoryService needs it (§8.3, #117)
    async def add(self, fact: Fact, *, embedding: bytes | None = None) -> int: ...  # returns the assigned rowid
    async def get(self, fact_id: int) -> Fact | None: ...
    async def fetch_live(self) -> Sequence[Fact]: ...              # non-superseded, idx_facts_live hot path
    async def mark_superseded(self, old_id: int, new_id: int, *, at: int) -> None: ...  # §7.8; paired CHECK
    async def delete(self, fact_id: int) -> None: ...             # forget() primitive, hard DELETE + cascade (§7.10)
    async def load_embeddings(self) -> Sequence[tuple[int, bytes]]: ...  # (id, BLOB) for the §8.5 boot rebuild
    async def keyword_search(self, query: str, *, limit: int) -> Sequence[int]: ...  # FTS5 ∪-half of §7.7, live ids
    async def aclose(self) -> None: ...
    # BLOB crosses as opaque `bytes`, ids as `int` — the port is numpy-free (§8.2, P1); the matrix is §8.5's.


class Retriever(Protocol):               # the §7.7 read path + its §8.5 write-through vector index (#120)
    async def rebuild(self) -> None: ...                          # boot reconciliation; SQLite is truth, matrix an index
    async def retrieve(self, query: str, *, correlation_id: UUID | None = None) -> tuple[int, ...]: ...  # hybrid, top-k
    async def similar(self, vector: Sequence[float], *, threshold: float, k: int) -> tuple[int, ...]: ...  # §7.8 near-dups
    def append(self, fact: Fact, embedding: bytes | None) -> None: ...  # write-through, after the row is durable
    def remove(self, fact_id: int) -> None: ...                   # write-through, after supersede/forget
    # retrieve() publishes memory.recall_completed ITSELF (§9.1.3); similar() publishes nothing (not a recall).


class TextModel(Protocol):               # the cheap off-turn-path text model for §7.8 (§9.4 catalog)
    async def judge_supersession(self, *, new_fact: str,
                                 candidates: Sequence[tuple[int, str]]) -> Sequence[int]: ...  # subset of input ids
```

`RealtimeClient` and `TurnSink` are the two M5 ports (AVID-100). `RealtimeClient` is the vendor blast radius: `ConversationService` depends only on it, the `openai`/`replay` adapters implement it, and it traffics in the neutral `RealtimeEvent` union (`UserTranscript` / `AssistantAudioChunk` / `AssistantTranscript` / `ToolCallRequested(call_id, name, arguments)` / `TurnDone(usage: TokenUsage)` / `SessionClosed`, defined in `core/realtime.py`) so no Realtime message shape ever crosses — if OpenAI changes the API, exactly one adapter changes (R-10). `ToolCallRequested` (#124) is the §6.6 tool-call seam: the adapter maps it off the vendor's `response.output_item.done` finalize frame, and `send_tool_output` returns the result and sends the mandatory `response.create` (§6.6's step-5 trap). The tool *dispatch* is `ConversationService`'s (#125): it parses the call and runs it against the injected **`MemoryTools`** port — never the concrete `MemoryService` (P2/P5) — so the composition root injects the service and `ConvSvc` names only the port. The three tool *declarations* (`TOOL_SCHEMA`, §6.6) and the §7.6 capability instructions ship in `services/tools.py` and are seeded into the session's cached prefix by `main` (a `remember_fact` on a barge-in-approximate turn is declined — §6.2.4/§7.6). `TurnSink` is how a turn's PCM crosses `ConvSvc ↔ AudioSvc` as a **direct call, never a bus event** (§9.1.4).

The five **memory ports** are M7 (AVID-114). `FactRepository` (#117) is durable fact storage behind `SqliteFactRepo`; `Retriever` (#120) is the §7.7 read path + its §8.5 write-through numpy matrix behind `HybridRetriever`; `Embedder` (#118/#119, §9.3) is text→vector behind `LocalMiniLmEmbedder`; `TextModel` (#122) is the cheap off-turn-path supersession judge behind an OpenAI text adapter; `MemoryTools` (#125) is the tool surface `ConversationService` dispatches to. `MemoryService` is a *service*, so it names only these ports and the composition root injects the concretes (P2/P5) — the numpy matrix, the FTS5 shadow and the vendor HTTPS client all stay on the adapter side of the boundary. Every method is `async` (SQLite and model inference are blocking I/O offloaded off the loop, P8) and every signature is numpy-free (`bytes` BLOBs, `Sequence[float]` vectors, `int` ids), so `core`/`domain` never import numpy (ADR-012).

**`FaceDetector` is M8 (#217, ADR-013), and it brings two types into the HAL vocabulary** beside `Frame`, `AudioChunk`, `DisplayFrame`, `CameraCaps` and `Axis`: **`BBox`** — a face's rectangle in **pixels of the frame that produced it**, top-left origin, `(x, y, w, h)` — and **`Detection {confidence: float, box: BBox}`**, one face seen once. Both are frozen/slotted/kw-only and stdlib-only like every other type in `core/hal.py`; no tensor, session handle or model detail crosses the port. The inversion is the usual one and it is worth naming here because it is easy to get backwards: the port is defined by **what `PresenceService` needs** — one frame in, this frame's faces out — never by what a detection library offers, which is why keypoints, landmarks, tracking ids and identity embeddings are all absent from a port sitting on top of a model that emits some of them. And it stops one step short on purpose: the port never answers *"is a person present."* That is a decision over time, it belongs to the pure hysteresis filter in `domain/vision.py` (§9.1.3), and a port that answered it would put the milestone's headline property behind a device boundary where it can be neither unit-tested nor replayed.

Three details worth defending:

**`Servo.move_to` clamps internally.** You could argue clamping belongs in the service. It doesn't: the safety limit is a property of the physical linkage, and if it lives in the service, a future second caller bypasses it. Safety invariants belong at the lowest layer that can enforce them universally.

**`Servo.axes` and `Camera.capabilities` exist** because of §3.9.3 — the system must run on a 1-servo rig, a 2-servo rig, and a simulator with 6, without conditionals scattered through the services.

**`Speaker.play()` returns milliseconds *accepted*, not `None` (AVID-91).** A write that moved no samples must not be indistinguishable from one that moved all of them. `pyalsaaudio`'s `write()` returns `-EPIPE` after an underrun having played nothing — measured on the Pi at the M4 gate as `write 1: 48000 @1.898s / write 2: -32 @0.000s / write 3: 48000 @1.909s` — so an adapter that discards its return drops every other utterance in silence, and the service then publishes `audio.playback_finished` for audio the room never heard. The figure therefore crosses the port, and `AudioService` accumulates *that* rather than recomputing it from the buffer it submitted. **Be exact about what the number is:** a device acknowledges frames **into its ring buffer**, not out of its DAC. So it is *accepted by the device* — exact about **drops**, and still optimistic by up to one buffer depth (~107 ms at 24 kHz with a 2560-frame buffer) about **photons**. That residual is the same error §6.2.4 step 3 already carries; closing it needs a device-level query and is deliberately out of scope. `play()` also **honours `chunk.sample_rate`/`channels`**: the chunk carries those fields precisely so a consumer can obey them, and an adapter that plays 16 kHz loopback PCM through its configured 24 kHz handle is 1.5× fast and a fifth high (measured: 6.00 s of capture echoed in 4.01 s). `[speaker] sample_rate` is therefore the **nominal** format the rig is tuned around, not a rate imposed on the audio — a deviation is logged once, never silently obeyed.

**`TurnSink.interrupt()` returns `played_ms`, not `None`.** Barge-in (§6.2.4) needs `audio_end_ms` to be *what the speaker actually accepted from us* — the frames the device took, not the frames the DAC has clocked out — and only the sink at the bottom of the playback path knows that. It is the sum of `Speaker.play()`'s returns, so it differs from what we *received* by everything that was dropped, and from what was *heard* by at most the buffer depth. Returning it from `interrupt()` puts the honest figure at the one layer that can measure it, exactly as `Servo.move_to` clamps at the one layer that owns the limit. Get it wrong and the model believes it said things the user never heard, which then poisons the conversation context. The method is named `interrupt`, not `stop`, because the real sink (`AudioService`, AVID-103) is also a `Service`, whose `stop()` unwinds the mic loop — a lifecycle shutdown is a different act from cutting a turn's playback, and the two must not collide. Its sibling `end_response()` is the *normal* end (no barge-in): the service calls it on `response.done`, and the sink then publishes `audio.playback_finished(truncated=False)` and drives `SPEAKING → IDLE`.

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
      └──────────┤ DEGRADED │◄──── from any state on session_lost,
                                    or THINKING on the 10 s think timeout (§6.9)
                 └──────────┘
```

"turn end detected" is **our own** `audio.speech_ended` — the local VAD gate's falling edge, `[gate] silence_hold_ms` after the last speech frame — not the model's transcript (§3.10.3). THINKING also has a back-edge to LISTENING on `audio.speech_started`, for the user who pauses and then keeps talking.

DEGRADED's exit is drawn here as a single arrow for legibility, but it returns to **LISTENING**, not to IDLE: the reopen is triggered by the user's own rising edge, so recovery always rejoins a turn already in flight (§3.10.3, AVID-162).

### 3.10.3 Transition table

Normative. Implemented as a frozen dict in `domain/state.py`, tested exhaustively as a pure function. Any transition not in this table raises `IllegalTransition` — loudly, in tests; logged-and-ignored in production.

| From | Event | To | Guard |
|---|---|---|---|
| BOOTING | `system.started` | IDLE | all required adapters healthy |
| IDLE | `audio.speech_started` | LISTENING | — |
| IDLE | `behavior.trigger_fired` | THINKING | §10.4 passes (rule 1 covers quiet hours, rule 2 is why IDLE is the only `From` state here) |
| IDLE | `vision.presence_lost` + 10 min | SLEEPING | — |
| SLEEPING | `vision.presence_gained` | IDLE | — |
| SLEEPING | `audio.speech_started` | LISTENING | — |
| LISTENING | `audio.speech_ended` | THINKING | — |
| LISTENING | timeout 30 s | IDLE | — |
| THINKING | `audio.speech_started` | LISTENING | the user resumed before the reply began |
| THINKING | `audio.playback_started` | SPEAKING | — |
| THINKING | timeout 10 s | DEGRADED | — |
| SPEAKING | `audio.playback_finished` | IDLE | — |
| SPEAKING | `audio.speech_started` | LISTENING | barge-in: stop playback first |
| LISTENING | `audio.playback_started` | SPEAKING | the overlap — both talking |
| SPEAKING | `audio.speech_ended` | THINKING | the overlap — the user stopped first |
| THINKING | `audio.playback_finished` | IDLE | the overlap — the reply drained after |
| *any* | `conversation.session_lost` | DEGRADED | — |
| DEGRADED | `audio.speech_started` / `audio.speech_ended` | DEGRADED | the local machine keeps running |
| DEGRADED | `system.degraded_exited` | LISTENING | recovery rejoins the turn that triggered it |

The barge-in row is the one that will bite you. The user interrupting the robot mid-sentence is *the* interaction that separates a companion from a kiosk, and it requires `Speaker.stop()` to be genuinely immediate — which is why it's on the port (§3.9.1) rather than being someone's afterthought.

**The turn-end edge is *our* falling edge, never the model's transcript — do not undo this.** LISTENING→THINKING hung off `conversation.user_transcribed` until the M5 bench measured what that actually is: a separate, slower transcription pass, arriving *after* the assistant's speech-to-speech audio and sometimes after `conversation.turn_ended` (t=52.482 playback vs t=53.594 transcript, `docs/demos/m5_evidence/trace_2026-07-26_streaming.log`). Because `audio.playback_started` is legal only from THINKING, every reply landed in LISTENING where it is illegal, SPEAKING became unreachable, the barge-in row above was dead code on hardware, and two bench runs scored zero barge-ins. `audio.speech_ended` is local, always fires, and needs no network — which is what §3.10.1's diagram has always called "turn end detected". The trap generalises: **an edge driven by a vendor event inherits that vendor's latency and ordering**, and a recorded fixture cannot reproduce a race the live API loses (§14.3), so replay CI was structurally incapable of catching it. `conversation.user_transcribed` remains a published fact in §9.1.3 — it simply drives nothing.

The THINKING→LISTENING row is its companion: a user who pauses past `[gate] silence_hold_ms` and then keeps talking was observed twice in the same trace. Without it the correction merely relocates the wedge from LISTENING to THINKING.

**The three overlap rows are an honest compromise rather than a model (AVID-161).** The model answers an earlier commit while the user has already begun their next utterance — measured at t=61.074, 0.9 s before our own falling edge — and for that window *both* are speaking. This machine has one axis and the robot has two mouths in the room, so no assignment of those rows is true; the set above is simply the one that leaves **no reachable illegal transition**, which is the only property worth optimising for here. Note especially that `SPEAKING + audio.speech_ended` goes to **THINKING and not to a SPEAKING self-loop**: the self-loop reads more naturally row-by-row and leaves SPEAKING sticky, so the *next* reply's `audio.playback_started` has no row and the wedge simply moves one step later. That failure — every row defensible alone, the composition dead-ending — is the one both AVID-158 and AVID-161 were, which is why the tests assert these as whole journeys rather than as rows. The real answers are a two-axis state model or AVID-163's echo cancellation, which makes the overlap impossible rather than legal.

**The two DEGRADED rows say what recovery means mid-turn (AVID-162).** Recovery is *rising-edge-driven*: `ConversationService._exit_degraded` has exactly one caller — its own `audio.speech_started` handler, after `open()` succeeds — because AVID-105 shipped without a background reconnect loop. So recovery never happens except with a turn already in flight, and the old IDLE target was a state the robot was never actually in. The recovery turn then drove `speech_ended`, `playback_started` and `playback_finished` from IDLE, all illegal, and the first turn after the robot had been broken came out **stateless**: no thinking face, no speaking face, and no state move behind a barge-in against that reply.

LISTENING is the only target that closes *both* continuations. A user still talking finds `LISTENING + audio.speech_ended`; a user whom a slow open outran finds `LISTENING + audio.playback_started` — the overlap row above, doing double duty — and that is the *common* case rather than the corner one, since AVID-157 measured `open()` at 1.5–6.7 s against a `silence_hold_ms` of a few hundred. THINKING reads better ("we sent the audio, we are waiting on the model") and dead-ends immediately: it has no `audio.speech_ended` row. The robot stays in DEGRADED for the whole open, which is what keeps LISTENING honest — a failed open publishes no `system.degraded_exited`, the speech edges are absorbed, and the robot goes on saying it is broken, because it is.

The absorbing row covers only the user's **own** two edges, deliberately. Both `audio.playback_*` triggers are driven solely from ConversationService's event pump, and **every** path into DEGRADED tears that pump down before DEGRADED is reachable — `_on_session_closed` when the socket drops, and the §6.9 think timeout (AVID-171) when the model never produces a first token. Both go through the same `_degrade` helper for exactly this reason, so a row for either trigger would be unreachable — and an unreachable row is a lie in a normative table. Note that `AudioService.interrupt` *does* run while degraded, cutting whatever playback the drop abandoned: it publishes `audio.playback_finished` as a **fact** but drives no trigger at all (§9.1.3), which is why no row is needed. ⚠️ **If a background reconnect is ever added it will recover with no turn in flight, and `system.degraded_exited` will be the wrong trigger for it** — that is a different fact and wants its own row, not a reused one.

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

**Optional-dependency groups (as built).** `pyproject.toml` declares three, each imported lazily inside its adapter(s) so every module still loads where the group is absent:

| Extra | Contents | Installed | Behind |
|---|---|---|---|
| `pi` | `adafruit-circuitpython-servokit`, `pyalsaaudio`, `onnxruntime`, `numpy<2`, `tokenizers` (#119) | on the Pi (`--extra pi`), never in CI | the HAL adapters + Silero VAD + the MiniLM embedder; the model blobs are Pi-gated (§14.4) |
| `openai` | `openai`, `websockets` (#105) | on a networked host (`--extra openai`); network-gated in CI | `OpenAIRealtimeClient` — the R-10 vendor boundary |
| `memory` | `numpy` (#120) | **in CI** (`--extra memory`) — a pure, platform-independent wheel | `HybridRetriever`'s §8.5 brute-force cosine matmul |

**Why numpy is an extra and not a core dependency** is ADR-012, restated as a build fact: **runtime dependencies stay `pydantic` alone** (§3.6.4). numpy is opt-in, lives only in the `memory`/`pi` adapters, and **never enters `domain`/`core`** — the ports cross vectors as `Sequence[float]` and BLOBs as `bytes` (§9.3), and the domain-purity import-linter contract fails the build if numpy ever appears there. CI installs `--extra memory` specifically so the ranking math *is* exercised, while the `mypy` job stays numpy-free (the `numpy.*` override resolves it to `Any`, so a laptop with numpy installed and CI without it type-check identically). numpy is capped `<2` **only in the `pi` extra** (AVID-57: a numpy-2 wheel shadows the apt `simplejpeg`'s 1.x C ABI and breaks `picamera2`); the `memory` extra is uncapped.

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
    "type": "realtime",                             # GA shape — see the note below
    "model": "gpt-realtime-mini-2025-12-15",   # PINNED. See §6.10.
    "instructions": composed_instruction_block,     # §6.4 — STATIC for session life
    "audio": {
        "input": {
            "format": {"type": "audio/pcm", "rate": 24000},   # API MINIMUM — adapter resamples up
            "transcription": {"model": "whisper-1"},          # or no user transcript ever arrives
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
                "create_response": True,
                "interrupt_response": True,
            },
        },
        "output": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "voice": "cedar",
        },
    },
    "tools": TOOL_SCHEMA,                            # §6.6 — STATIC for session life
    "max_output_tokens": 512,                        # §6.10 — a cost guardrail, not a style choice
    "truncation": "auto",
}
```

Notes on the non-obvious choices:

- **This is the GA shape, and the beta one is gone.** The beta interface — selected by an
  `OpenAI-Beta: realtime=v1` header, with a bare `"pcm16"` format string and no session `type` — is
  disabled server-side and closes the socket with `4000
  invalid_request_error.beta_api_shape_disabled`. Both `rate` fields are **required**; omitting one
  earns `missing_required_parameter: session.audio.output.format.rate`. This is R-10 and the §6.10
  volatility warning arriving exactly as predicted, and it cost one adapter and two config keys —
  the blast radius the architecture promised. Found on the adapter's **first live run** (#106 prep),
  because every `assets/sessions/` fixture and therefore all of CI ran against `replay`.
- **`audio.input.transcription` is not optional for us.** Realtime does not transcribe the user's
  speech unless the session asks it to. Without this key no
  `conversation.item.input_audio_transcription.completed` frame arrives, so `UserTranscript` never
  crosses the port and `conversation.user_transcribed` is never published — the robot answers aloud
  with **no record of what was said**, so §7.5/§7.6 have nothing to extract a memory from and the
  episode store keeps only the assistant's half. The fixtures all *record* that frame, which is why
  only a live session could reveal its absence. (The state machine no longer depends on it: since
  AVID-158 LISTENING→THINKING is driven by our own `audio.speech_ended` — §3.10.3.)
- **24 kHz is the API's floor, and our capture is 16 kHz — so the adapter resamples.** Anything
  lower is rejected outright (`integer_below_min_value`: "Expected a value >= 24000"), while
  capture cannot simply be raised: ADR-007's Silero gate accepts **only 8 or 16 kHz**. Both
  constraints are real and neither side can move, so `send_audio` converts 16 kHz → 24 kHz inside
  the adapter — a vendor format demand is exactly what an adapter exists to absorb (CLAUDE.md §3),
  and nothing upstream (mic, VAD, `AudioService`, the port) learns that 24 kHz matters. The
  resampler is pure stdlib, keeping the default runtime pydantic-only (ADR-012). It **refuses to
  downsample** rather than alias quietly — the #146 lesson, one layer down.
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
0. …and the frame is LOUDER than the running echo floor by [gate] barge_in_margin_db
1. Local VAD fires while assistant playback is in flight — at the rising edge, OR on any
   frame of an utterance the reply started talking over (AVID-161)
2. Speaker.stop()                    ← immediate; must be on the port (§3.9.1)
3. Compute audio_end_ms = how much the device ACCEPTED — the sum of Speaker.play()'s
   returns (frames ALSA took ÷ 48 bytes/ms at 24kHz mono 16-bit), NOT what we submitted
4. Send conversation.item.truncate(item_id, content_index, audio_end_ms)
5. Send response.cancel -- ONLY IF a response is still in flight (see below)
6. Mute inbound deltas for item_id until the next assistant item begins
```

**Step 0 exists because a VAD cannot tell you *whose* speech it is (AVID-159).** This section used to assume it could. It cannot, and it is not a detector quality problem: the robot's voice coming back through the mic **is** speech, and Silero is right to say so. The only discriminator left is loudness, so a rising edge that happens while the robot is talking counts as the user only if it clears `EchoFloor` — a running estimate of what the mic hears *while the robot is the one speaking* — by `[gate] barge_in_margin_db`.

The floor is adaptive rather than a configured `playback_level × coupling` constant, because while the assistant speaks what the mic hears **is** the echo: tracking it *is* the acoustic-coupling calibration, so speaker volume, mic gain, room and rig geometry all cancel out of the comparison and nothing needs re-measuring when the desk moves. Two rules make it honest — it never adapts on a frame it has judged to be the user (that would raise the bar under the speaker mid-sentence), and it is only ever consulted while the uplink is shut, so **ordinary turn-taking is never tested against the margin at all.**

**Step 5 is conditional, and the condition is the common case (AVID-178).** `response.cancel` with nothing in flight is an error — measured against the live API as `response_cancel_not_active`, *"Cancellation failed: no active response found"* — and **generation finishes long before playback does**, so by the time a user interrupts a reply they are still *hearing*, the model stopped generating seconds ago. Sending it unconditionally made every barge-in draw an error frame, which the adapter then misread as a session close (§6.2.5): ten sessions in eighty seconds of bench. The client therefore tracks `response.created`/`response.done` and skips step 5 when there is nothing to cancel.

⚠️ **Step 4 is deliberately *not* guarded the same way.** The same probe confirmed the API accepts `conversation.item.truncate` with our device-derived `audio_end_ms`; only step 5 was ever rejected. Guarding both would have been the tempting symmetric change and would have broken the half of barge-in that worked.

⚠️ **A wrong guard here fails silently.** If the tracking is broken no cancel is sent, the user hears nothing amiss — step 6's mute drops the in-flight deltas regardless — and the model goes on believing it said the whole reply, poisoning the context (trap 2 above) and billing output tokens for audio nobody heard. The skip is therefore logged, and a bench run is graded on *"every barge-in shows a sent cancel **or** a logged skip"*, never on the absence of errors alone.

**Step 1 says "at the rising edge *or* mid-utterance", and that is not a detail (AVID-161).** The reply can begin while the user is *already* talking, and such a user has no rising edge left to be judged on — so a check that only ran at the edge left the escape hatch unreachable in exactly the case that needed it: the robot talks over you, the half-duplex uplink shuts, and your words stop reaching the model until you give up and start again. The margin is therefore re-evaluated on every frame for the duration of an overlap, and the interrupt re-opens the uplink so the rest of the turn gets through.

Three things follow that are worth stating before anyone tunes this:

- **The margin is a measured number, not a constant.** Every playback episode logs the floor it saw and the closest any rejected edge came to clearing it, so every bench run is a calibration run. §16's AC-3 for the M5 gate records the value and the distance/volume it was measured at.
- **A very large margin is full half-duplex.** If the levels turn out not to separate, barge-in can be switched off by config alone, with no code change — and AC-3 waived, as M4's was.
- **This is a bet on a number nobody has measured.** §6.3 records the mic's own noise floor at −21 dBFS and a speech capture at rms −16.1 dBFS; amp→mic coupling was called "weak" at M4 bring-up and never quantified. If the suppressed-edge and genuine-barge-in populations overlap, **no margin can be tuned into working** and the answer is echo cancellation (AVID-163) — subtracting the playback signal we already own from the capture. The hard part there is not the filter but the clocks: the 16 kHz USB mic and the 24 kHz I2S amp are separate ALSA devices with independently drifting clocks, and AEC needs sample-accurate alignment.

**Step 1 says "playback in flight", not "state == SPEAKING", and the difference is not pedantry.** `AudioService` gates the interrupt on its own in-flight playback item, because `RobotState` is a *derived view* that can lag the speaker: the bench caught a reply to an earlier server-side commit beginning 0.9 s **before** our falling edge fired, which leaves the machine in LISTENING with the speaker live. Gated on the state, the interrupt was dead code in exactly that window — and it silently dropped the truncated `audio.playback_finished` that steps 4–6 below are triggered by, so the model was never told (AVID-158). The service that owns the speaker is the one that knows whether it is speaking.

Five traps, all of which will cost you an afternoon each if you meet them cold:

- **Step 6 is not optional.** Audio deltas already in flight keep arriving *after* truncation. Without muting by item ID you will hear the robot's cancelled sentence resume for ~200 ms after it should have stopped.
- **Step 3 must measure what the speaker *took*, not what we received.** There are **three** quantities here, not two: what we **received** from the model, what the device **accepted** (`Speaker.play()`'s return), and what the DAC **emitted**. Received-vs-accepted is the entire dropped-audio class — an underrun makes ALSA refuse a whole utterance while returning instantly (AVID-91) — and closing it is what we implement. Accepted-vs-emitted is the playback buffer depth (~107 ms at 24 kHz) and remains a bounded, knowingly-accepted over-report: do not build anything that assumes it is zero. Note step 3's formula above measures **accepted**; that is the deliberate choice, not an oversight. Getting this wrong makes the model believe it said things the user never heard — which then poisons the conversation context.
- **`conversation.item.truncate` also drops the transcript for the unplayed portion.** Audio/transcript alignment is imprecise, so the transcript you keep for memory extraction (§7.6) is approximate at the truncation boundary. Don't build anything that assumes it's exact.
- **Steps 4 and 5 may be *refused*, and a refusal is a log line, not a degrade** (AVID-178). The API answers a client event it dislikes with an `error` frame on a socket that stays open; treating that as a session close made every barge-in tear the session down. See §6.2.5.
- **Steps 2–6 span two tasks, and the playback episode is shared mutable state between them** (AVID-174). `play()` runs on `ConversationService`'s pump; `interrupt()` runs on `AudioService`'s own mic loop; step 6's mute is armed on a third context, a bus worker. Nothing serialises them. A barge-in landing inside `await Speaker.play()` used to clear the episode out from under the suspended writer, which then wrote its ms back into the *next* reply's total and dereferenced the episode that no longer existed — an `AssertionError` on the pump, an owned task nothing observed, so the conversation ended with the socket open and the log silent. Two rules close it and both are load-bearing: **the episode's close is synchronous and single-taker** (`_take_playback` contains no `await`, so exactly one of `end_response`/`interrupt` can ever finalize a given episode — otherwise both pass their guard, a second `audio.playback_finished` makes the model truncate a response that ended normally, and the echo-gate report runs twice, halving the suppression counts §6.3's margin is calibrated from); and **a write that outlives its episode is discarded, not written back** (a playback epoch, captured before the speaker await and re-checked after). A lock is the wrong instrument here: to help it would have to span a 100–200 ms delta write, and its contender is the mic loop, so it would park frame consumption behind the speaker. One consequence worth stating: ms the device accepted into a buffer `Speaker.stop` then closed are now excluded from `audio_end_ms`, so the accepted-vs-emitted over-report above **shrinks** — it does not grow.

## 6.2.5 An error frame is not a close — AVID-178

The Realtime API answers a client event it dislikes with an `error` **frame**, on a socket that stays open. Until AVID-178 the adapter mapped any such frame to `SessionClosed`, and the consequences ran all the way down: `_pump` → `_on_session_closed` → `conversation.session_lost` → DEGRADED → a canned "one sec, I lost my connection" → `client.aclose()`. **The bench measured ten sessions in eighty seconds**, every barge-in tearing down a working socket and announcing an outage to a user whose connection was fine.

**The normative rule: the socket is the authority on whether the session is alive.** An `error` frame is a complaint about *one client event*; a **close** frame ends the session, and `ConnectionClosed → SessionClosed(cause="network")` is the only path that mints it in the real client. An error yields **no** `RealtimeEvent` at all — it is logged and dropped inside the adapter, so nothing about it crosses the port.

**No error type is fatal, and that is deliberate.** A hard-coded list of "fatal" vendor error types is precisely what §6.10's volatility warning says will rot, and every fatal condition this project has actually met arrived as a close instead: the GA-shape migration closed with `4000 invalid_request_error.beta_api_shape_disabled` (§6.2.2), and a bad key fails fast at boot (§3.12.3) without ever reaching a live session.

⚠️ **The risk this accepts, stated so it is not rediscovered:** an error that leaves the socket open but the *session* unusable would now be logged and tolerated rather than degraded. That trade is right — the old behaviour was a degrade→reopen→same-failure loop — and two backstops already cover it: §6.9's think timeout degrades a session that produces no first token within `[gate] think_timeout_s`, and `[gate] session_idle_close_s` closes a quiet one. **Revisit if, and only if, a bench log shows an error frame followed by a socket that stays open and never produces another useful frame.**

**The frame, and what may be logged.** `error` carries `type`, `code`, `message`, `param` and `event_id`. The log line is an **allow-list of exactly those five**, never the frame — an error about `conversation.item.create` echoes the payload that caused it, and at M7 that payload is a tool output built from on-device memory (§7.10, the only memory OpenAI ever sees); a rejected `input_audio_buffer.append` would spill base64 PCM. `message`/`param` are length-bounded so one frame stays one line.

**`error.event_id` echoes the *client* event that caused the error**, which is why every client event we send is stamped `avid_<n>_<type>`. Without it the log can only say "something you sent was rejected"; with it, it names the call. That is the difference between diagnosing the next vendor rejection from a log and diagnosing it from a bench session.

**No fixture represents an error, by design.** `assets/sessions/` fixtures are timelines of *neutral* `RealtimeEvent` members (§14.3), and an error frame yields none — so there is nothing for a fixture to carry, and a fixture would exercise `ReplayRealtimeClient` (the fake) rather than the real client's translation half where this defect lived. The inbound path is covered instead by frame-level tests that drive `_events` with canned JSON.

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

**Implemented (#153).** "Stream live" is literal, and getting it wrong cost M5 its O1 objective once already. `AudioService` hands the drained pre-roll up the `TurnSink` at the rising edge and then **every frame as it is captured** — one `AudioChunk` per mic frame, matching what the `TurnSink` port always specified (`mic()` mirrors `Microphone.stream`) and what `FakeTurnSink` always did. Three points that are load-bearing:

⚠️ **AVID-194 superseded the next two bullets. Read this first.** They describe the two-authority arrangement — local VAD *and* server VAD both deciding turn boundaries — that shipped through M5. It is now off: `[ai.turn_detection] type = "none"` in both shipped profiles, and `ConversationService` commits explicitly via `RealtimeClient.end_user_turn()` at the local falling edge. The reason is that **a 500–900 ms pause is ordinary speech**, so the server routinely committed inside our hold window and answered a fragment of a sentence still being spoken. Given any required margin, the smaller window commits first *by construction*; there is no assignment of the two knobs that leaves one authority. The bullets are kept because the two-authority mode stays reachable by config and the measurements below are the evidence for why it is not the default — the `Config` margin assertion is now conditional on it. Full analysis: `docs/enhancement-single-turn-authority.md`.

- **The trailing silence is streamed too.** The server closes the turn on silence *it* hears, so withholding the hangover frames leaves the turn uncommitted forever. `AudioService` streams through its whole `[gate] silence_hold_ms` window and stops at the falling edge. *(Under AVID-194 the falling edge is also where the commit is sent, so the hangover frames still matter — they are the audio the model transcribes.)*
- **Therefore `[gate] silence_hold_ms` must exceed `[ai.turn_detection] silence_duration_ms` *by a margin*** (≥ 200 ms, asserted in `Config` at load; the shipped profiles carry 900/500, a 400 ms margin). A shorter local hold cuts the stream before the server has heard enough silence: the robot listens and then never answers, silently. **Equal is just as fatal, and for a subtler reason** — the local gate stops streaming *exactly* at its hold, so at parity the server receives its threshold at the instant the audio ends, and that is a race it usually loses. Measured 2026-08-01 on the bench: **500/500 → 2 transcripts and 2 replies in 13 turns; 900/900 → 1 and 1 in 8; 900/500 → 8 transcripts and 10 replies in 14.** Equal failing in *both* directions is what identifies a race rather than a value being too short. ⚠️ This paragraph previously read *"the shipped configs set both to 500 ms, so the two VADs close together"* — that claim was wrong and cost a bench session (AVID-176).
- **The margin does not delay the reply, and nothing should be written as though it does.** The server commits on its own clock, ~`silence_duration_ms` after the user's last speech frame, **while we are still streaming** — so the model starts generating at the same moment it always did. What moves later is only *our* falling edge: `audio.speech_ended`, `LISTENING → THINKING`, the thinking face and its §6.9 cue, and the O1 pairing. Mouth-to-ear is unchanged. Two consequences follow: first audio now **routinely precedes** `audio.speech_ended`, so **O1 is measured from the user's last speech frame** (`playback_started − (speech_ended − silence_hold_ms)`) — a definition invariant under the hold, comparable with every figure taken before this change, and one under which a non-positive sample is again physically impossible and rightly fails the harness. Second, `ConversationService` does not arm the §6.9 thinking cue when a delta for that turn has already arrived, or it would play *"one sec"* over a reply already coming out of the speaker.
- **The mic-up queue is bounded and drops oldest**, ~10 s deep, warning once per overflow episode. Nothing drains it between sessions or while a degraded robot's `open()` keeps failing, and per-frame emission into an unbounded queue leaks captured audio indefinitely (§3.5.2's rule, applied off the bus).
- **The uplink is half-duplex — nothing is streamed while the robot itself is talking** (AVID-159). "Stream live" was written without this qualifier, and the unqualified version is what shipped the defect: the mic hears the speaker, so the model was fed its own voice, the server's turn detection saw near-continuous audio, and it stopped committing turns altogether — the conversation died with the socket still open while frames kept arriving. `AudioService` shuts the seam from the first playback delta until `[gate] echo_tail_ms` after the reply ends. The tail is not padding: `end_response` means the model finished *sending*, not that the room went quiet, and the DAC is still clocking out up to a playback-buffer depth (§6.2.4). A barge-in gets no tail — `Speaker.stop` closes the handle so ALSA drops the buffer, and the user is mid-utterance. Barge-in itself survives the gate on **loudness**, see §6.2.4.

The M4 loopback (`loopback=True`, the #91 transport gate) is the one path that still buffers the whole clip — an echo needs it. `AudioService._capture` is the only place the two modes diverge.

**What it replaced, and why it was invisible.** Until #153 the service accumulated the utterance and flushed it at the falling edge, so the model's first byte arrived `silence_hold_ms` *after* the user stopped, as one blob. Three delays ran in series where this design has one — the local hold, the upload, then the server hunting the same silence inside the blob — and the model could not prefill while the user spoke. Measured on the Pi at the #106 bench run: **P50 1350 ms / P95 11278 ms against P50 800 / P95 1500**, with a floor of 1004 ms. It also made barge-in awkward to trigger by hand (the model cannot hear an interruption when it hears nothing until the turn is over) and forced `interrupt_response: False` for a second reason (§6.2.2): whole utterances arriving as one burst are indistinguishable from a barge-in, so the server cancelled every reply it started. No replay fixture could catch any of this — fixtures *record* the frames these bugs suppress.

**Cost of the gate:** ~200 ms of session-open latency on the first utterance of a conversation, paid once per conversation rather than per turn (the session stays open between turns). Against the §2.8.1 budget this pushes first-turn P50 to ~810 ms — marginally over the 800 ms target, in range on subsequent turns. §6.9 covers this with the thinking-cue mitigation.

⚠️ **The ~200 ms above is wrong, and the measured figure is ~1.08 s (AVID-157).** Measured on the Pi, 2026-08-16, `tools/probe_realtime_open.py`, warm medians:

| phase | ms | what it is |
|---|---|---|
| WebSocket **upgrade** | **849–881** | OpenAI's HTTP upgrade handshake |
| `session.created` | 1.5–2.2 | the server's own session bootstrap — **free** |
| `session.update` send | ~1 | a local socket write |
| `session.updated` ack | 199–219 | one round trip |
| **total** | **~1.08 s** | |

Full DNS+TCP+TLS to the host is **188 ms** on the Pi and 172 ms on the laptop, so **~660–700 ms of the upgrade is the vendor's handshake** — not our code, not the payload, not the hardware. Three candidates were ruled out by that table: the server's session bootstrap (1.5 ms), the payload size (**a full four-layer §6.4 prefix of 1326 characters costs ~20 ms**, which is why M6's layer 2 does not make this worse), and Pi-specific cost.

The number is **kept as a named gap rather than quietly widened to fit**: the only remaining lever is a pre-warmed or pooled connection, and that reopens ADR-007 — this gate exists precisely to avoid holding a socket while nobody is speaking. §6.9's thinking cue is what makes ~1 s tolerable in the meantime, and it is the mitigation R-01 always intended to be carrying this.

**Value of the gate:** see §6.10. It is the difference between $4/month and $108/month.

> **As measured on hardware (AVID-91).** The door-slam claim above is the one this section
> stakes itself on, and it holds on real audio rather than a synthesised burst: **0 false opens
> in 3000 frames** of deliberate transients — knocks, a clap, a door, a chair — and **0 in
> 15000 frames** of five minutes of loud broadband mic noise (a −21 dBFS constant floor, which
> a loudness threshold would have opened on continuously). Across 8.2 minutes of material
> containing no speech at all: **40/24750 frames = 0.16% false-open**. Speech detection, measured
> at the 500 ms utterance boundary `[gate] silence_hold_ms` itself defines: **49/53 = 92.5%**.
>
> Two cautions for anyone re-measuring. `audio_pi.py --mode vad` counts every frame outside a
> label span as silence, so pointing it at a set containing a *speech* take scores the pauses
> between words as silence the VAD should have ignored — on this set that inflates the report to
> 2.4% / 35.9%, of which 94% of the false opens are in the one speech take and ~70–90% of all
> disagreement lies within ±100 ms of a hand-drawn boundary. And label the speech-**energy**
> region, never the clip extent: doing the latter once produced a bogus 56% missed-speech that
> was pure labelling artifact. Separate takes of known provenance are what make labels
> trustworthy, because energy alone cannot tell a voice from a door slam — which is precisely
> the discrimination under test. Evidence: `docs/demos/m4_evidence/`.

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
| `remember_fact` | `(text, kind, importance, schedule?)` → `{ok, fact_id}` | **Durable before return** | §3.7.3. The model is told "remembered" only when it's true. `schedule` added at M10 — see below. |
| `recall` | `(query, k=5)` → `{facts: [...]}` | Async-safe | §7.7. The long-tail retrieval path. |
| `forget` | `(query)` → `{deleted: n}` | **Durable before return** | UC-07. Hard delete, not supersession. §7.10. |
| `set_affect` | `(affect)` → `{ok}` | Async, fire-and-forget | §6.8 |
| `set_quiet` | `(duration_s)` → `{ok, until}` | Async, fire-and-forget | §10.4's manual override, added at M10. Also reachable over HTTP as `POST /quiet` (§9.5) — **one piece of state, two doors.** |

Declaration is at session level in `session.update`, as JSON Schema. Static for the session (§6.2.2 — they're part of the cached prefix).

**`remember_fact`'s `schedule` argument (M10).** Optional, and meaningful only when `kind = "routine"`:

```jsonc
"schedule": {
  "rrule":      "FREQ=DAILY",      // RFC 5545 (§10.3). No invented DSL.
  "local_time": "08:00",           // HH:MM wall clock, in the zone below
  "timezone":   "Asia/Beirut"      // IANA; defaults to [behavior] timezone if omitted
}
```

It exists because §10 needs a *machine-readable* time to schedule and §8.3's `routines` table has
nowhere else to get one. The alternative — reading `facts.text` back and parsing "every day at 8 AM"
locally — re-derives structure the model already had in hand while producing the fact text, and a
heuristic that reads "8" as 20:00 delivers the coffee reminder at night. So the model supplies it on
the same call, and the `routines` row is written **in the same transaction as the `facts` row**: this
tool promises durability before it returns, and a fact whose schedule landed separately would make
that promise half true.

⚠️ A `kind="routine"` fact arriving with no `schedule` is **logged, loudly, with the fact and
correlation ids**. It is a legitimate outcome — not every routine has a clock time — but a model that
quietly stops filling the field is indistinguishable from a user with no routines, and that is the
exact shape of the `set_affect` failure §6.8 spent six live runs discovering.

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

**Step 5 waits for step 2's response to finish (AVID-284).** Step 2 arrives on `response.output_item.done`, which the API emits *before* the `response.done` that terminates the response requesting the tool — so sending `response.create` the moment we have the output races it, and loses often enough to matter (twice in 17 turns on the 2026-08-15 rig run):

```
error: invalid_request_error / conversation_already_has_active_response
       "Conversation already has an active response in progress: resp_… "
```

The adapter therefore waits on its own response-in-flight state — the same state §6.2.4 step 5's cancel guard reads — before sending, and sends anyway after a bounded wait rather than dropping the turn.

⚠️ **AVID-194 changed who else creates responses, and this paragraph used to say the opposite.** When AVID-284 was written, `turn_detection.create_response` was on and the server created every response except this one. AVID-194 switches server turn detection **off** (`[ai.turn_detection] type = "none"`), so the position is now reversed: `RealtimeClient.end_user_turn()` — the local VAD's falling edge — is the *only* thing that creates a response, and `send_tool_output` is its second caller. The guard is unchanged and needed by both; AVID-194's own write-up predicted exactly this (*"`response.create` becomes ours to not-send twice"*).

**A third caller since M10 (#313).** `RealtimeClient.begin_proactive_turn()` is §10.7 step 3 — a bare `response.create` with **no `input_audio_buffer` frame at all**, which is the one thing that distinguishes it from the other two: `end_user_turn` commits the buffer first, `send_tool_output` creates a conversation item first. It inherits the same in-flight guard rather than re-implementing it, which is why the method is three lines. The port names it for the application's need (ADR-003), and the only promise it makes about frames is that negative one: committing an empty buffer here would ask the model for a reply to silence, on a turn whose entire point is that nobody spoke.

⚠️ **Waiting, not cancelling, and the distinction is semantic, not stylistic.** The in-flight response here *is* the one that asked for the tool, so `response.cancel` would discard the call being answered. The opposite holds in §6.2.4: there the user is interrupting a reply they no longer want, and cancelling is right. One piece of state, two situations, two answers — and the reason AVID-284's acceptance criteria demanded the choice be made deliberately rather than raced.

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

**Implemented (#126):** `ConversationService` composes the top-facts block and hands it to `RealtimeClient.open(memory=...)` as an awaitable; the adapter `asyncio.gather`s it with the socket connect (the overlap above) and appends it as instruction **layer 4** in the one `session.update`, leaving layers 1–3 the byte-identical cached prefix (§6.2.2). The fetch is bounded (`[gate] memory_inject_timeout_s`); an empty or failed retrieval degrades to the stateless instruction — a robot that talks but does not remember, never one that does not talk — and every cold reconnect re-seeds it (§6.2.3).

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
| WSS drop mid-turn | **close** frame / timeout (an `error` frame is **not** a close — §6.2.5) | §3.7.6: DEGRADED, canned WAV, backoff reconnect, **re-seed session** (§6.2.3) |
| Realtime `error` frame | an `error` message on a socket that stays open | **WARNING carrying the vendor `event_id`; no transition, no cue, no teardown — the session continues** (§6.2.5) |
| 429 rate limit | HTTP status | Exponential backoff + jitter, 1s→30s cap |
| 401 | HTTP status | **Fail fast at boot.** Refuse to start. §3.12.3. |
| Slow first token (>10 s) | THINKING timeout | → DEGRADED |
| Tool handler raises | exception | Return `{ok: false, error}` to the model — *don't* crash the turn. The model will apologise gracefully, which is the correct behaviour. |

**The THINKING timeout (AVID-171).** The row above was specified from the start and **nothing drove it until AVID-171** — `Trigger.THINK_TIMEOUT` and its `(THINKING, THINK_TIMEOUT) → DEGRADED` row existed and were unreachable. The bench measured what that costs: a **60 ms** noise transient cleared the local VAD, opened a session the model never answered, and the robot sat in THINKING for **54 seconds**. The process was healthy throughout — mic frames arriving, VAD running, queue empty — it was simply waiting forever for a first token. Nothing else watches that silence: `[gate] session_idle_close_s` does fire, but an idle close tears the socket down and drives **no transition**, so the machine never moves.

`ConversationService` arms the deadline at the local VAD's **falling edge** — the same instant as the thinking cue above, because that is when the wait for a first token actually begins — and cancels it on the turn's first assistant delta, on a fresh rising edge (the user resumed: `THINKING + speech_started → LISTENING` leaves no row for the deadline), and on any teardown. Duration is `[gate] think_timeout_s`, default **10 s**, and it **must be less than `session_idle_close_s`** — asserted in `Config`, because the idle close cancels this timer without transitioning, so a larger value silently never fires and restores the wedge exactly.

Three properties are load-bearing and each is enforced by a test:

- **It tears the session down.** DEGRADED has a row for neither `audio.playback_*` trigger, justified in §3.10.3 by every path into DEGRADED killing the pump first. A timeout that degraded while leaving the pump alive would let a late delta drive `audio.playback_started` from DEGRADED, where there is no row — and it would strand recovery, since `_exit_degraded` only fires when a rising edge finds no open session.
- **It publishes `system.degraded_entered` (`cause="think_timeout"`) but *not* `conversation.session_lost`.** Nothing dropped; we gave up on a socket that is still open, exactly as the idle close does — and that one publishes nothing either. The distinction is not cosmetic: `conversation_pi.py` counts `session_lost` to grade #106's AC-6, so conflating them would make a pulled cable and a quiet model indistinguishable in the one artifact that grades recovery.
- **The cue is `SOMETHING_WRONG`** ("something went wrong on my end"), not `LOST_CONNECTION` — no connection was lost, and the degraded-mode phrases that promise a return are lies while there is no background reconnect loop (AVID-105).

⚠️ The timer re-checks that the machine is still in THINKING before degrading. This is not defensive padding: AVID-161's overlap arc (`THINKING + playback_finished → IDLE`, an *earlier* turn's reply draining while this one waits) leaves the deadline armed in IDLE with no cancel site reached, and only `(THINKING, THINK_TIMEOUT)` exists.

**The thinking cue.** ⚠️ **Implemented as a real timer only at AVID-170.** For M5 there was no 600 ms threshold: `ConversationService` scheduled the cue immediately at the falling edge and relied on cancellation to stop it, which is a race the cue reliably won because it starts pushing a WAV to ALSA in the same tick the user stops speaking. Net effect — it played on **every** turn, so a mitigation for occasional slowness became a permanent verbal tic and made fast turns *sound* slower than they were, the exact inverse of the argument below. The threshold is now `[cues] thinking_delay_ms` (default **600 ms**, injected, never a literal) and the cue is `Cue.THINKING_HMM` — the "hmm" this section always specified, not the `THINKING_ONE_SEC` ("one sec.") that shipped: "hmm" costs nothing if the reply lands right after it, while "one sec." *promises* a wait and is wrong precisely when the model turns out to be fast.

§2.8.1 concedes ~75% of latency isn't ours, and §6.3's gate adds ~200 ms to the first turn. R-01's contingency is that **perceived latency is designable even when actual latency isn't**: THINKING state renders instantly (Tier 1 affect, <20 ms), and if first audio hasn't arrived by 600 ms we play a short local "hmm" from the WAV bank. A robot that visibly and audibly thinks feels responsive at 1200 ms. A robot that sits silently feels broken at 800 ms.

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

**Shipped since 2026-08-01: `gpt-realtime-2025-08-28` (flagship).** Escalated from the mini for **latency, not quality** — a trigger this section did not anticipate, which is why it is recorded here rather than left to disagree with `config/pi.toml`.

Measured on the Pi with `tools/probe_first_token.py`, 6 trials each — model time-to-first-token, `response.created` → first `output_audio.delta`:

| model | min | median | max |
|---|---|---|---|
| `gpt-realtime-mini-2025-12-15` | 430 ms | 530 ms | 710 ms |
| **`gpt-realtime-2025-08-28`** | **214 ms** | **328 ms** | **424 ms** |
| `gpt-realtime-2.1` | 306 ms | 416 ms | 572 ms |
| `gpt-realtime-2.1-mini` | 292 ms | 464 ms | 2039 ms |

The flagship's *worst* trial beats the mini's median, and O1's P95 (§2.8.1) is graded on exactly that tail. It costs **3.2×** — projected ~$10/month against O7's $25, so the headroom absorbs it. The expectation that flagship would be *slower* was wrong, and had the O1 budget been amended without running this probe it would have been amended around an avoidable 200 ms (AVID-106).

**Revisit if O7 tightens**, or if a newer mini's tail improves — `gpt-realtime-2.1-mini`'s 2039 ms outlier, not its median, is what disqualifies it today. Reverting is a one-line config edit by design (the vendor boundary, CLAUDE.md §3).

*Superseded:* the original rule was **default `gpt-realtime-2.1-mini`**, ~3× cheaper, escalating to flagship **only** if M6's eval suite showed the mini failing personality adherence or tool-calling reliability. That quality trigger still stands; it is now one of two.

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

**Implementation (#119).** `LocalMiniLmEmbedder` (`avid/adapters/embedder.py`) runs the model via ONNX Runtime and follows `SileroVad` (§14.4): `onnxruntime` / `numpy` / `tokenizers` are lazy-imported inside the adapter (the Pi-only `pi` extra, ADR-008), so the module loads off-Pi where the fake path and CI live. It tokenizes with the model's WordPiece `tokenizer.json`, **mean-pools the token embeddings (`last_hidden_state`) with the attention mask applied**, and L2-normalises to the §8.2 unit vector — the pooling matches sentence-transformers, and the exact vector is pinned as a Pi-gated reference test (a mis-wired pooling/tokenizer is the classic silent quality bug). Because inference is *tens of ms* (not sub-ms like Silero), `embed` runs **off the event loop** via `asyncio.to_thread` (P8) and logs its own per-embed latency so the §7.4 number is recorded at the gate, not assumed. The ~86 MB model + tokenizer are **not committed**; `tools/fetch_minilm.py` downloads them to `/var/lib/robot/models/` (beside `silero_vad.onnx`) and verifies each against a pinned SHA-256, and a missing blob fails with an actionable error naming that script.

## 7.5 Episodic memory

Raw transcripts, `correlation_id`-keyed, 90-day retention. Not retrieved during conversation — it exists for debugging (§3.12.2), for reflection (§7.9), and for the M7 eval set. Pruned on a schedule because §2.7.1 says the SD card is the binding constraint.

**Implemented (#123):** `EpisodeRecorder` (`services/episode_recorder.py`) is a **write-only observer** — it subscribes to the four `conversation.*` facts that carry a turn's shape (`turn_started` / `user_transcribed` / `assistant_responded` / `turn_ended`) and mirrors each into the `episodes` table behind the `EpisodeStore` port, keyed by the turn's `correlation_id`, accumulating the transcript and maintaining `started_at` / `ended_at` / `turn_count`. It **publishes nothing and is read by no retrieval path** (a failure inside it is swallowed by the bus and can never affect a turn), and a barge-in `is_approximate` transcript is recorded **with its flag** (§6.2.4). It owns the 90-day prune: a scheduled loop on the injected `Clock`, deleting a **bounded** batch per pass so a large table never stalls the loop (P8). The store reuses the `[adapters] store` switch (facts and episodes are one SQLite file).

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

> **As built (#125).** The shipped `CAPABILITY_INSTRUCTIONS` (`avid/services/tools.py`) is this block **verbatim**, plus two trigger lines for the other two tools — *"when the user asks about something they told you before that is not already in your context, call recall"* and *"when the user asks you to forget something, call forget."* — seeded into the session's cached instruction prefix by `main` (§6.2.2). The `remember_fact.kind` enum in the tool schema is **derived from the domain `FACT_KINDS` tuple**, so it cannot drift from the §8.3 `CHECK` the database enforces — the model is structurally prevented from inventing a seventh kind. `ConversationService` declines a `remember_fact` on a barge-in-*approximate* turn (§6.2.4): a truncated transcript is not a reliable thing to persist.

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

> **As built (#120).** `HybridRetriever` (`avid/adapters/retrieval.py`) is exactly this: it embeds the query, does one matmul over the pre-normalised §8.5 matrix, unions those ids with `FactRepository.keyword_search`'s FTS5/bm25 hits, ranks the union with §7.7's `rank_candidates`, and publishes `memory.recall_completed` (§9.1.3). The retrieval eval set (#115) scored the wired retriever at **recall@5 = 0.54** — strong on proper-noun (0.80) and direct (0.80) queries, weak on paraphrase (0.10) and negatives (0.00). That split is honest: the CI-side `FakeEmbedder` is bag-of-words (real semantic recall is `LocalMiniLmEmbedder`'s job, #119, proven on the Pi), and the **relevance floor is deliberately deferred** — `n_returned` currently returns the top-k without a hard cosine cutoff, so a negative query still returns its best-but-irrelevant matches. Setting that floor is a tuning decision left for a measured pass, not guessed now.

### Vector storage — ADR-005, confirmed

**v1: embeddings as SQLite BLOBs + pre-normalised numpy brute-force cosine (single matmul).**

Not sqlite-vec. The research is clear on why:

- sqlite-vec is **pre-1.0 alpha** (stable v0.1.9, March 2026); breaking SQL/storage changes are expected before 1.0.
- Its search is **brute-force O(n) anyway** — the same thing numpy does, in someone else's alpha C extension. ANN indexes exist only in alpha pre-releases.
- **The ARM64 wheel has a documented history of shipping a 32-bit ARM binary** (v0.1.6, issue #211) which won't load on 64-bit Pi OS. v0.1.9 publishes a correct aarch64 wheel, but this is precisely the class of problem you don't want between you and M7.

Numpy brute-force over pre-normalised 384-dim vectors is a two-line function with no dependency risk. **Adopt sqlite-vec when it hits 1.0 and we have >50k facts**, i.e. probably never for a single-user robot.

**Scale thresholds — MEASURED on the Pi, 2026-08-02 (SPK-3 / #127, `tools/spk3_retrieval_scan.py`, 50 queries per size, real `LocalMiniLmEmbedder`):**

| Facts | scan p50 / P95 | of it, **inline on the loop** | FTS5 | Action |
|---|---|---|---|---|
| 1k | 9.9 / 27.8 ms | **0.6 / 4.1 ms** | 7.8 ms | numpy brute force — comfortable |
| 5k | 33.1 / 41.8 ms | **2.8 / 4.0 ms** | 28.8 ms | numpy brute force — comfortable |
| 10k | 62.3 / 71.8 ms | **5.7 / 5.8 ms** | 55.5 ms | over the 50 ms scan budget, **and it is FTS5, not the vectors** — see below |
| 50k–100k | extrapolated | — | — | int8 quantise, or sqlite-vec |
| > 100k | extrapolated | — | — | ANN (vectorlite/HNSW) |

**ADR-005 is confirmed, and by a wider margin than it claimed.** The numpy brute-force cosine — the part ADR-005 exists to defend — is the **inline** column: **0.6 ms at 1k, 5.7 ms at 10k**, roughly 2% of what a `recall` costs. The scan budget's breach at 10k is **SQLite FTS5** (55.5 of 62.3 ms), which is the keyword branch, not the vector branch. *int8 quantising the embeddings would optimise the component that is already free.* If retrieval ever needs work at scale, it is the FTS5 side that needs it.

**The scan budget and P8's slow-callback bar are different claims that share a number.** Only the matmul + top-k runs inline on the event loop; `FactRepository.keyword_search` goes through the repository's executor. So the 10k scan breach is **latency, not a loop stall** — P8's bar is met with an order of magnitude to spare at every measured size.

Index rebuild at boot (§8.5) is linear and cheap: **50 ms / 222 ms / 495 ms** at 1k / 5k / 10k.

Realistic ceiling for one user over years: **low thousands.** We are architecting for a problem we will not have — deliberately, because the cheap option is also the correct one at our scale, and §7.4's escape hatch covers the rest.

**Latency budget: <50 ms on the Pi.** Retrieval is off the turn path (§6.7) so this has slack, but if it exceeds ~150 ms the `recall` tool starts to be noticeable even with async function calling.

> ⚠️ **As measured, `recall` is already past the ~150 ms line at every size — and the index is not why.** Full recall is **225 / 245 / 277 ms p50** at 1k / 5k / 10k, of which **~190 ms is a single `LocalMiniLmEmbedder.embed` of the query** (§7.4, flat in N, 1.86–1.88 cores after #168's thread-pool cap). The embedding is ~85% of a recall at 1k and still ~70% at 10k. Mitigations therefore belong to §7.4 — a smaller or quantised sentence model, or caching query vectors — **not** to ADR-005's storage choice. §6.6's async function calling is what keeps this from being heard as dead air today; that is a real mitigation, not a reason the number is fine.

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

> **As built (#122).** `MemoryService.store_fact` (`avid/services/memory.py`) implements this pseudocode, durable-before-return then publishing `memory.fact_stored`/`memory.fact_superseded`. `k = 5`, and the cosine threshold is injected from `[memory] supersession_threshold` / `supersession_k` (P7, defaults in `avid/core/config.py`), not hard-coded. Step 3's judgement is the `TextModel.judge_supersession` port (§3.9.1): the real adapter is an OpenAI text client, the fake decides in-process, and it returns only the ids genuinely superseded — a subset of the candidates, `()` for none, never a guess (the gap-year rule below).

### The threshold, measured — and why 0.85 was wrong (#260)

The pseudocode's `0.85` shipped for a whole milestone and **fired on nothing**. Step 2 is a *hard* gate: below it, step 3 never runs, so the judge that exists to decide contradictions was never asked. Nothing errored; the store simply accumulated contradictions and answered with stale facts.

Measured on the Pi, real `LocalMiniLmEmbedder`, 42 pairs — `assets/eval/supersession.json` via `tools/eval_supersession.py` (Tier-5, §14.7):

| class | n | min | p50 | max |
|---|---|---|---|---|
| **contradictions** (must reach the judge) | 16 | 0.6217 | 0.7615 | 0.8915 |
| **paraphrases** (should reach it) | 10 | 0.7917 | 0.9519 | 0.9803 |
| **unrelated** (must not) | 16 | 0.2151 | 0.4152 | 0.5807 |

At **0.85 only 4 of 16 contradictions were admitted** — including *"Ali has switched to tea and no longer drinks coffee"* against a coffee fact, at 0.6328, the exact case §7.8 is written about. A close paraphrase of a fact scores 0.7917 against *itself* and was also rejected.

**The classes separate cleanly:** worst contradiction 0.6217, best unrelated 0.5807, so any bar in **(0.5807, 0.6217]** admits every contradiction and no unrelated pair. **`supersession_threshold = 0.60`** sits in that band, roughly centred, and admits 16/16 contradictions, 10/10 paraphrases, 0/16 unrelated.

The value is chosen *recall-first* on purpose. Step 2 is a cheap pre-filter and step 3 is the precise judge, so admitting a doubtful pair costs one off-turn-path TextModel call, while rejecting a real contradiction leaves a wrong memory in place indefinitely. **A gate tuned for precision rejects its own judge's input** — which is what 0.85 did.

⚠️ The band is narrow (0.041 wide) and the value is tied to *this* embedder. **Re-run the eval whenever `[memory] embedder` changes**; a threshold carried across models is a guess again.

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

### What `forget` may delete (#257)

`forget` ran with **no relevance bar at all** until the M7 gate: it deleted every id the top-k returned, so on a small store the query's whole neighbourhood went. One call destroyed **five of six facts** — hard cascading DELETE, nothing to undo it with, and the model reported success.

A fact is now deletable when its **raw cosine** clears `[memory] forget_relevance_floor` **or** FTS5 matched the query text directly (`deletable_ids`, `avid/domain/memory.py`; the evidence comes from `Retriever.match`, which publishes nothing because a forget is not a recall).

Both halves are load-bearing:

- **Without the floor**, the top-k sweep above.
- **Without the keyword branch**, forgetting *by name* breaks. A proper noun embeds to something generic — the reason §7.7 unions FTS5 into retrieval at all — so a floor-only rule would leave the row in place while telling the user it was forgotten. A privacy failure wearing a success message.

**The floor is 0.65, deliberately stricter than §7.8's 0.60**, because the error costs are reversed: a doubtful *supersession* costs one model call, a doubtful *deletion* destroys data permanently. 0.65 also clears the closest genuinely-related pair measured in the real gate store (0.6017, "building a robot" vs "learning AI") — two facts a user would be dismayed to lose while forgetting the other.

⚠️ The calibration set measures **fact-vs-fact** similarity, which is exactly what §7.8 does but only an approximation of `forget`, whose query is model-authored (at the gate the model passed a whole fact's text as the query, which is why the analogy holds as well as it does). Short, user-phrased queries are the weak case — and they are precisely what the FTS5 branch covers.

> **The cosine is the raw one, never §7.7's score.** `rank_candidates` min-max normalises relevance across the candidate set, so a lone candidate always scores 1.0 — a ranking, not a measurement. A caller deciding what to destroy must not read it as one.

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

> **As built (#117).** The DDL below is the **verbatim** shipped `avid/adapters/migrations/0001_initial.sql` — the spec block and the file are byte-identical, not a sketch that drifted. The file ships inside the wheel and is the single source the checksummed runner applies (§8.6); `FakeFactRepository`/`FakeEpisodeStore` run this same SQL at `":memory:"`, so the simulator's schema *is* the Pi's (P6). All of `facts`, `routines`, `triggers`, `proactive_log`, `episodes`, the `facts_fts` FTS5 shadow + its sync triggers, and `schema_migrations` exist as written. **Including the three §10 tables** — `routines`, `triggers` and `proactive_log` shipped with `0001`, months before anything read them, and M10 added no migration. Note this before planning one: the runner is checksummed and append-only (§8.6), so a second `CREATE TABLE routines` fails at boot and `0001` can never be edited to make room.

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

> **As built (#120/#122).** The write-through matrix lives in `HybridRetriever` behind the `Retriever` port (§3.9.1). `MemoryService.start` calls `retriever.rebuild()` at boot — the one reconciliation — which loads every live `(id, BLOB)` via `FactRepository.load_embeddings` and stacks them into the `N×384` float32 matrix; the numpy `import` and the `vstack` run **off the loop via `asyncio.to_thread`**, while the per-query matmul stays inline (sub-millisecond at the low thousands a single-user robot reaches, well under the 50 ms slow-callback gate, P8). `append`/`remove` are the synchronous in-memory upkeep on a durable write, and `forget` removes from SQLite then matrix, in that order.

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

> **As built (#117).** The runner is `migrate()` in `avid/adapters/sqlite.py` (~50 lines, no dependencies), invoked lazily on first store use. For each already-applied version it recomputes the file's **sha256 and compares it to the `checksum` recorded in `schema_migrations`** — a mismatch `raise`s at boot, naming the changed file, so the append-only rule is now **mechanically enforced, not merely a convention**. Unapplied versions run in ascending order, one transaction each; `now` (epoch seconds, §8.2) stamps `applied_at` and is injected (P7). A companion `check_fts5()` asserts the SQLite build has FTS5 up front, so §8.3's `facts_fts` virtual table can never fail cryptically mid-migration. Only `0001_initial.sql` exists so far.

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

**As built (M10): `avid/core/schedule.py`, and `python-dateutil` is now a runtime dependency.** Two
decisions worth recording, because both push against a stated posture.

*Where the library lives.* Runtime dependencies were `pydantic` alone; every other third-party
package in this tree sits in an optional extra behind exactly one adapter (`numpy`, `onnxruntime`,
`websockets`). `python-dateutil` is instead a **core** dependency, imported from `core/schedule.py`.
It is pure Python with no C extension, so it costs the Pi nothing, and — the real argument — hiding a
recurrence resolver behind a port would oblige P6 to provide a *fake* one, which is a second, worse
RRULE implementation grading itself against the first. That is precisely the DSL this section
forbids. The domain stays clean mechanically rather than by promise: `dateutil` is on
`import-linter`'s `domain-purity` forbidden list, beside `numpy` and `sqlite3`.

*How the DST fix actually works.* `dateutil.rrule` operates on **naive local** datetimes only; the
zone is attached afterwards:

```
after (UTC epoch) → local datetime in routines.timezone → strip tzinfo
                  → rrulestr(rrule, dtstart=<the routine's own start date at local_time>)
                  → first occurrence after the cursor
                  → re-attach ZoneInfo → UTC epoch → minus lead_time_s
```

⚠️ **DTSTART is the routine's start date, never "today".** It carries no column in §8.3 because it
does not need one — it is the `facts.created_at` of the fact the routine belongs to. Anchoring it at
the moment the scheduler happens to ask changes what the rule *means*, silently: `COUNT=2` re-counts
from today and never exhausts, and a bare `FREQ=WEEKLY` takes its weekday from DTSTART, so "weekly"
becomes "a week from whenever I last looked". Both still resolve, still return a plausible time, and
are simply not the schedule the user described.

### 10.3.1 A booking is a claim about a moment (as built, M10, #339)

`_fire_due` takes every heap entry whose deadline has passed, with the delay clamped at zero. That
is right for a running robot — a loop that wakes 200 ms late must still fire — and **wrong for one
that was switched off**, because "the deadline has passed" then means hours, not milliseconds.

Found on the rig: the Pi was powered down overnight, so 07:55's coffee reminder never fired. On the
next boot `BehaviorService._on_started` restored `next_fire_at` verbatim, the loop found it due, and
the gate — which grades the *room*, never the clock — waved it through. The robot would have
announced coffee at 10:43.

So a due trigger is now graded on its **age** before §10.4 is consulted at all:

```
now - fire_at > behavior.stale_grace_s   →   skip, log reason='stale', book the next occurrence
```

Three things about that line are deliberate.

**It is not a seventh rule.** §10.4's six are a normative, exhaustively-enumerated set that grade the
room; this grades the booking, and one belongs before the other rather than inside it. `STALE` is
therefore defined *outside* `POLICY_RULES`, and `SUPPRESSION_REASONS = POLICY_RULES | {STALE}` is what
the `proactive_log.reason` column now accepts.

**It is still logged.** §10.6's instrument must be able to tell a morning the robot slept through
from a scheduler that stopped working; from an empty table those are identical, which is the
failure mode §10.6 exists to prevent.

**The grace window is not zero.** Zero would mean "only ever exactly on time", which no scheduler can
promise: restart the service at 07:55:30 for a booking at 07:55:00 and the reminder is simply gone.
Overshooting this fix trades a loud bug for a quiet one — the robot says less and nothing reports it.
600 s is wide enough for an honest restart and far narrower than a night.

⚠️ **The age is measured against the heap entry, not `triggers.next_fire_at`.** The two are permitted
to diverge — a re-arm after a delivery schedules without persisting — so `SchedulerLoop` hands the
callback `(trigger_id, fire_at)`. The first implementation read the column and made a trigger staged
into the heap for *now* look fifteen hours old.

Let rrule work in UTC and you get exactly the "+86400 each day" bug above, wearing a library's
clothes. Two edges need a named policy or they become a 2 a.m. incident, and both fall out of `fold=0`
(the default — pinned by test, not trusted). A **nonexistent** local time (spring forward — 02:30
does not occur) resolves on the *pre-transition* offset: the instant the old timeline would have
reached, which lands at 03:30 on the new wall clock. **Late, never skipped** — a reminder that
silently evaporates once a year is the worse failure, because nothing logs and nothing errors. An
**ambiguous** one (fall back — 01:30 happens twice) fires once, on the earlier pass. A finite rule that has run out (`COUNT=`/`UNTIL=`)
returns no occurrence, the trigger's `next_fire_at` goes NULL, and `idx_triggers_due`'s partial
predicate drops it from the scheduler's only query for free.

⚠️ `tzdata` is a **dev**-group dependency, not a runtime one. Linux and the Pi carry a system tz
database; Windows does not, and without the wheel `zoneinfo.ZoneInfo("America/New_York")` raises
`ZoneInfoNotFoundError` — so the DST tests below pass in CI and fail on a Windows dev box, which is
the worst possible way for a test to behave.

The clock is injected (`Clock` port). M10's gate criterion — *"the coffee scenario, end to end, unprompted"* — is testable in 40 ms with a `FakeClock`, and separately once for real. Waiting until 07:55 to test the 07:55 code path is not a testing strategy.

### 10.3.2 The clock that books is not the clock that waits (as built, M10, #345)

A deadline is a **wall-clock** instant; the sleep that waits for it is **monotonic**. Converting one
to the other is a single subtraction, and it is only valid while the wall clock stays put.

Found on the rig, and it is not an exotic case — it is what the Pi does on every offline boot. There
is no RTC, so the machine comes up on a restored clock and NTP steps it forward once the network
arrives. On 2026-08-19 the robot booted believing it was the previous morning, computed a **9h38m**
delay for a booking that was really 35 minutes away, and parked. The correction moved the wall clock
33 hours and moved the outstanding sleep by nothing at all. The booking passed unattended.

The damage is worse than lateness. While the loop is parked on a deadline that no longer exists,
`_rearm` never runs either, so the *next* occurrence is never booked: the scheduler is **wedged**,
not behind. Only a restart recovered it, which is precisely what §5.2's *unattended* gate criterion
forbids.

So every sleep is bounded, not just the idle one:

```
delay = min(max(0, deadline - now()), max_sleep_s)
```

Three notes.

**The bound is injected, not chosen here.** `SchedulerLoop` has no opinion about tolerable lateness;
it receives a number. `BehaviorService` passes `min(stale_grace_s, IDLE_SLEEP_S)`, which is the one
relationship that makes the two fixes agree: a step can then leave a booking at most
`stale_grace_s` late, and §10.3.1 has already decided that much lateness is worth delivering rather
than skipping. Floored by `IDLE_SLEEP_S` so that a generously-configured grace cannot quietly
re-open the unbounded sleep.

**The idle branch keeps `IDLE_SLEEP_S`.** With nothing booked there is no deadline for a step to
invalidate, and `schedule()`'s wake event covers the moment that stops being true.

**A step is logged.** The cap makes the loop correct; the log makes the event diagnosable. §10.6's
argument applied to the scheduler itself — from a silent journal, a robot that slept through a
morning and a scheduler that died are the same picture, and the nine-hour nap left no trace
whatsoever.

⚠️ **The fake could not state this defect, which is why it shipped.** `FakeClock` derives `now()`
and `monotonic_ns()` from one counter *specifically so they cannot drift* — a good property that
also meant the only drift production actually produces had no expression in any test.
`FakeClock.step_wall_clock()` is that expression: it moves `_epoch0` alone, wakes no sleeper, and
allows a negative delta because wall clocks genuinely do step backward. The lesson generalises past
this bug: **the case a fixture is built to exclude is the case that reaches production.**

## 10.4 The interruption policy

Rules, in order. **First veto wins.** All state arrives in a `PolicyContext`; the function touches nothing else.

| # | Rule | Veto when | Rationale |
|---|---|---|---|
| 1 | **Quiet hours** | `now` within configured quiet window | Hard. Non-negotiable. Default 22:00–07:30. |
| 2 | **State** | state is not IDLE | Never interrupt a conversation in progress. §3.10.1. **See below** — this cell used to read `∉ {IDLE, SLEEPING}`. |
| 3 | **Presence** | no presence within last 5 min | **See below.** |
| 4 | **Ambient speech** | >60 s of VAD speech in last 5 min that opened no session | **See below.** |
| 5 | **Cooldown** | trigger fired < `cooldown_s` ago, *or* any proactive < 15 min ago | Global cooldown, not just per-trigger. |
| 6 | **Daily budget** | ≥5 proactive delivered today | Ceiling, not a target. G3 asks for ≥1. |

### Rule 2 — why IDLE alone, and not IDLE-or-SLEEPING

This cell read `state ∉ {IDLE, SLEEPING}` until M10 tried to implement it and found the SLEEPING half
unreachable. Three shipped facts, together:

- `(SLEEPING, VISION_PRESENCE_GAINED) → IDLE` is a row in §3.10.3. Presence is what wakes the robot.
- `IDLE → SLEEPING` happens only on `PRESENCE_LOST_TIMEOUT` — ten minutes *without* presence.
- Rule 3, below, vetoes unless presence is newer than `presence_window_s` (300 s).

So "asleep **and** seen in the last five minutes" cannot occur: by the time rule 3 can pass, rule 2's
SLEEPING case has already become IDLE. Admitting SLEEPING here would have obliged a
`(SLEEPING, behavior.trigger_fired)` row in the normative table that nothing could ever drive — and
§3.10.3 is a table where an unreachable row is not documentation but a claim that fails silently.
AVID-173 and AVID-189 were both shipped by reasoning about reachability one row too narrowly; this is
the same argument, made once, in the direction that removes a row rather than adding one.

The consequence is worth stating plainly: **proactivity does not wake a sleeping robot.** It speaks
to someone who is already there. Given rule 3 that was always true; now the two rules agree about it.

*Revisit if* rule 3 is ever degraded to a non-vision proxy (§7.3's M8-cut scenario, "was there a
conversation recently") — that proxy *can* be fresh while the machine sleeps, and this cell and the
state table would both need the SLEEPING case back, together.

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

#### The condition, made codeable (M10)

"Speech that opened no session" is not a state this architecture can observe. Per §6.3, **every**
Silero detection opens a session — that is what the gate is for — so there is no VAD-fired-but-quiet
case to count. The rule was written against a system that does not exist.

What *is* observable is the same idea one step later: speech that opened a session and never became a
conversation. So the settled condition is

> **cumulative `audio.speech_ended` duration within the last `presence_window_s` seconds whose turn
> never reached `conversation.turn_started`.**

`BehaviorService` already receives `audio.speech_ended{duration_ms}` in §9.1.3, and every one of them
carries the `correlation_id` minted at its own rising edge. So the accumulator is a pure fold:

```
record(window, correlation_id, at_s, duration_ms)   ← on audio.speech_ended
attribute(window, correlation_id)                   ← on conversation.user_transcribed
ambient_speech_s(window, now_s, window_s)           ← sum of the un-attributed, inside the window
```

**Count everything, then subtract what turned out to be a conversation.** The obvious alternative —
hold each utterance in a pending bucket for a grace period and promote it if no transcript arrives —
needs a timer, a new config knob, and a defensible value for it; and §3.10.3's own trace evidence
shows transcripts landing *after* the assistant's audio and sometimes after `conversation.turn_ended`,
so the grace would have to be seconds long and would be a guess. Retraction needs none of that.

It is transiently wrong for a second or two after a genuine user utterance, before the transcript
retracts it. That window is unreachable: rule 2 has already vetoed, because the machine is not IDLE
while a turn is in flight.

The distinction earns its keep in §10.6. Because attributed speech is excluded, a suppression
histogram separates *"the user was on a call"* from *"the user was talking to me"*. Counting all
speech would collapse both into `ambient_speech` and destroy the only diagnostic the table exists for.

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

⚠️ **`rule` and `reason` are the same value under two names, and both are normative.** The event field is `rule` (§9.1.3's `behavior.proactive_suppressed`); the column is `reason` (§8.3's `proactive_log`). Both shipped before either had a writer, and renaming a normative schema for cosmetics is not worth a migration. The values are pinned once — `POLICY_RULES` in `domain/behavior.py`, the same frozen vocabulary §10.4's gate returns — and mapped at the single write site, with a test that nothing outside that set ever reaches the column. If the two ever disagree, the query below silently under-counts, which is the one failure this table cannot afford.

⚠️ **`outcome` is written when the gate passes, which is before any words exist.** That gap is not theoretical: on the M10 rig a turn was recorded `delivered` and then `ignored` after crashing on its first audio chunk, and §10.5's backoff would have disabled the trigger within three mornings for being ignored — having never spoken. So `utterance` is filled when the robot actually speaks, **its absence is the signal**, and a delivered turn that produced nothing is never counted as ignored: the turn failed, the user did not decline.

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

⚠️ **The sink must be told whose turn this is, before any audio arrives (#337).** ``AudioService`` mints a ``correlation_id`` in exactly one place — its own VAD's rising edge — because until M10 every turn began with someone speaking. A proactive turn begins with a clock, so the sink receives assistant audio for a turn it never heard start, and asserts. Found live on the rig: the robot fired its reminder, opened a session, and said **nothing**, while `proactive_log` recorded `delivered`. The id therefore crosses the `TurnSink` port (`adopt_turn`) rather than the bus — §9.1.4 makes that seam a direct call because audio does not belong on an at-most-once bus, and a turn's identity travels with its audio.

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
| `audio.speech_started` | `ring_buffer_ms: int` | AudioService | StateManager, ConversationService, BehaviorService (M10) | DROP_OLDEST |
| `audio.speech_ended` | `duration_ms: int` | AudioService | StateManager, BehaviorService | DROP_OLDEST |
| `audio.playback_started` | `item_id: str` | AudioService | StateManager | DROP_OLDEST |
| `audio.playback_finished` | `item_id: str`, `played_ms: int`, `truncated: bool` | AudioService | StateManager, ConversationService | DROP_OLDEST |

`audio.speech_started` mints the `correlation_id` for a user-initiated turn. It is one of exactly two turn origins; `behavior.trigger_fired` is the other.

**Two M10 subscribers on this table need their reason stated, because neither is obvious from the event's name.** `BehaviorService` takes `audio.speech_started` as its *reply* signal (§10.5): a user answering a proactive turn speaks, and speaking mints a **fresh** `correlation_id` here — so the reply cannot be matched to the proactive turn by id, and does not need to be. The policy gate guarantees no second proactive is in flight and rule 2 guarantees the machine was IDLE when we fired, so any speech inside the hold-open window *is* the reply. And it takes `conversation.user_transcribed` as rule 4's **retraction** signal (§10.4): speech that became a transcript was speech directed at the robot, and is subtracted from the ambient accumulator.

`played_ms` on `audio.playback_finished` is §6.2.4's barge-in measurement — the milliseconds the speaker **accepted from us**, summed from `Speaker.play()`'s return, not the length of the buffer we submitted. `played_ms == 0` therefore means the device took nothing, and a gate may treat it as a hard failure (AVID-91).

#### `conversation`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `conversation.turn_started` | `initiator: "user" \| "proactive"` | ConversationService | EpisodeRecorder, Observability | DROP_NEWEST |
| `conversation.user_transcribed` | `text: str`, `is_approximate: bool` | ConversationService | EpisodeRecorder, BehaviorService (M10) | DROP_NEWEST |
| `conversation.assistant_responded` | `text: str`, `item_id: str` | ConversationService | EpisodeRecorder, Observability, BehaviorService (M10) | DROP_NEWEST |
| `conversation.turn_ended` | `duration_ms: int`, `usage: TokenUsage` | ConversationService | EpisodeRecorder, Observability (cost meter, §6.10.6) | DROP_NEWEST |
| `conversation.session_lost` | `cause: str`, `was_mid_turn: bool` | ConversationService | StateManager, ExpressionService | DROP_NEWEST |

`is_approximate` is not a hedge — it's §6.2.4's truncation consequence made explicit. `conversation.item.truncate` drops the transcript for unplayed audio, and audio/transcript alignment is imprecise, so a barge-in leaves the tail of the transcript unreliable. Anything downstream that treats this text as ground truth (fact extraction, episode recording) must consult the flag.

`conversation.turn_ended` carries the `usage` payload from `response.done`. This is the sole feed for §6.10.6's cost meter — the smoke detector for the $12-vs-$85 failure.

#### `affect` / `state`

| Event | Payload | Published by | Subscribers | Queue |
|---|---|---|---|---|
| `affect.changed` | `affect: Affect`, `tier: 1 \| 2`, `previous: Affect` | AffectService | **ExpressionService** ✅, MotionService (M9) | **DROP_OLDEST** |
| `state.transitioned` | `from_: RobotState`, `to: RobotState`, `trigger: Trigger` | StateManager | **ExpressionService** ✅, **AffectService** ✅, BehaviorService (M10), Observability (M10) | DROP_OLDEST |

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
| `memory.recall_completed` | `query: str`, `n_returned: int`, `latency_ms: float` | HybridRetriever | Observability | DROP_NEWEST |

**These are published *after* the write is durable, never before.** §3.7.3's diagram: commit, then publish. They are notifications that something already happened — which is the only reason it's safe to put them on an at-most-once bus (§9.1.4).

`memory.fact_stored` → `BehaviorService` is how UC-02 becomes UC-03 with zero coupling. `MemoryService` does not know the behaviour engine exists.

> **As built (#116/#120/#122).** All four names + payloads ship as written (`avid/domain/memory.py`, `MemoryFactStored`/`MemoryFactSuperseded`/`MemoryFactDeleted`/`MemoryRecallCompleted`). One publisher correction: the three `fact_*` events come from `MemoryService` (the write path), but **`memory.recall_completed` is published by the `HybridRetriever`** (the read path, `avid/adapters/retrieval.py`) — `retrieve()` emits it itself with a monotonic-derived `latency_ms` (§9.1.1, never wall-clock), so `MemoryService.retrieve` must not re-publish. `n_returned` is the count actually returned (0 for a clean miss); the deferred relevance floor (§7.7) means a negative query can still return its best-but-irrelevant top-k.

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

**Owned tasks are spawned through `core/tasks.py::spawn`, never `asyncio.create_task` directly** (AVID-174). A service's background loops — the mic loop, the Realtime pump and mic forwarder, the idle and first-token timers — are `Task` objects nobody awaits, so an exception inside one used to be discarded entirely: the loop simply stopped and CPython printed `Task exception was never retrieved` at interpreter shutdown, long after the run that mattered. That is how #174's `AssertionError` turned a one-line inconsistency into a robot that was permanently deaf to the model with its socket still open and nothing in the log. `spawn` attaches a done-callback that logs the task name and traceback at ERROR, and treats `CancelledError` as what it is — every teardown path here cancels its owned tasks, so reporting that would make a clean shutdown indistinguishable from a crash. The bus's subscriber workers are deliberately excluded: the bus already owns its failure semantics (§9.1.6), and routing them through `spawn` would double-report.

⚠️ **This is visibility, not supervision.** Nothing yet *reacts* to a dead owned task — deciding whether a dead pump should degrade, restart or tear the session down is a policy question with §3.10.3 consequences and wants its own issue. `spawn`'s `on_death` hook is the seam it will hang off. Note also that a service must **not** publish `system.handler_failed` for this: that fact is the bus's statement about a failing *subscriber* (§9.1.6), and a bus fact for a dead owned task would need its own event and catalog row.

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
    async def embed(self, text: str) -> Sequence[float]:   # NOT NDArray — see below
        """Returns a PRE-NORMALISED, unit-length vector. §8.2."""
    @property
    def dimensions(self) -> int: ...

class VoiceActivityDetector(Protocol):   # §6.3
    def is_speech(self, frame: AudioChunk) -> bool:
        """MUST return in <5ms — called on every 30ms frame."""
```

> **As built (#118/#119):** `Embedder.embed` returns a plain **`Sequence[float]`**, not `NDArray[np.float32]`. The vector crosses the port numpy-free on purpose — `core` and `domain` never import numpy (P1, ADR-012 keeps runtime deps pydantic-alone), so `FakeEmbedder` needs no third-party dependency and the domain-purity test stays green. Packing the floats into the §8.2 384×f32 LE BLOB is the repository's job (`FactRepository.add`), and stacking them into the search matrix is the index adapter's (§8.5); numpy is the adapters' private business, downstream of this port. `LocalMiniLmEmbedder` (real, ONNX) and `FakeEmbedder` (the P6 simulator) both satisfy it.

The **memory-storage ports** M7 added — `FactRepository`, `Retriever`, `TextModel`, `MemoryTools`, `EpisodeStore` — are defined normatively in **§3.9.1** (like the HAL ports), not restated here.

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
silence_hold_ms    = 900        # silence run before a turn is declared over (AudioService debounce).
                                # MUST clear [ai.turn_detection] silence_duration_ms by >= 200 ms,
                                # asserted in Config: the mic stream stops exactly here, so at
                                # parity the server VAD is still counting when the audio ends and
                                # the turn never commits. Measured 900/500 -> 10 replies in 14
                                # turns, vs 2 in 13 at 500/500 (AVID-176).
session_idle_close_s = 30
barge_in_margin_db = 3.0        # dB over the echo floor a rising edge must clear to be the USER
                                # (UNCALIBRATED - AVID-296: tuned with capture AGC in an
                                #  unknown state, and AGC moves the floor ~20 dB)
highpass_hz        = 150.0      # AVID-283: the LEVEL measurement is high-passed, not the VAD
highpass_order     = 3          # 30 dB at 50 Hz for 1 dB at 1 kHz; one pole buys only ~10 dB
                                # while the robot speaks (§6.2.4). PROVISIONAL — the M5 gate's
                                # AC-3 records the measured value. Very large = full half-duplex.
echo_tail_ms       = 150        # uplink stays shut this long after a reply ends (the DAC drain)
think_timeout_s    = 10.0       # §6.9: no first audio delta this long after the falling edge -> DEGRADED.
                                # MUST be < session_idle_close_s (asserted in Config): the idle close
                                # cancels this timer and drives no transition, so a larger value never
                                # fires and the robot wedges in THINKING (AVID-171).

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
hold_open_s      = 30           # §10.7 step 5 / §10.5's "wait 30 s" — one number, two readers
ignore_backoff_multiplier = 2   # §10.5. cooldown_s *= this, per ignore
stale_grace_s    = 600          # §10.3. How late a booking may be and still be worth saying

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

Its companion, `test_every_trigger_drives_at_least_one_row`, holds the other invariant: `Trigger` **is** the §3.10.3 Event column, so a member with no row is not documentation, it is seven guaranteed-illegal pairs padding the test above and a value `StateTransitioned.trigger` can never legally carry. Rows without a *driver* are fine and expected (the three `timer.*` expiries are unwired; `behavior.trigger_fired` gained its driver at M10) — membership tracks the table, not the call sites. That is why AVID-158 deleted `CONVERSATION_USER_TRANSCRIBED` outright rather than leaving it row-less.

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
    {"delay_ms": 250, "type": "tool_call_requested",   "call_id": "call_0", "name": "recall", "arguments": "{\"query\": \"...\"}"},
    {"delay_ms": 300, "type": "assistant_transcript",  "text": "...", "item_id": "item_0"},
    {"delay_ms": 20,  "type": "assistant_audio_chunk", "item_id": "item_0", "wav": "turn0_a.wav"},
    {"delay_ms": 200, "type": "turn_done",             "usage": {"input_tokens": 320, "cached_input_tokens": 256, "output_tokens": 48}},
    {"delay_ms": 40,  "type": "session_closed",        "cause": "network"}
  ]
}
```

`type` selects a neutral `RealtimeEvent` member (`core/realtime.py`); its remaining keys are that member's fields. `delay_ms` is the gap *before* the event, replayed on the injected `Clock` (never wall time), so a `FakeClock` steps the whole timeline instantly and deterministically. `assistant_audio_chunk.wav` is a filename resolved beside the manifest and loaded as one `AudioChunk` (24 kHz mono S16_LE, the §6.2.4 playback format). Four fixtures ship — a normal two-turn conversation, a barge-in (approximate user transcript mid-reply plus a post-truncation delta), a mid-turn session loss, and a tool call (a `recall` invocation interleaved in a turn, #124). The openai adapter's `--capture` mode (AVID-105) writes this exact format from a live session, so the fixtures cannot drift from real API behaviour.

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

**As built (M10): `tests/e2e/test_m10_gate.py`.** The `Sim` sketch above is the shape, not the API — this repo realised the scenario tier as one `tests/e2e/test_mN_gate.py` per milestone, wiring the real `AsyncioEventBus`, the real services and fakes only. The steps are the same line for line, including the process restart against the same SQLite file, and it carries the M4/M5 addition the sketch predates: **the harness's own pass/fail logic is under test**, one neutered guard per criterion, because a gate that can pass on silence is not a gate.

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
