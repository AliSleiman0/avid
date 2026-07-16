<!-- PR title: Conventional Commits — <type>(<scope>): <summary>  (≤400 lines changed) -->

## What & why

<!-- One paragraph. Link the issue: Closes #NN -->

Closes #

## SDS

<!-- Section(s) implemented. If an interface/event/schema changed, the SDS PR is linked. -->

## Definition of Done (PMP §10.2)

- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy --strict` clean on touched modules
- [ ] `import-linter` contracts pass (P1/P5)
- [ ] Unit tests written; new non-adapter code **≥ 90% covered**
- [ ] If a port changed: contract tests updated, passing against **both** real and fake adapters
- [ ] Tests pass with `PYTHONASYNCIODEBUG=1`, no slow-callback warnings (P8)
- [ ] CI green on **3.11 and 3.13**
- [ ] SDS updated if any interface, event, or schema changed
- [ ] Hardware feature: verified on the actual Pi (not just simulated) — or N/A
- [ ] No new `# type: ignore` / `# noqa` without an inline reason
- [ ] New failure paths log with a correlation ID
