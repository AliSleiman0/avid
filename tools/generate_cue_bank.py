"""Regenerate the degraded-mode WAV cue bank in ``assets/cues/`` (AVID-80, SDS §6.9).

A **dev-only** tool — it is not imported by the application, not run in CI, and lives
outside ``avid/`` so it is clear of ruff/mypy/coverage. The committed WAVs are what ship;
this script only documents *how* they were made and lets them be regenerated.

Two sources, both offline and zero-network:

* **The boot chime** — procedurally synthesized with the stdlib (``wave`` + ``math``): a
  short fading major arpeggio. Unambiguously CC0 / public domain.
* **The spoken cues** — Windows **SAPI** (``System.Speech.Synthesis``) via PowerShell,
  pinned to 24 kHz mono 16-bit (``SpeechAudioFormatInfo``), one phrase per file, using the
  Microsoft Zira Desktop voice. Windows-only; on Linux/Pi the committed files are used as
  is. See ``assets/cues/NOTICE.md`` for provenance and the re-record-if-public caveat.

Every file is **24 kHz mono 16-bit** to match the playback format the ``Speaker`` port
plays (SDS §6.2.4). Run from the repo root:  ``python tools/generate_cue_bank.py``
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

# Import the manifest so filenames can never drift from the shipped bank.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avid.domain import Cue  # noqa: E402
from avid.services.cue_bank import CUE_FILES  # noqa: E402

_SAMPLE_RATE = 24_000
_VOICE = "Microsoft Zira Desktop"
_OUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "cues"

# The wording each spoken cue is recorded with. The source of truth for the *text*; the
# ``Cue`` member names carry the intent, so re-wording a line never renames a member.
PHRASES: dict[Cue, str] = {
    Cue.THINKING_HMM: "hmm.",
    Cue.THINKING_LET_ME_SEE: "let me see.",
    Cue.THINKING_ONE_SEC: "one sec.",
    Cue.GREETING: "hi there.",
    Cue.ACKNOWLEDGE: "okay.",
    Cue.LISTENING: "I'm listening.",
    Cue.CONNECTION_TROUBLE: "I'm having trouble connecting.",
    Cue.LOST_CONNECTION: "one sec, I lost my connection.",
    Cue.RECONNECTING: "give me a moment to reconnect.",
    Cue.ONE_MOMENT: "one moment.",
    Cue.BACK_ONLINE: "okay, I'm back.",
    Cue.DIDNT_CATCH: "sorry, I didn't catch that.",
    Cue.SAY_AGAIN: "could you say that again?",
    Cue.TROUBLE_HEARING: "I'm having trouble hearing you.",
    Cue.SOMETHING_WRONG: "something went wrong on my end.",
    Cue.TRY_AGAIN_LATER: "let's try again in a bit.",
    Cue.NEED_A_MOMENT: "I need a moment.",
    Cue.GOODBYE: "talk to you later.",
    Cue.RESTING: "I'll be here if you need me.",
}

# The SAPI driver: reads text + output path from the environment (so no quoting of the
# phrase into a command line), pins the wave format to 24 kHz mono 16-bit, and speaks.
_SAPI_SCRIPT = """
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($env:CUE_VOICE)
$s.Rate = -1
$s.SetOutputToWaveFile($env:CUE_OUT, $fmt)
$s.Speak($env:CUE_TEXT)
$s.Dispose()
"""


def _synth_boot_chime(path: Path) -> None:
    """Write a short fading C-major arpeggio (C5-E5-G5-C6) — stdlib only, CC0."""
    notes = [523.25, 659.25, 783.99, 1046.50]  # C5 E5 G5 C6
    note_s = 0.16
    amplitude = 0.35
    samples: list[int] = []
    for i, freq in enumerate(notes):
        n = int(_SAMPLE_RATE * note_s)
        for k in range(n):
            t = k / _SAMPLE_RATE
            # Per-note attack/decay envelope so notes don't click into each other.
            env = min(1.0, k / (0.01 * _SAMPLE_RATE)) * (1.0 - k / n)
            # Let the final note ring a little longer for a settled ending.
            if i == len(notes) - 1:
                env = min(1.0, k / (0.01 * _SAMPLE_RATE)) * (1.0 - k / n) ** 0.5
            samples.append(int(amplitude * env * math.sin(2 * math.pi * freq * t) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(b"".join(struct.pack("<h", s) for s in samples))


def _synth_speech(text: str, path: Path) -> None:
    """Speak *text* to *path* at 24 kHz mono 16-bit via Windows SAPI (PowerShell)."""
    env = {"CUE_TEXT": text, "CUE_OUT": str(path), "CUE_VOICE": _VOICE}
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT],
        check=True,
        env={**_os_environ(), **env},
    )


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def main() -> int:
    if sys.platform != "win32":
        print(
            "This generator uses Windows SAPI and only runs on Windows. "
            "The committed WAVs in assets/cues/ are what ship; regenerate on a Windows box."
        )
        return 1
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cue, filename in CUE_FILES.items():
        out = _OUT_DIR / filename
        if cue is Cue.BOOT_CHIME:
            _synth_boot_chime(out)
        else:
            _synth_speech(PHRASES[cue], out)
        print(f"  wrote {out.relative_to(_OUT_DIR.parents[1])}")
    print(f"Generated {len(CUE_FILES)} cue files into {_OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
