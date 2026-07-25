"""The §8.2 embedding wire format — pack a vector into its on-disk BLOB (#120/#122).

One pure, stdlib home for the ``384×float32 little-endian, pre-normalised`` layout every embedding
is stored and matched in (SDS §8.2). It lives in ``core`` rather than an adapter because **both**
sides of the port boundary need it and neither may import the other's layer: the write path
(``MemoryService``, a service) packs a freshly embedded vector before handing the BLOB to
``FactRepository.add`` and the ``Retriever`` (P1 forbids a service importing the retriever adapter),
and the index adapter unpacks the very same ``<f4`` bytes back into its matrix (§8.5). Keeping the
format in one place is what stops the packer and the unpacker drifting.

Deliberately **stdlib** (``array``), not ``numpy`` (P1/ADR-012, P8): packing runs on the write path,
and pulling numpy's one-time import onto the event loop there would block it — the heavy numpy work
stays in the index adapter, off the loop. ``core`` therefore stays ``numpy``-free.
"""

from __future__ import annotations

import sys
from array import array
from collections.abc import Sequence

# The §8.2 dtype: 384 × float32, **little-endian**, pre-normalised. Packing (here) and the index
# adapter's numpy unpack both pin this so a vector round-trips byte-identically regardless of host
# byte order. ``numpy.frombuffer(blob, dtype=EMBEDDING_DTYPE)`` is the matching read.
EMBEDDING_DTYPE = "<f4"


def pack_embedding(vector: Sequence[float]) -> bytes:
    """Pack a pre-normalised embedding into the §8.2 384×float32 LE BLOB (:meth:`FactRepository.add`).

    ``array('f')`` is native-endian float32, byte-swapped on a big-endian host so the bytes are always
    the little-endian §8.2 layout :data:`EMBEDDING_DTYPE` describes. Stdlib only, so it is safe to call
    on the event loop (unlike numpy's import); the matrix build stays off-loop in the index adapter.
    """
    packed = array("f", vector)
    if sys.byteorder == "big":  # pragma: no cover - CI/Pi are little-endian
        packed.byteswap()
    return packed.tobytes()


__all__ = ["EMBEDDING_DTYPE", "pack_embedding"]
