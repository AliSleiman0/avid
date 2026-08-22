# Security & Privacy — Avid / Pico

**`SDS.md` §13 is the authority.** This file is the operator-facing summary: the rules in one
place, each pointing at the subsection that reasons about it. Where the two disagree, §13 wins and
this file is the bug — the same rule `CLAUDE.md` states for itself.

> ⚠️ It was the other way round until AVID-21. While §13 was an unwritten table-of-contents entry,
> this file was authoritative and drifted: it named a model the project stopped using on
> 2026-08-01, credited `structlog` — which is imported nowhere and which the SDS had already
> corrected in AVID-378 — and described `GET /facts` as the privacy audit when that route is not
> served. All three are fixed below, and `tests/docs/test_security_claims.py` now checks the
> falsifiable literals in both documents against the code (§13.7). **A security document that is
> wrong is worse than one that is missing, because it is trusted.**

Pico is a home-LAN desktop device we fully control. The threat model is a trusted device on a
trusted network — but "trusted" is not "careless", and §13.1 writes down the non-goals that
scoping buys.

---

## 1. Secrets — §13.2

- **`OPENAI_API_KEY` is read from the environment exactly once**, in `load_config`
  (`avid/core/config.py`) — the only function in the tree that may touch `os.environ` — and
  injected as a value. Never in a config file, a log line or a repr.
- It is a pydantic **`SecretStr`**, so an accidental `print(config)` prints `**********`. It is
  unwrapped in exactly **two places, both in `main.py`**, for the two adapters that need it.
- On the Pi it lives in `EnvironmentFile=-/etc/robot/robot.env`, **root-owned `0600`** — referenced
  by the unit, never inside it.
- The startup banner reports the key's **presence, never its value** (AVID-373).
- **P7 — configuration is injected, never read.** CI greps `avid/` for `os.environ`/`os.getenv`
  outside `core/config.py` and fails the build.

## 2. Network exposure — §13.4

- The local control API binds **`127.0.0.1:8787`**. No auth, no TLS, no accounts.
  **Localhost binding *is* the authentication.**
- `0.0.0.0` would be a security bug. `ApiConfig` rejects a non-loopback bind **at config load**, so
  the process refuses to start; the server asserts it again at construction.
