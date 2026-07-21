"""``Cue`` — the degraded-mode audio vocabulary (AVID-80, SDS §6.9).

The names of the short local sounds the robot can play with **zero network**: a boot
chime, "thinking" cues, and a handful of degraded/fallback speech phrases (SDS §6.9,
lines 1221-1223 — *"perhaps 20 short files… does more for perceived quality than any
optimisation"*). This is the R-01 contingency that makes perceived latency *designable*:
a robot that visibly and audibly thinks feels responsive; one that sits silently feels
broken.

**Pure vocabulary, nothing else.** Like :class:`~avid.domain.affect.Affect`, this module
is a bare enum — stdlib only, no filesystem, no ``Path``, no ``.wav`` strings (P1). A
*cue* is a name the rest of the system can ask for; *which file* backs it and *how* it
plays live one layer out, in ``avid.services.cue_bank`` (``CUE_FILES`` + ``CueBank``),
because only there does a filesystem exist. That split is exactly what keeps the domain
free of I/O — a caller names ``Cue.ONE_MOMENT``, never a path.

*When* a cue plays — the 600 ms thinking-cue timer, the boot chime at boot, a degraded
phrase on a dropped session (SDS §6.9) — is an M5 ``ConversationService`` concern. The
domain only enumerates what *can* be said.
"""

from __future__ import annotations

from enum import Enum, auto


class Cue(Enum):
    """A named degraded-mode sound (SDS §6.9). Backed by a shipped WAV via ``CUE_FILES``.

    Grouped by role: the boot chime; the "thinking" cues played while first audio is
    awaited; and the degraded/fallback speech the robot falls back to with no network —
    connection trouble, comprehension failures, generic errors, and farewells. ~20 short
    clips in all (SDS §6.9). Members are named for *intent*, not wording, so re-recording
    a phrase is a swap behind the manifest, never a rename here.
    """

    # Boot — a short non-speech chime (the only synthesized, non-TTS member).
    BOOT_CHIME = auto()

    # Thinking cues — the "hmm" family, played while the first real audio is awaited.
    THINKING_HMM = auto()
    THINKING_LET_ME_SEE = auto()
    THINKING_ONE_SEC = auto()

    # Greeting / acknowledgement — the everyday no-network niceties.
    GREETING = auto()
    ACKNOWLEDGE = auto()
    LISTENING = auto()

    # Connection degraded — SDS §6.9's canned WAVs for a dropped/absent session.
    CONNECTION_TROUBLE = auto()
    LOST_CONNECTION = auto()
    RECONNECTING = auto()
    ONE_MOMENT = auto()
    BACK_ONLINE = auto()

    # Comprehension fallback — the model never answered / speech wasn't understood.
    DIDNT_CATCH = auto()
    SAY_AGAIN = auto()
    TROUBLE_HEARING = auto()

    # Generic error — something failed on our end, gracefully.
    SOMETHING_WRONG = auto()
    TRY_AGAIN_LATER = auto()
    NEED_A_MOMENT = auto()

    # Farewell / rest.
    GOODBYE = auto()
    RESTING = auto()
