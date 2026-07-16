# Security & Privacy — Avid / Pico

This file consolidates the security rules that are currently scattered across the
SDS (§3.12, §7.10, §8.4, §9.5, §9.6). The SDS's dedicated **§13 "Security and
Privacy"** — §13.1 threat model, §13.2 secrets management, §13.5 camera & mic
privacy controls — is referenced throughout the design but **not yet written**;
authoring it is tracked as a `debt` issue. Until then, the rules below are
authoritative and `SDS.md` is the source of truth for each.

Pico is a home-LAN desktop device we fully control. The threat model is a trusted
device on a trusted network — but "trusted" is not "careless."

---

## 1. Secrets (SDS §9.6)

- **`OPENAI_API_KEY` is read from the environment exactly once, in `main.py`, and
  injected as a value.** It never appears in a config file, a log line, or a repr.
- It is a pydantic **`SecretStr`**, so an accidental `print(config)` prints
  `**********`.
- Secrets are **not** in the TOML config. Config carries settings, never keys.
- **P7 — configuration is injected, never read:** no module calls `os.environ` or
  `os.getenv` outside `core/config.py`. CI greps for violations and fails the build.

## 2. Network exposure (SDS §9.5)

- The local control API binds **`127.0.0.1:8787`**. There is no auth, no TLS, no
  accounts. **Localhost binding *is* the authentication.**
- Binding to `0.0.0.0` would be a security bug. The bind address is **asserted at
  startup** — the process refuses to start if it isn't loopback.
- Transport to OpenAI uses a **server-side API key directly** over WSS (ADR-010);
  no ephemeral client-secret exchange, because the Pi is the trusted server.

## 3. Data at rest & privacy (SDS §7.10, §8.4, ADR-011)

- Memory is **SQLite on-device**. It is **never synced** and never leaves the
  device.
- Embeddings are computed **locally** (ADR-011) — fact text never goes to a third
  party for indexing. What OpenAI sees is live audio and the top-k injected facts
  in the instructions. **Nothing else.**
- **"Forget that" is a hard, cascading `DELETE`** (UC-07), not a supersession. It
  cascades to embeddings and episodes. Supersession is an epistemics feature (what
  is true now); deletion is a **rights** feature (what may be retained) — the user
  is withdrawing consent and the row goes away.
- `PRAGMA foreign_keys = ON` **per connection**. `forget()` silently orphaning
  embeddings or triggers is a **privacy bug**, not a tidiness bug.
- `MemoryService.forget()` is a direct awaited call, **durable before it returns**.
  Losing a deletion is a privacy bug. `GET /facts` exposes all non-superseded
  facts — the "what do you know about me?" audit.

## 4. Availability & fail-fast (SDS §3.12.3)

- A **bad API key (401) fails fast at boot** — the robot refuses to start with a
  loud log. **Nothing except a bad key at boot is allowed to stop the robot**;
  every other external failure degrades to `DEGRADED`, it does not crash.

## 5. Logging & PII (SDS §3.12.2, §7.5)

- Structured JSON logs via `structlog` to **journald with `Storage=volatile`** —
  **not to the SD card** (also protects card lifetime, risk R-05).
- Secrets are never logged. Every failure path logs with its **correlation ID**.
- Episodic raw transcripts have a **90-day retention** (SDS §7.5).

## 6. Dependencies & supply chain (ADR-008, SDS §3.11.2, §7.4)

- The AI model is **pinned**: `gpt-realtime-2.1-mini-2026-07-06`. No floating tags.
- `picamera2` is an **optional dependency group**, imported only inside its one
  `Real*Camera` adapter — it never touches the laptop dev/CI environment.
- The Pi environment is pinned via a **Pi-specific lock**; `uv.lock` is committed.
- Known hazards to watch: the `picamera2` ARM64 wheel has shipped a 32-bit binary
  historically (fixed in a later release); `sqlite-vec` is deferred until it hits
  1.0 and we exceed ~50k facts. Prefer the dependency-light path.

## 7. Input validation

- `Servo.move_to` **clamps to configured safe limits internally** — safety
  invariants live at the lowest layer that can enforce them universally
  (SDS §3.9.1; contract test `test_clamps_beyond_limits`).
- `conversation.user_transcribed` carries `is_approximate: bool`; downstream
  fact-extraction and episode-recording **must consult the flag** — a barge-in
  truncates the transcript tail and makes it unreliable (SDS §9.1.3).

---

## Reporting a vulnerability

This is a private, single-maintainer project. Report suspected security issues by
opening a **private** security advisory on the GitHub repository, or contact the
maintainer directly. Do not open a public issue for an unpatched vulnerability.

## Status of SDS §13

`SDS.md` §13 (threat model / secrets management / camera & mic privacy controls)
is a documented gap. It is tracked as a `debt` issue and should be authored before
M11 ("It's a product"). Until it lands, this file governs.
