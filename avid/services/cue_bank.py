"""The degraded-mode WAV cue bank — say *something* with zero network (AVID-80, SDS §6.9).

SDS §6.9's least glamorous, highest-leverage deliverable: a handful of short local sounds
the robot can play when the network (or the model) is unavailable. This module is the
application half of that bank — the :data:`CUE_FILES` manifest that binds a pure
:class:`~avid.domain.cues.Cue` name to a shipped WAV filename, and :class:`CueBank`, which
resolves a *named cue* to a file under an injected base directory and plays it through the
:class:`~avid.core.ports.Speaker` port (``play_file``, already on the port from AVID-54).

**Why a bank and not a bare call.** Callers name a cue (``Cue.ONE_MOMENT``), never a path
(AC-2): the cue→file binding lives here, in one place, and the *"assets missing"* fallback
(AC-4) lives here too — the bank is best-effort perceived quality, not a correctness
obligation, so a missing directory or file logs and is swallowed rather than crashing a
turn. The base directory is **injected** (P7): the module never reads the environment or a
config file; ``main.py`` will hand it the path from ``[cues] dir`` (that wiring is #89).

**Not a reactive service.** :class:`CueBank` owns no task, subscribes to nothing, and
publishes nothing — playing a degraded cue is a best-effort *direct awaited call* (CLAUDE.md
§4, like a render or a servo move), so it is a method the future ``ConversationService`` (M5)
calls, not an event. It depends only on the ``Speaker`` **Protocol** (P2) and the pure
``Cue`` vocabulary; it is constructed by the composition root, never here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

from avid.core.ports import Speaker
from avid.domain import Cue

_log = logging.getLogger(__name__)

# The manifest (AC-2): every ``Cue`` → the *relative* filename that backs it, under the
# injected asset directory. Filenames only — the base path is a config concern resolved by
# ``CueBank``, never here. The ``_require_exhaustive`` guard below fails loudly at import if a
# cue is ever added without a file, so the map can never silently drift from the vocabulary.
CUE_FILES: Mapping[Cue, str] = MappingProxyType(
    {
        Cue.BOOT_CHIME: "boot_chime.wav",
        Cue.THINKING_HMM: "thinking_hmm.wav",
        Cue.THINKING_LET_ME_SEE: "thinking_let_me_see.wav",
        Cue.THINKING_ONE_SEC: "thinking_one_sec.wav",
        Cue.GREETING: "greeting.wav",
        Cue.ACKNOWLEDGE: "acknowledge.wav",
        Cue.LISTENING: "listening.wav",
        Cue.CONNECTION_TROUBLE: "connection_trouble.wav",
        Cue.LOST_CONNECTION: "lost_connection.wav",
        Cue.RECONNECTING: "reconnecting.wav",
        Cue.ONE_MOMENT: "one_moment.wav",
        Cue.BACK_ONLINE: "back_online.wav",
        Cue.DIDNT_CATCH: "didnt_catch.wav",
        Cue.SAY_AGAIN: "say_again.wav",
        Cue.TROUBLE_HEARING: "trouble_hearing.wav",
        Cue.SOMETHING_WRONG: "something_wrong.wav",
        Cue.TRY_AGAIN_LATER: "try_again_later.wav",
        Cue.NEED_A_MOMENT: "need_a_moment.wav",
        Cue.GOODBYE: "goodbye.wav",
        Cue.RESTING: "resting.wav",
    }
)


def _require_exhaustive() -> None:
    """Fail at import if any ``Cue`` lacks a file — the manifest must cover the vocabulary.

    Mirrors ``domain/state.py``'s table invariants: a cue added to the enum without a WAV
    would otherwise ``KeyError`` only when that cue happened to be played, in production.
    """
    missing = set(Cue) - set(CUE_FILES)
    if missing:
        names = ", ".join(sorted(cue.name for cue in missing))
        raise RuntimeError(f"CUE_FILES is missing an entry for: {names}")


_require_exhaustive()


class CueBank:
    """Plays a named :class:`~avid.domain.cues.Cue` through the ``Speaker``, or degrades quietly.

    Resolves ``cue`` to ``asset_dir / CUE_FILES[cue]`` and plays it via
    :meth:`~avid.core.ports.Speaker.play_file` — the degraded-mode path, no ``play`` streaming
    and no Realtime session. When the bank is unavailable (``asset_dir`` omitted, or the file
    absent) it logs with the turn's correlation id and returns: perceived quality is
    best-effort, never a reason to crash a turn (AC-4).

    Depends only on the ``Speaker`` **Protocol** (P2). ``asset_dir`` is injected (P7) and may
    be ``None`` to mean *no bank configured*. Constructed by the composition root (P3).
    """

    def __init__(self, *, speaker: Speaker, asset_dir: Path | None) -> None:
        self._speaker = speaker
        self._asset_dir = asset_dir

    async def play(self, cue: Cue, *, correlation_id: UUID) -> None:
        """Play *cue*'s WAV, or log-and-return if the bank is unavailable (AC-4).

        ``correlation_id`` ties any degradation log to the turn it belongs to (SDS §3.12.2), so
        a missing cue is greppable to its turn. The missing-*file* case rides on
        ``play_file`` raising from its worker thread — no filesystem ``stat`` touches the loop
        (P8); only the missing-*directory* case is decided in-process, which needs no I/O.

        A **third** failure has no exception to ride on: the file is there, the device takes
        nothing, and the robot is silent at the one moment it was trying hardest to speak.
        ``play_file`` returns the ms accepted (AVID-91), so that case is logged too.
        """
        if self._asset_dir is None:
            _log.warning(
                "no cue asset directory configured; skipping %s [correlation_id=%s]",
                cue.name,
                correlation_id,
            )
            return
        path = self._asset_dir / CUE_FILES[cue]
        try:
            played_ms = await self._speaker.play_file(path)
        except (FileNotFoundError, OSError) as exc:
            _log.warning(
                "cue %s unavailable at %s: %s [correlation_id=%s]",
                cue.name,
                path,
                exc,
                correlation_id,
            )
            return
        if played_ms == 0:
            # The file opened and the device took nothing — the degraded path degraded. A
            # *short* return is a barge-in truncating the cue and is normal; zero is not.
            # Not knowing the clip's length is deliberate: reading it would put a file stat
            # on the loop (P8) to learn something the return value already tells us.
            _log.warning(
                "cue %s played no audio from %s [correlation_id=%s]",
                cue.name,
                path,
                correlation_id,
            )
