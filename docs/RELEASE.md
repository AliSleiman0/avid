# Releases — what a tag produces, and what it does not

> PMP §11.4 has always said *"each tag builds a Pi-deployable artifact — so every milestone is a
> thing that exists, permanently, that you can go back and run."* Nine tags (`v0.M0.0` … `v0.M10.0`)
> went by with **nothing behind any of them**, and the clause was simply false. This document is
> AVID-388's half of the answer; `.github/workflows/release.yml` is the other half.
>
> It matters more than release hygiene usually does, because it is the named mitigation for
> **R-03 — motivation decay, the highest-scored risk in the register.** The mitigation is a visible
> artifact trail, and nine tags with nothing runnable behind them is a weaker trail than the
> register assumed it had.

## What "Pi-deployable" means here — decided, not assumed

**The bundle is not the deployment mechanism, and pretending otherwise would be the more
impressive lie.** The Pi runs an *editable* install of a git checkout and updates with `git pull`;
that decision is load-bearing enough to have its own reasoning in `avid/adapters/build_id.py`, and
it is why the runtime build identifier is `git describe` rather than a packaged version.

So the artifact is the **permanence guarantee**: everything needed to stand this exact build up
again, minus what the Pi supplies itself.

`avid-<tag>-pi.tar.gz` contains:

| | Why it is in there |
|---|---|
| the built **wheel** and **sdist** | the application itself, installable anywhere |
| `pyproject.toml`, `uv.lock` | the exact dependency resolution — "go back and run it" must mean *that* resolution, not today's |
| `config/` | the profiles, **including `personality/`** — a build without the personality is not that build's behaviour |
| `deploy/` | the systemd units, the journald drop-in, `PI_OPERATIONS.md` and `RUNBOOK.md`. A build without the unit that supervises it is not something you can stand up |
| `README.md`, `SECURITY.md` | what it is, and the rules it is operated under |
| `BUILD_INFO` | tag, commit, `git describe`, and the workflow run that produced it |

Deliberately **excluded**, each for a reason:

- **The ONNX model blobs** (Silero, MiniLM, YuNet) — hundreds of megabytes, fetched by
  `tools/fetch_*.py` against pinned revisions with SHA-256 verification, and Pi-gated (SDS §14.4).
- **`picamera2`** — apt, never pip (ADR-008).
- **Anything resembling a secret** — and this is *checked*, not trusted:
  `tools/build_release.py`'s `verify_bundle` reads the assembled archive back and refuses on an
  OpenAI-shaped key, an assigned `OPENAI_API_KEY`, a private-key block, or a file whose name alone
  disqualifies it (`.env`, `.pem`, `.key`, `robot.env`).

## The path a tag takes

```
git tag v1.0.0 && git push origin v1.0.0
   │
   ├── build    stamp the version → uv build → assemble the bundle → generate the notes
   ├── verify   install the wheel on 3.11, import it, run `avid --help`, re-check the bundle
   └── publish  gh release create, with the notes and every artifact
```

**`publish` depends on `verify`.** That edge is the whole of AC-3: an artifact nobody has installed
is a tarball, not a release, and a verification step running *beside* the thing it gates is a gate
that passes on silence. `tests/deploy/test_release_workflow.py` asserts the edge still exists.

Verification runs on **3.11** — ADR-008's floor and the interpreter the Pi actually runs. Proving
the wheel on 3.13 would prove the wrong thing.

### The version, and why `pyproject.toml` still says `0.0.0`

The tree keeps `version = "0.0.0"` and the workflow stamps the tag in at build time, ephemerally.
This is not laziness: the Pi updates with `git pull` against an editable install, so a *committed*
version would be frozen at install time and would still have read `0.0.0` after the five-commit
pull that exposed this whole issue (#388's first comment). The runtime identifier stays
`git describe`; only the artifact carries the tag.

⚠️ The canonical version is then read **back off the built wheel's filename**, not re-derived from
the tag, because PEP 440 normalises `v1.0.0-rc.1` to `1.0.0rc1` — and a check comparing the
installed version against the raw tag would fail on exactly the pre-release tags a dry run uses.

### Release notes

`tools/release_notes.py`, from Conventional Commits (PMP §11.2), with no third-party action and no
new dependency. ⚠️ Two limits, stated rather than implied:

- It reads **subjects**, so it detects a breaking change from `type!:` and **cannot** see a
  `BREAKING CHANGE:` footer in a body. Use the bang.
- PMP §11.2 says the convention is *"enforced by `commitlint` in CI"* and **no such job exists**.
  So the generator **reports what it could not classify** in its own section with a count, rather
  than dropping it — a tidy list that silently omitted a non-conforming commit would turn a
  drifting habit into an invisible one.

## Dry-running it — do this before a tag needs it

The workflow accepts a `workflow_dispatch` with a tag to build *as*. It builds and verifies, and
then stops: `publish` is gated on the ref actually being a tag, so a dry run cannot create a
release for a tag that does not exist.

```sh
gh workflow run release.yml -f tag=v0.0.0-rc.1
gh run watch
```

The tag must be PEP 440-valid after the leading `v`, or `uv build` rejects it.

## ⚠️ Earlier tags do not get artifacts retroactively (AC-5)

**The trail starts at the first tag this workflow builds.** `v0.M0.0` … `v0.M10.0` remain
source-only tags, and that is a decision rather than an omission:

> An artifact built today from an old tag would be built with **today's toolchain against that
> tree**, and labelled as that milestone's build. It would be a fabrication of a build that never
> happened. For a trail whose entire purpose is *"a thing that exists, permanently, that you can go
> back and run"*, a plausible reconstruction is worse than an honest gap — you would be unable to
> tell which entries in the trail were real.

Those tags are still checkouts, and `uv build` at any of them produces a wheel on demand. What they
do not get is a published artifact claiming to be from their day.

## What this path deliberately does not do

- **It does not deploy.** Nothing here touches the Pi. Deployment stays a deliberate, verified act
  (`deploy/PI_OPERATIONS.md`), and a pipeline that pushed to the robot mid-soak would invalidate the
  very run it was watching (SDS §12.6).
- **It does not sign.** There is no artifact signing and no provenance attestation; SDS §13.6
  records that as accepted risk with its compensating controls.
- **It does not decide when to tag.** `v1.0.0` is the M11 gate's call and depends on the soak
  result (#389).
