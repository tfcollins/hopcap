# tron / nemo runbook

Verify every host fact against `~/dev/infra/HOSTS.md` before use.

- **Board**: ADRV9009-ZU11EG on ADRV2CRR-FMC, labgrid place `tron`, coordinator
  `10.0.0.41:20408`. Two boot paths through `scripts/run-tron-hopcap.sh`, chosen by `--boot`:
  - `--boot auto` (default) follows the place's `boot-mode` tag (`jtag`|`sd`), the lab's record of
    how the boot-mode switches are set; with no `boot-mode` tag, jtag is assumed and the run says
    so. Any other value, or a place labgrid can't describe, stops the run before anything is staged;
    pass `--boot` explicitly then. An explicit `--boot` wins over the tag, with a warning when they
    disagree. The chosen mode and why (`mode_source`) land in `board-session.json`. Whoever moves
    the switches changes the tag (`labgrid-client -p tron set-tags boot-mode=sd`).
    `PYTHONPATH=src python3 -m hopcap.lab.preflight --boot-mode` prints what auto would pick.
  - `boot-strategy` is not the strap record. adi-lg renders `LG_ENV` from it, and hopcap drives
    both paths through `BootZynqMPJTAG`, so it stays `BootZynqMPJTAG` whichever way tron is
    strapped (it already was while tron was SD-strapped, 2026-10-20..25). Any other value stops the run.
  - `jtag` (tron's setup since 2026-10-25): the PL bitstream and
    `psu_init.tcl` come from the XSA of the build under test (`--xsa FILE`, default
    `system_top.xsa` in the artifact set) and load over JTAG (`BootZynqMPJTAG`,
    `LG_FORCE_LOCAL_XSDB=1` on tron); the kernel/DTB are staged beside the stock files as
    `Image.vrt49.test`/`system.vrt49.dtb`. This is vrt49's Corundum CI path. tron moved to it when
    an SD-strapped power-on printed nothing for 300 s while vrt49's JTAG boot of the same board passed.
  - `sd` (an SD-strapped board, `boot-mode=sd`): the SD card's `BOOT.BIN`/`Image`/`system.dtb`
    are replaced and the board power-cycles into them, then the stock trio is restored. The files
    are staged from the running Linux, so the SD mux is not needed.
- **Link** (the artifacts and the run must name the same port, `--port` / `VRT49_PHYSICAL_PORT`):
  - `sfp` (default, cabled since 2026-11-05): tron SFP+ cage ↔ nemo 82599 `enp2s0` (ixgbe, MAC
    `6c:92:bf:5b:38:b9`, `192.0.2.2/24`, NM profile `vrt49-sfp`) over an SFP-H10GB-CU3M passive DAC.
    GT reference AD9545 Q1A at 156.25 MHz. First hopcap 4/4 on it: hw-test run 42072932538.
  - `qsfp`: tron QSFP cage lane 0 ↔ nemo XL710 `enp4s0f0np0` (i40e, MAC `64:9d:99:6f:a9:6a`, NM
    profile `vrt49-qsfp`) over a Mellanox MC2210128-003 passive DAC. GT reference AD9545 Q1B at
    156.25 MHz (not HMC7044 channel 10). The XL710 cage now carries mini2's 4x10G breakout
    (datax_sdk_sdr#25), so this needs the DAC moved back first.
  - FPGA source `192.0.2.1:4991` either way; the hopcap DTB programs the port's refclk.
- **Lock**: `/tmp/vrt49-corundum-hardware.lock` on nemo is shared with vrt49; both launchers hold it.
- **Never reprogram the PL at runtime** (kills JESD204 / RX DMA). Boot-resident images only.
- **Stock-file caveat** (from the 2026-10-21 QSFP report): the SD staging backs up whatever is on
  `/boot` at session start. Keep an independent stock copy of `BOOT.BIN`/`Image`/`system.dtb`.
  JTAG boot never touches the stock trio; it leaves `Image.vrt49.test`/`system.vrt49.dtb` on
  `/boot`, which the next vrt49 or hopcap run re-stages with hash checks.
- **VRT time**: every run arms the framer's PPS time base before it enables the stream (vrt49's
  overlay boots it disabled, and disabled means every timestamp is 0). Seconds are host UTC at arm
  (±1 s); the sub-second phase is Corundum's free-running PTP PPS, not GNSS.
- **Do not upgrade nemo's `venv_WORKING` labgrid** — it runs the exporter.
- **NIC** (`qsfp`): the XL710 drops a residual 1e-5..3e-4 of datagrams at full rate even when tuned
  (ring 8160, ntuple to a fixed queue, fixed `rx-usecs`). hopcap reports host loss, it does not
  fail on it; FPGA-side drops are hard failures.

Preflight (read-only) from nemo:

```sh
PYTHONPATH=src python3 -m hopcap.lab.preflight
```

A run from nemo (the launcher takes the shared lock itself; do not wrap it in `flock`):

```sh
scripts/run-tron-hopcap.sh --artifacts ~/hopcap-artifacts/boot-attempt3 \
  --url http://10.0.0.71:8749/boot-attempt3 --out ~/hopcap-artifacts/run-$(date +%Y%m%d-%H%M) \
  --plan examples/plans/band-2400-2500-4slot-slow.json --python ~/hopcap-venv/bin/python \
  --xsa ~/hopcap-artifacts/bitstream-tbfix/system_top.xsa
```

`bitstream-tbfix` (built from `ed086ef`, XSA SHA-256 `6b02535c...`) carries vrt49's time-base
seconds CDC fix; `bitstream-attempt3` predates it and can load a torn seconds value when armed.
