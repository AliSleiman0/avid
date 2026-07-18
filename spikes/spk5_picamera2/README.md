# SPK-5 — Picamera2 + UV + `--system-site-packages`

Timeboxed spike (PMP §6.4, 1 IED) buying down **ADR-008 / M2**. Question: does
`uv venv --system-site-packages` on the Pi pick up the apt-installed
`python3-picamera2` (+ `libcamera`) under the Pi's **system Python 3.11**?

The deliverable is [`FINDINGS.md`](FINDINGS.md) + a summary posted to issue #43.
`check.sh` is the reproducer — it runs **on the Pi**, not in CI (there is no camera
or `libcamera` on the CI runners, by design: `picamera2` is apt-only, imported in
one adapter — SDS §3.11.2).

## Run (on the Pi)

```bash
scp spikes/spk5_picamera2/check.sh alisleiman0@AVID:~/
ssh alisleiman0@AVID 'bash ~/check.sh'
```

## Answer

**Yes** — proven end-to-end (import → enumerate → capture a frame) through a
`uv venv --python /usr/bin/python3 --system-site-packages`. The `--python
/usr/bin/python3` flag is load-bearing: without it uv downloads a managed 3.13
that can't see the apt `picamera2`. Full detail and versions in `FINDINGS.md`.
