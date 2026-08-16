# `tests/assets/audio/`

## `ambient_hum_5s.wav`

Five seconds of **empty room**, recorded on the Pi rig on 2026-08-15 with `robot.service`
stopped, from the USB PnP mic at `plughw:CARD=Device,DEV=0` — 16 kHz, mono, S16_LE.

**Contains no speech.** Nobody was in the room and nothing was played. That is what makes it
committable where `assets/vision/` deliberately is not: there is no voice, no face, and nothing
identifying anyone.

It is the recording AVID-283 was filed from, and it is here so the fix can be asserted against
the *measurement that motivated it* rather than against a synthetic tone:

```
raw broadband            -18.4 dBFS   <- what EchoFloor was reading
speech band 300-3400 Hz  -49.1 dBFS   <- what a human calls silence
dominant below 500 Hz     50 Hz
Silero frames = speech    0 / 250      <- the local VAD is untroubled by it
```

⚠️ **Read the last row before drawing conclusions from this file.** The −18.4 dBFS floor is real
and it is what the high-pass exists to fix, but it did *not* open phantom sessions — Silero never
fired on it. The dropped turns AVID-283 reported were `Auto Gain Control` (AVID-296), which is a
capture-mixer setting, not this hum. Two defects were tangled in one issue for a fortnight.

Larger recordings from the same investigation — `amb3.wav`, the 25 s AGC-on sample, and the
speech control — live on the Pi under `~/vision_traces/logs/` rather than here. Only the one
needed by a test is committed.
