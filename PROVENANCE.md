# Provenance

Upstream code this repository **adapts** (copies and modifies) rather than imports.
Each row is a fail-closed contract: `tests/unit/test_provenance.py` recomputes the
SHA-256 of the upstream file at the recorded revision and fails if it changed, so an
upstream fix cannot silently diverge from our adaptation.  Submodule pins themselves live
in `pins.json`.

| Local file | Upstream | Revision | SHA-256 of upstream | License |
|---|---|---|---|---|
| `hdl/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v` | `vrt49:hdl/vrt49_ad9361_hop_ctrl/vrt49_ad9361_hop_ctrl.v` | submodule pin | `76912cd77c03df1ca9597245c70d8632050e1d8e61de435154d1969f5e235d94` | BSD-2-Clause |
| `src/hopcap/native/udp_spool.c` | `vrt49:tests/integration/sfp_udp_capture.c` | `39d42946cfe973a472621e09d6e2584b4fb8eee9` (main, vrt49 #52) | `c42dfbe12074bb1019cb685b41b5dc810fda4961bffd13383c01355001ed32cd` | LGPL-2.1-or-later |
| `kernel/dts/zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts` | `vrt49:kernel/iio-vrt49-buffer/zynqmp-adrv9009zu11eg-vrt49-control-overlay.dts` | submodule pin | `eb0e38b420b9cc3703958cefe357a4b0fea28879f211077d90f30fc914dc1583` | GPL-2.0 |
| `kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts` | `vrt49:kernel/iio-vrt49-buffer/zynq-adrv9361z7035-vrt49-sfp-control-overlay.dts` | submodule pin | `45e1f896e4170f816950f0ccb458571bbcd70885f71b4a4b3cef4171494cd741` | GPL-2.0 |
| `scripts/lablp_hopcap_runtime_load.sh` | `tick:scripts/lablp_tick_runtime_load.sh` | submodule pin | `ccc63ebde994a78f42f14248de55157b01f0deeac56daf272e8e6aa638f64373` | MIT |

Everything else under `src/`, `hdl/`, `scripts/` and `tests/` is original and MIT.
Apart from the row above, Tick RTL, driver and TCL are consumed unmodified from the `third_party/tick` submodule
by the workspace assembler (`scripts/assemble_workspace.py`), which records per-file
hashes of every staged file in its `MANIFEST.json`.
