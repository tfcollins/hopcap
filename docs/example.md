# The example: hard-timed FHM hop, settled and gated capture, hop frequency in Context

This page is the one-stop description of what hopcap does on the ADRV9009-ZU11EG, how the data
flows, and what the first end-to-end run on `tron` produced. Every plot below is rendered from
the committed data of that run (`docs/data/run7/`, 2026-10-23) by
`docs/diagrams/make_run_plots.py`.

```{admonition} Status of these results
:class: note
Run 7 is a first end-to-end result, not a qualification. Five of five hops were captured and
tagged correctly; two captures were short because nemo's XL710 ring (default 512 entries)
missed 178 datagrams during the 8.4 Gb/s bursts. Nothing in the FPGA dropped. See the
[dated report](reports/2026-10-23-tron-first-run.md).
```

## What happens, in one picture

```{mermaid}
flowchart LR
    subgraph HOST["nemo (host)"]
        PLAN["hopcap plan<br/>hop table → slots, edges,<br/>Tick descriptors"]
        SESS["TickSession<br/>submit rise/fall pairs,<br/>stage next frequency,<br/>read back PLL"]
        RX["udp_spool (C)<br/>every datagram + arrival time"]
        ASSOC["associate → SigMF + manifest<br/>Context ↔ capture, never guessed"]
        PLAN --> SESS
        RX --> ASSOC
    end
    subgraph PL["ZU11EG programmable logic (100 MHz sys_cpu_clk)"]
        TICK["axi_timed_command_scheduler<br/>(Tick) @0x86000000"]
        HOP["vrt49_fhm_hop_ctrl<br/>@0x84AC0000<br/>DRAIN→SETTLE→PUBLISH→OPEN"]
        FRAMER["axi_vrt49_framer<br/>IF Data + Context<br/>(VITA 49.2)"]
        UDP["vrt49_udp_tx → Corundum<br/>10GbE QSFP lane 0"]
        TICK -- "gpio_out[3:1] slot" --> HOP
        TICK -- "gpio_out[0] event" --> HOP
        HOP -- "fabric_sample_enable<br/>fabric_context_valid<br/>rf_frequency_q44_20, hop_epoch" --> FRAMER
        FRAMER --> UDP
    end
    subgraph RF["Talise A (ADRV9009)"]
        PLL["RF PLL in FHM<br/>GPIO-trigger mode"]
        ADC["RX ADC 245.76 MSPS"]
    end
    SESS -- "IIO: command_descriptor,<br/>frequency_hopping_mode" --> TICK
    SESS -- "IIO (adrv9009-phy)" --> PLL
    SESS -- "/dev/mem: slots, SETTLE,<br/>CAPTURE_TICKS" --> HOP
    TICK -- "gpio_out[0] → TAL_GPIO_0" --> PLL
    ADC -- "JESD204 → cpack → FIFO" --> FRAMER
    UDP -- "UDP 192.0.2.1:4991 → 192.0.2.2" --> RX
```

Tick's `gpio_out[0]` is both the FHM trigger pin (the path Tick validated on this board) and the
hop controller's event input; the controller never generates the pulse. `gpio_out[3:1]` carries
the slot index, so one Tick descriptor selects which of eight host-written Q44.20 frequency words
the Context will carry.

## One hop, step by step

```{mermaid}
sequenceDiagram
    participant H as host (TickSession)
    participant T as Tick
    participant P as Talise PLL
    participant C as hop controller
    participant F as framer / UDP
    H->>P: stage frequency_hopping_mode = f[k+1]
    H->>T: GPIO_WRITE rise @ t_k (slot k), fall @ t_k + 10 ms
    Note over T: waits for device time t_k
    T->>P: TAL_GPIO_0 rising edge → PLL hops to f[k] (staged before edge k−1)
    T->>C: scheduler_gpio = {slot k, 1}
    C->>F: fabric_sample_enable = 0 (DRAIN)
    F-->>C: fabric_samples_drained
    Note over C: SETTLE_TICKS (200 µs in run 7)
    C->>F: fabric_context_valid + slot word + epoch k+1
    F-->>H: Context packet (CIF0 RF ref freq, CIF2 info source = epoch)
    F-->>C: fabric_context_accepted (packet left)
    C->>F: fabric_sample_enable = 1 (OPEN, CAPTURE_TICKS)
    F-->>H: IF Data packets (683 × 360 samples for 1 ms)
    C->>F: fabric_sample_enable = 0 (CLOSED)
    T-->>H: completion (executed tick); H reads back the PLL
```

The ordering guarantee the host relies on is enforced in hardware and proven in simulation
against the real framer: after an event, no IF Data leaves before the changed Context, and the
gate only reopens once the framer reports the Context has left.

## Hop controller state machine

```{mermaid}
stateDiagram-v2
    [*] --> IDLE
    IDLE --> DRAIN: event (gpio[0] ↑)
    DRAIN --> SETTLE: fabric_samples_drained<br/>or DRAIN_TIMEOUT
    SETTLE --> PUBLISH: settle_ticks elapsed
    PUBLISH --> OPEN: fabric_context_accepted
    PUBLISH --> DRAIN: event after handshake (stale Context, superseded++)
    OPEN --> CLOSED: capture_ticks elapsed
    OPEN --> DRAIN: event
    CLOSED --> DRAIN: event
    DRAIN --> DRAIN: event (latest wins, superseded++)
    SETTLE --> DRAIN: event (restart, superseded++)
```

