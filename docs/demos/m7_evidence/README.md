# M7 memory evidence — and what it says about the seal

`facts.json` is the declared corpus: twenty facts, each with the sentence to say, the needle that
identifies the stored row, and the probe that should retrieve it, plus three semantic probes. It is
**part of the criterion, not a sample** — AC-2's denominator is "the 20 facts stated", so a corpus
that drifts to 19 or 21 silently changes the bar.

`recall_result.json` and `mutations_result.json` are the **actual output of the M7 seal run**, on
the Pi, 2026-08-14.

## ⚠️ These two files sat on the rig, untracked, for ten days

They were recovered from `/opt/avid` on 2026-08-24 while deploying, and until then the repo's
position — stated in `tests/e2e/test_m7_gate.py` — was that *"that run's `recall_result.json` was
never committed, only `facts.json` was."* That was true, and it cost #447 a question it could not
answer: **whether the sealed run would score the same today.**

They are committed here because a gate's output is evidence, and evidence that lives on one SD card
is evidence that ends with the SD card. What they record:

| criterion | sealed verdict |
|---|---|
| **AC-2** recall of stated facts | **18/20 = 90% — FAIL** (misses `standup`, `dog`) |
| AC-1 extraction: 19/20 declared facts landed | recorded, not graded |
| **AC-1** extraction was silent | **FAIL** — "announced: noted" |
| AC-3 *"what did I say about my sister?"* | pass |
| **AC-3** *"tell me about Biscuit"* | **FAIL** |
| AC-3 *"what do I know about Karim?"* | pass |
| **AC-4** the superseded row carries its pointer | **FAIL** |
| AC-4 (×3 others), AC-5 (×3) | pass |

**M7 was sealed with O2 unmet and four criteria failing.** That is not a rediscovered scandal — each
one has an issue and most are since fixed — but the *number* has been quoted since as though it
were a pass, and it was not.

## Both of the sealed AC-2 misses were later explained, and neither was the retriever

- **`standup`** — the model stored *"stand-up"*, the needle said *"standup"*, and `_norm` folded
  whitespace but not punctuation, so a **correctly stored fact** scored as a recall miss. The
  `v0.M7.0` tag called it *"a scoring artifact"* and nobody filed it; #450 fixed the fold nine days
  later.
- **`dog`** — #264. The proper-noun probe *"tell me about Biscuit"*, which §7.7's whole keyword
  branch exists to answer, and which was **inert** until #375 carried a keyword hit into the score.

⚠️ **So the sealed 18/20 and #447's 18/20 are different failures wearing the same number** — and
#447's own two misses (`guitar`, `marathon`) were measured at δ = 1.0, before #451. Three distinct
pairs of misses, one aggregate. **An aggregate is not a diagnosis**, which is why the recall tests
in `tests/e2e/test_m7_gate.py` assert named probes alongside the rate.

## Reproducing the numbers

The sealed run is not reproducible — its store was built by a live conversation and is gone, and
both the matcher (#450) and δ (#451) moved afterwards. What *is* reproducible is the corpus against
a real-MiniLM store:

```sh
python tools/fetch_minilm.py --dest ./models
uv run --frozen --with onnxruntime --with tokenizers pytest -m real_embedder
```

⚠️ That store is the **best case** and the tests say so: every fact gets the same importance and one
shared timestamp, so `_min_max` collapses the recency and importance terms to constants, and
extraction cannot fail because the rows are written directly. O2 says *"at 30 days"*; nothing there
ages anything. A pass is necessary and not sufficient.
