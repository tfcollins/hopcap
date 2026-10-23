# hopcap

**Hard-timed Talise FHM hop, hardware-settled and gated capture, hop frequency in VITA 49.2
Context, over 10G QSFP — on the ADRV9009-ZU11EG.**

```{admonition} Status
:class: warning
In development. The RTL, host runner and build pipeline exist and are tested offline; the
composite bitstream and the first hardware run on `tron` are tracked in `STATUS.md`. Nothing
on this site claims a hardware-validated result until a dated report under *Reports* says so.
```

hopcap composes two upstream projects into one example of the DataX radar-tracking proposal's
dwell loop (close admission, hard-timed profile select, qualified settle, publish Context N+1,
open capture):

- [Tick](https://github.com/tfcollins/tick) fires the Talise frequency-hopping-mode (FHM) GPIO at
  an exact device-time tick.
- [vrt49](https://github.com/tfcollins/vrt49) frames samples as VITA 49.2 over UDP on the QSFP cage.
- A new FPGA block, `vrt49_fhm_hop_ctrl`, watches the same Tick edge, drains and settles, hands
  the hop frequency and a hop epoch to the framer as a hardware Context packet, then opens
  sample admission for a fixed capture length.

```{toctree}
:maxdepth: 1
:caption: Tutorials

tutorials/dry-run
tutorials/first-hardware-run
```

```{toctree}
:maxdepth: 1
:caption: How-to

how-to/build
how-to/tron-nemo-runbook
how-to/decode-and-replay
```

```{toctree}
:maxdepth: 1
:caption: Reference

reference/cli
reference/hopctrl-registers
reference/manifest-schema
reference/requirements-traceability
```

```{toctree}
:maxdepth: 1
:caption: Explanation

explanation/architecture
explanation/fhm-pipeline
explanation/loss-accounting
```

```{toctree}
:maxdepth: 1
:caption: Reports

reports/index
```
