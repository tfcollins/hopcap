# tron / nemo runbook

Verify every host fact against `~/dev/infra/HOSTS.md` before use.

- **Board**: ADRV9009-ZU11EG on ADRV2CRR-FMC, labgrid place `tron`, coordinator
  `10.0.0.41:20408`. Boots from SD through the USB SD mux (`VRT49_CORUNDUM_BOOT=sd`); JTAG
  (`BootZynqMPJTAG`, `LG_FORCE_LOCAL_XSDB=1` on tron) remains for recovery.
- **Link**: tron QSFP cage lane 0 ↔ nemo XL710 `enp4s0f0np0` (i40e, MAC `64:9d:99:6f:a9:6a`,
  `192.0.2.2/24`) over a Mellanox MC2210128-003 passive DAC. FPGA source `192.0.2.1:4991`. The
  QSFP GT reference is AD9545 Q1B at 156.25 MHz (not HMC7044 channel 10); the hopcap DTB programs it.
- **Lock**: `/tmp/vrt49-corundum-hardware.lock` on nemo is shared with vrt49; both launchers hold it.
- **Never reprogram the PL at runtime** (kills JESD204 / RX DMA). Boot-resident images only.
- **Stock-file caveat** (from the 2026-10-21 QSFP report): the SD staging backs up whatever is on
  `/boot` at session start. Keep an independent stock copy of `BOOT.BIN`/`Image`/`system.dtb`.
- **Do not upgrade nemo's `venv_WORKING` labgrid** — it runs the exporter.
- **NIC**: the XL710 drops a residual 1e-5..3e-4 of datagrams at full rate even when tuned
  (ring 8160, ntuple to a fixed queue, fixed `rx-usecs`). hopcap reports host loss, it does not
  fail on it; FPGA-side drops are hard failures.

Preflight (read-only) from nemo:

```sh
PYTHONPATH=src python3 -m hopcap.lab.preflight
```
