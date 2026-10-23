# STATUS — 2026-10-23 — offline pipeline complete; first Vivado build in flight

Plan: `docs/superpowers/specs/2026-10-23-hopcap-design.md`. Ledger of rulings in the commit history.

## Done (tested)
- `vrt49_fhm_hop_ctrl` RTL: 15 cocotb unit tests + 3 chain tests against the real vrt49 framer
  (Context before data, byte-exact vs libvrt49, capture window, epoch per hop); Verilator lint clean;
  mutants (off-by-one window, uncounted superseded, gate-before-accept) all caught.
- Host: plan compiler (FHM one-behind pipeline), Tick descriptors (replayed through tick.hdl_model),
  hop-controller programmer (regmap JSON is the single source), VITA 49.2 codec, C spooler + Python
  receiver, association (never guesses), SigMF/manifest export, run orchestrator, CLI. 114 unit tests.
- Build: assembler verified on the real pinned ADI tree (e57851ffc; Tick's `patch_system_top()` applies
  to `system_top_corundum.v`; both vrt49 contract checks pass; 97 files hashed). Kernel + DTB + overlays
  built on picard: 9/9 fdtget checks (Q1B refclk, framer, Tick @0x86000000, hop ctrl @0x84AC0000).
- Lab: tron boot/restore, nemo preflight, hardware suite (collects without a board), adi-lg launcher.
- Docs (Sphinx -W), CI workflows (actionlint clean), DataX example page, HOSTS.md QSFP link.

## In flight
- Vivado 2025.1 build on hdl-dev-2 (`/mnt/disk2/scratch/hopcap/ws-*`, `scripts/build_bitstream_remote.sh`).
  BOOT.BIN needs its XSA (`build_boot_artifacts.sh --xsa ...` on a Vivado host for xsct/bootgen).

## Not done
- No hardware run yet. Hard-fail table lives in `tests/hardware/test_hw_hopcap.py`.
- Upstream PRs (vrt49 `VRT_FABRIC_HOP_ENABLE`, Tick `--system-top`); `overlays/patches/` is empty
  because the assembler currently edits the staged tree directly.
- Talise settle time is a knob, not a measured figure.