Registers: see [`vrt49_fhm_hop_ctrl` registers](reference/hopctrl-registers.md). With `enable=0`
the gate is forced open and the design behaves exactly like stock vrt49.

## Host-side dataflow

```{mermaid}
flowchart TD
    SPOOL["datagrams.spool<br/>(mono_ns, real_ns, peer, bytes)"] --> PARSE["vrt.parse<br/>prologue + CIF walker"]
    PARSE --> SEG["segment into windows<br/>split on Context or on a gap<br/>> ½ inter-hop silence"]
    EDGES["EdgeResult per edge<br/>Tick status, executed tick,<br/>PLL readback"] --> JOIN
    SEG --> JOIN["join by epoch<br/>Context immediately before<br/>the window, else invalid"]
    JOIN --> CHK["checks<br/>context_hz == readback == planned<br/>samples ≈ capture, mod-16 continuity"]
    CHK --> OUT["run.sigmf-data / -meta<br/>hops/hop_NNNN.npy<br/>manifest.json<br/>SHA256SUMS.json"]
```

## Run 7 on tron (2026-10-23)

Plan `band-2400-2500-4slot-slow`: edges every 200 ms, 200 µs settle, 1 ms capture, four planned
frequencies plus the priming edge that lands on `fhmInitFrequency` (2450 MHz).

```{figure} _static/run7/timeline.svg
:alt: Tick-domain schedule of the five edges with settle and capture windows; executed markers coincide with requested edges.
:width: 100%

Top: every `GPIO_WRITE` executed at exactly its requested tick (`late_count` unchanged). Bottom:
one hop zoomed, with the 200 µs settle guard and the 1 ms gated window the controller opened and
closed by `CAPTURE_TICKS`; it counted five events and five timer-closed captures, none superseded.
```

```{figure} _static/run7/frequency_per_hop.svg
:alt: Planned, read-back and Context RF frequency per edge; all three coincide for every hop.
:width: 100%

The planned slot word, the Talise PLL readback after each edge and the RF reference frequency the
FPGA wrote into the Context agree on every hop. The priming edge lands on 2450 MHz because the
Talise engine is one edge behind its staging; see [FHM pipeline](explanation/fhm-pipeline.md).
```

```{figure} _static/run7/arrivals.svg
:alt: Datagram arrival histogram on the host: five bursts each preceded by a Context, with silence between.
:width: 100%

Host-side view in nemo's monotonic clock. Top: one burst every 200 ms, each announced by its
Context; the two smaller bursts are the hops the NIC ring lost packets from. Bottom: inside one
burst at 20 µs resolution, the Context arrives first and the 683 datagrams follow within about a
millisecond, i.e. the full 8.4 Gb/s line rate while the gate is open.
```

```{figure} _static/run7/packets_per_hop.svg
:alt: Packets received per hop against the 683 expected; hops 1 and 3 are short by about 90 packets.
:width: 100%

The framer counters show 684 packets per hop with zero drops; the shortfall on hops 1 and 3 is
entirely `rx_missed_errors` on the XL710 (178 in the run). Complete hops carry 683 packets =
245 880 samples: the framer finishes its partial last packet, inside the designed ±1-packet tolerance.
```

```{figure} _static/run7/spectra.svg
:alt: Welch power spectral density of each captured hop; a dominant tone sits at +40 MHz offset in every panel.
:width: 100%

Each capture decoded from the spool and transformed. The dominant tone sits at the same +40 MHz
baseband offset at every RF centre, so it is a self-generated or digital signal, not something
received over the air; the RF input was not characterised for this run. The broadened skirts on
hops 1 and 3 are artefacts of the missing packets (discontinuities in the concatenated samples),
not of the radio.
```

```{figure} _static/run7/iq_excerpt.svg
:alt: First 150 I and Q samples of hop 2.
:width: 100%

The first 150 samples of hop 2 (2440 MHz), as little-endian CI16 straight from the framer payload:
the +40 MHz tone at six samples per cycle.
```

### Numbers from `manifest.json`

| Quantity | Value |
|---|---|
| Edges executed / requested | 5 / 5, skew 0 ticks each |
| Hop controller | 5 events, 5 captures, 0 superseded, no drain timeout |
| Framer / UDP | 3420 packets forwarded, 0 dropped, 0 UDP errors |
| Contexts received | 5, epochs 1..5, none orphaned |
| Datagrams spooled | 3242 (0 malformed, single peer 192.0.2.1:4991) |
| Host NIC `rx_missed_errors` | 178 |
| Complete hops | 3 of 5 (hops 1 and 3 short by 90 and 88 packets) |
| Transport round trip (`iio_attr`) | 15.2 ms → staging floor ≈ 91 ms, hence the 200 ms plan |

### Reproducing the plots

```sh
python3 docs/diagrams/make_run_plots.py       # reads docs/data/run7, writes docs/_static/run7/*.svg
```

`docs/data/run7/` holds the manifest, per-datagram arrival times, per-hop Welch PSDs and a
600-sample I/Q excerpt derived from the run bundle on nemo (`~/hopcap-artifacts/run7`).
