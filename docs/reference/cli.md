# CLI

| Command | What |
|---|---|
| `hopcap plan --plan F --dry-run` | compile offline; slots, edges, descriptor hex, timing |
| `hopcap run --plan F --iio-uri U --target T --out D [--dry-run]` | live run (or resolved config) |
| `hopcap decode --spool S --run R --out D` | rebuild a bundle from a spool |
| `hopcap verify {bitstream,boot,run} DIR` | fail-closed evidence checks |
| `hopcap settle DIR... --ref-hz F [--tol-hz] [--json]` | hop settle from a fixed RF reference ([how-to](../how-to/measure-settle.md)) |

`run` overrides: `--settle-us`, `--capture-us` or `--capture-samples`, `--period-us`,
`--lookahead`, `--receiver c|python`, `--rx-cpu`, `--hopctrl-base`, `--dest-mac`.

`plan` and `run` take `--platform zu11eg|adrv9361`; without it the plan file's `platform` key
decides, else `zu11eg`. The platform supplies the Tick clock (`--tick-hz` overrides it: 100 MHz on
the ZU11EG, 50 MHz on the ADRV9361), the GPIO bit map, the hop-controller base, the receiver
(`--bind`, `--dest-mac`, `--rx-interface` default to it) and the framer's source tuple, MTU and PPS
source. An `adrv9361` plan has `slots` (`rx_lo_hz`, `tx_lo_hz`) and `hops` instead of
`frequencies_hz`; see [Run on the ADRV9361-Z7035](../how-to/adrv9361-lablp.md).
