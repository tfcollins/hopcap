# CLI

| Command | What |
|---|---|
| `hopcap plan --plan F --dry-run` | compile offline; slots, edges, descriptor hex, timing |
| `hopcap run --plan F --iio-uri U --target T --out D [--dry-run]` | live run (or resolved config) |
| `hopcap decode --spool S --run R --out D` | rebuild a bundle from a spool |
| `hopcap verify {bitstream,boot,run} DIR` | fail-closed evidence checks |

`run` overrides: `--settle-us`, `--capture-us` or `--capture-samples`, `--period-us`,
`--lookahead`, `--receiver c|python`, `--rx-cpu`, `--hopctrl-base`, `--dest-mac`.
