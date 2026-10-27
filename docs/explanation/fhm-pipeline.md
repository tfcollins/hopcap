# The Talise FHM pipeline

Validated on `tron` (tick `STATUS.md`): the Talise frequency-hopping engine is one stage deep.
A GPIO edge switches the RF PLL to the frequency staged **before the previous edge**; the first
edge after enabling FHM lands on `fhmInitFrequency` (2450 MHz by default).

So for a plan `[A, B, C]` hopcap emits four edges:

| edge | commits | host stages before this edge fires |
|---|---|---|
| 0 (priming) | 2450 MHz | A |
| 1 | A | B |
| 2 | B | C |
| 3 | C | — |

`FhmPipelineModel` tracks this host-side and predicts the PLL readback after each edge; the run
compares `frequency_hopping_mode` readback against it and against the Context frequency.

Staging is a libiio write between edges: **hard-timed edge, soft-timed staging**. The hop period
floor is therefore host latency (milliseconds), not Tick. A staging write that lands after the
next edge is recorded as `stage_late` and that hop is `invalid`.

Slot words are host-written: slot *k* holds the frequency live *after* edge *k*. Nothing about
the pipeline is in RTL.
