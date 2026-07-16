# Contributing — Avid / Pico

Workflow rules for the project. Derived from `PMP.md`; see `CLAUDE.md` for the
engineering principles and `SECURITY.md` for security rules.

---

## Issue hierarchy (PMP §12.3)

```
Epic   (label: epic — one per WBS 2nd level, e.g. "1.4 The event bus")
  └─ Issue   (a Ready-able unit of work, ≤ 2 IED)
       └─ Task   (a checkbox inside the issue — NOT a separate issue)
```

Three levels. Not four.

---

## Sizing — Ideal Engineering Days (PMP §2.6, §12.4)

Estimates are in **IED**: one uninterrupted 8-hour day. 1 IED ≈ 2.5–3 calendar
days in practice.

| Size | IED |
|---|---|
| XS | 0.25 |
| S | 0.5 |
| M | 1 |
| L | 2 |
| XL | 3 |

Anything sized **XL must be split** before it can be `Ready`. If it can't be
split, it isn't understood — make it a `spike`.

---

## Definition of Ready (PMP §10.1)

An issue may not enter `In Progress` unless:

- [ ] It states its outcome in terms of **observable behavior**, not implementation
- [ ] Acceptance criteria are written and are **binary**
- [ ] It names the **SDS section** it implements (or opens a PR against the SDS first)
- [ ] Dependencies are linked and closed
- [ ] It's sized (§12.4)
- [ ] If it touches a port, the **contract-test change is identified**

---

## Definition of Done (PMP §10.2)

Universal. No exceptions, no "I'll do it next PR."

- [ ] Code merged to `main` via PR
- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy --strict` clean on touched modules
- [ ] `import-linter` contracts pass (P1/P5 enforcement)
- [ ] Unit tests written; new non-adapter code **≥ 90% covered**
- [ ] If a port changed: contract tests updated and passing against **both** real and fake adapters
- [ ] Tests pass with `PYTHONASYNCIODEBUG=1`, no slow-callback warnings (P8)
- [ ] CI green on **3.11 and 3.13**
- [ ] SDS updated if any interface, event, or schema changed
- [ ] If it's a hardware feature: verified on the actual Pi, not just simulated
- [ ] No new `# type: ignore` or `# noqa` without an inline reason
- [ ] Observability: new failure paths log with a correlation ID

**A milestone is "done done" only when its gate demo is recorded and committed to
`docs/demos/`** — not when the code merges (PMP §5.1).

---

## Work-in-progress limits (PMP §12.2)

| Column | WIP limit |
|---|---|
| Ready (passes DoR) | 8 |
| **In Progress** | **2** |
| In Review (PR open, CI running) | 3 |

**The WIP limit of 2 is the single most important number on the board.** Solo
developers don't fail from working slowly; they fail from six things being 70%
done.

---

## Commits & branches (PMP §11.2, §11.3)

- **Conventional Commits**, enforced by `commitlint` in CI.
- Types: `feat fix refactor perf test docs build ci chore spike adr`
- Scopes: `core ai audio vision display motion memory behavior hal infra docs`
- Branch names: `<type>/<issue>-<slug>` (e.g. `feat/9-event-bus`). Max ~3 days alive.
- **Pull requests: ≤ 400 lines changed.** All CI gates green — **no merge on red.**
  Squash-merge.

---

## CI quality gates (PMP §10.4 / SDS §14.9) — all blocking

| Gate | Tool |
|---|---|
| Format | `ruff format --check` |
| Lint | `ruff check` (incl. `ASYNC`, `DTZ` rules) |
| Types | `mypy --strict` |
| Architecture | `import-linter` |
| Composition-root purity | grep for `Real*(`/`Fake*(` outside `main.py`/fixtures (P3) |
| Config purity | grep for `os.environ` outside `core/config.py` (P7) |
| Unit tests | `pytest` on **3.11** and **3.13** |
| Contract tests | `pytest -m contract` |
| Async debug | `PYTHONASYNCIODEBUG=1 pytest` (slow-callback > 50 ms fails, P8) |
| Coverage | `pytest --cov`, **≥ 90% non-adapter** |
| Event-catalog drift | generated subscriber graph vs. SDS §9.1 |

Nightly, non-blocking: AI eval suite (`pytest -m eval`, needs key), HIL suite
(self-hosted Pi runner).

The architecture gate is the load-bearing one: reviewers get tired, `import-linter`
doesn't.

---

## Technical-debt policy (PMP §10.6)

Debt is **written down at the moment it is taken**, as an issue labelled `debt`,
linked from a `# TODO(#123)` comment. An untracked shortcut is not debt, it's a
defect. Budget: one Friday per milestone for debt paydown.

---

## Labels

Component `comp:*`, size `size:*`, priority `prio:*`, type (`epic`/`spike`/`debt`/
`adr`), `no-hardware`/`hardware-required`, confidence `conf:*`, risk `risk:R-NN`.
Milestones (`M0`–`M11`) are native GitHub milestones, mirrored on the project board.
