# Avid — Pico

AI Desktop Companion Robot (codename *Pico*). A single asyncio process on a
Raspberry Pi, built as a hexagonal (ports & adapters) system around an in-process
event bus, with every device behind a `Protocol` and a shipping fake.

Currently at **M0 — Walking Skeleton** (infrastructure only, no hardware). See the
[M0 issues](../../issues?q=is%3Aissue+label%3AM0) (AVID-1 … AVID-15).

## Governance

- **[`CLAUDE.md`](CLAUDE.md)** — codebase rules: the 8 principles (P1–P8), layering, mandated design patterns, the event bus, state/affect.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — Definition of Ready/Done, WIP limits, sizing, commits, CI gates.
- **[`SECURITY.md`](SECURITY.md)** — secrets, network exposure, data privacy, supply chain.

## Design documents

- **`SDS.md`** — System Design Specification (source of truth for architecture).
- **`PMP.md`** — Project Management Plan (milestones, risk register, process).
- **`M0.md`** — the Walking Skeleton milestone breakdown.

## Layout (target)

```
domain/    pure logic — no I/O, no async, no third-party deps but pydantic
core/      EventBus, StateManager, Config, ports.py (the Protocols)
services/  async use-case orchestration
adapters/  Real*/Fake* implementations (fakes ship here, and ARE the simulator)
main.py    composition root — the only place adapters are built
```

Run (once M0 lands): `uv run avid --config config/sim.toml`.
