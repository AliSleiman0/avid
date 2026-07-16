# Avid — Pico

AI Desktop Companion Robot (codename *Pico*). A single asyncio process on a
Raspberry Pi, built as a hexagonal (ports & adapters) system around an in-process
event bus, with every device behind a `Protocol` and a shipping fake.

Currently at **M0 — Walking Skeleton** (infrastructure only, no hardware). See the
[M0 milestone](../../milestone/1) (AVID-1 … AVID-15).

## Governance

- **[`CLAUDE.md`](CLAUDE.md)** — codebase rules: the 8 principles (P1–P8), layering, mandated design patterns, the event bus, state/affect.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — Definition of Ready/Done, WIP limits, sizing, commits, CI gates.
- **[`SECURITY.md`](SECURITY.md)** — secrets, network exposure, data privacy, supply chain.

## Design documents

- **`SDS.md`** — System Design Specification (source of truth for architecture).
- **`PMP.md`** — Project Management Plan (milestones, risk register, process).
- **`M0.md`** — the Walking Skeleton milestone breakdown.

## Layout

```
avid/
  domain/    pure logic — no I/O, no async, no third-party deps but pydantic
  core/      EventBus, StateManager, Config, ports.py (the Protocols)
  services/  async use-case orchestration
  adapters/  Real*/Fake* implementations (fakes ship here, and ARE the simulator)
  main.py    composition root — the only place adapters are built
tests/       unit + contract tests (no hardware, no network, no API key)
```

## Development

Requires [uv](https://docs.astral.sh/uv/). The dev interpreter is pinned to
Python 3.13 (`.python-version`); uv downloads it automatically.

```sh
uv sync --dev        # create the venv and install everything
uv run avid --help   # run the CLI
uv run pytest        # run the test suite
```

Run the robot (a real composition root lands in AVID-14):
`uv run avid --config config/sim.toml`.
