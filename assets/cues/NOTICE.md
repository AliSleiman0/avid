# Cue bank — provenance & licensing (AVID-80, SDS §6.9)

The degraded-mode WAV bank: ~20 short clips the robot plays with **zero network** (a
boot chime, "thinking" cues, and fallback speech). All files are **24 kHz mono 16-bit**
to match the `Speaker` playback format (SDS §6.2.4). The cue→file map is
`avid/services/cue_bank.py::CUE_FILES`; regenerate with `python tools/generate_cue_bank.py`
(Windows only — see below).

## Sources

| File | Source | License |
|---|---|---|
| `boot_chime.wav` | Procedurally synthesized (stdlib `wave`, a C-major arpeggio) by `tools/generate_cue_bank.py` | Public domain / CC0 |
| all other `*.wav` | Windows **SAPI** text-to-speech (`System.Speech.Synthesis`, *Microsoft Zira Desktop* voice) via `tools/generate_cue_bank.py` | See caveat below |

The exact spoken wording for each file lives in `tools/generate_cue_bank.py` (`PHRASES`).

## Caveat — before making this repo public

The spoken clips are output of a Microsoft-bundled TTS voice, generated on the developer's
Windows machine. That is fine for this **private** companion-robot project. If the
repository is ever published, **regenerate the spoken cues** with an unambiguously
redistributable voice (e.g. [Piper](https://github.com/rhasspy/piper), CC0/MIT models) —
because callers name a `Cue`, not a path, that is a drop-in swap behind `CUE_FILES` with no
code change. The boot chime is already CC0 and needs no action.

## Regenerating

`tools/generate_cue_bank.py` is a **dev-only** tool (not imported by the app, not run in
CI). It uses Windows SAPI, so it only runs on Windows; on Linux/Pi the committed files here
are used as-is. The committed WAVs — not the generator — are what ship and what the tests
assert against.