- ⚠️ **As built the API serves `/health`, `/metrics`, `/state`, `/events/stream` and
  `POST /quiet`** (#385).
- ⚠️ `/facts` appears in SDS §9.5 but is **not implemented** (#386), so §7.10's audit is currently
  a spoken query rather than an endpoint.
- `/events/stream` is a **read-only tap** on the internal event bus (#385): event names, sources
  and correlation ids, never payloads. Like every other route it is loopback-only, and that is
  the whole of its access control — a live feed of what the robot is doing deserves the same
  treatment as the rest.
- Transport to OpenAI is **WSS with the server-side API key used directly** (ADR-010); no ephemeral
  client-secret exchange, because the Pi is the trusted server.

## 3. Data at rest & privacy — §13.3

- Memory is **SQLite on-device**. **Never synced**, never uploaded, no account, no cloud copy.
- Embeddings are computed **locally** (ADR-011) — fact text never goes to a third party for
  indexing. What OpenAI sees is live audio and the top-k injected facts. **Nothing else** (§13.4).
- **"Forget that" is a hard, cascading `DELETE`** (UC-07), not a supersession — the row, its FTS5
  entry, its embedding. Supersession is an epistemics feature (what is true now); deletion is a
  **rights** feature (what may be retained).
- `PRAGMA foreign_keys = ON` **per connection**. A `forget` that orphans an embedding is a
  **privacy** bug, not a tidiness bug.
- `MemoryService.forget()` is a direct awaited call, **durable before it returns**. Losing a
  deletion is a privacy bug, so it is never an event.
- Raw transcripts are retained **90 days**, and the retention is **enforced** by `EpisodeRecorder`'s
  bounded prune pass — not merely declared (§7.5).
- The "what do you know about me?" audit is currently a **spoken query**; the endpoint is #386.
- ⚠️ **Two escapes, named in §13.3**: provisioning backups under `/var/backups/robot/` sit outside
  the retention window and outside `forget`'s reach, and **user content reaches the logs in two
  known places** (the `forget` query text and the "routine fact stored with no schedule" warning).
  Secrets are never logged; *no personal content is ever logged* is a different claim, and false.

## 4. Availability & fail-fast — §3.12.3

- A **bad API key (401) fails fast at boot** — the robot refuses to start, loudly. **Nothing except
  a bad key at boot is allowed to stop the robot**; every other external failure degrades to
  `DEGRADED` rather than crashing.

## 5. Logging & PII — §3.12.2, §13.3

- **Structured JSON via the stdlib `logging` module** — `ObservabilityService` composes it with
  `logging` + `json.dumps`. ⚠️ This file credited `structlog` until AVID-21; it is not a dependency
  of this project and is imported nowhere (the SDS corrected the same sentence in AVID-378).
- Logs go to **journald with `Storage=volatile`** and a 64 MB cap (`deploy/journald-avid.conf`),
  **not to the card** — so nothing in the journal survives a reboot.
- Secrets are never logged. Every failure path logs with its **correlation ID**.

## 6. Dependencies & supply chain — §13.6, ADR-008

- Model snapshots are **pinned and dated**. As configured in `config/pi.toml`:
  `gpt-realtime-2025-08-28` (flagship, chosen for latency — see the file's own table),
  `whisper-1` for input transcription. No floating tags. ⚠️ This entry named the *mini* snapshot
  for three weeks after the flagship shipped — the literal is deliberately not repeated here,
  because `tests/docs/test_security_claims.py` requires every model named in this file to be one
  the **Pi profile** pins, and a historical example would have to be exempted to survive that.
- **`uv.lock` is committed and CI installs `--frozen`.** Runtime dependencies are **`pydantic`
  alone** (ADR-012); numpy, onnxruntime and `picamera2` are opt-in extras confined to adapters.
- `picamera2` is imported only inside its one `Real*Camera` adapter — it never touches the laptop
  dev/CI environment.
- Migrations are **ordered, checksummed and append-only** (§8.6): editing an applied migration
  fails at boot.
- Secret scanning runs on every pull request as a repository integration.
- Known hazards: the `picamera2` ARM64 wheel has shipped a 32-bit binary historically; `sqlite-vec`
  is deferred until it hits 1.0 and we exceed ~50k facts. Prefer the dependency-light path.

## 7. Input validation

- `Servo.move_to` **clamps to configured safe limits internally** — safety invariants live at the
  lowest layer that can enforce them universally (SDS §3.9.1; contract test
  `test_clamps_beyond_limits`).
- `conversation.user_transcribed` carries `is_approximate: bool`; downstream fact-extraction and
  episode-recording **must consult the flag** — a barge-in truncates the transcript tail and makes
  it unreliable (SDS §9.1.3).

## 8. Camera & microphone — §13.5

- **Frames are captured, detected on, and discarded.** No frame is stored, published on the bus, or
  uplinked. Face detection is local ONNX; the pixels never leave the device.
- Microphone audio is captured continuously **on-device** and uplinked **only inside a turn** — the
  local Silero gate is the single turn-taking authority (ADR-007), plus a bounded **300 ms
  pre-roll** so a turn does not lose its first syllable.
- ⚠️ **There is no capture indicator**, and presence polling deliberately continues through
  `SLEEPING`. Quiet hours suppress *proactive turns*, not *capture*. A guest is never asked. §13.5
  records these as accepted properties of a single-user device.

---

## Reporting a vulnerability

This is a private, single-maintainer project. Report suspected security issues by opening a
**private** security advisory on the GitHub repository, or contact the maintainer directly. Do not
open a public issue for an unpatched vulnerability.
