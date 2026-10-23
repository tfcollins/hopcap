# STATUS — 2026-10-23 — offline pipeline complete and reviewed; first Vivado build in flight

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

## Review (2026-10-23)
Whole-branch review found two production-only defects the fakes masked (pipeline model never
advanced on hardware; teardown deadlock while the framer floods) and seven Important items; all
fixed in commit 25f83a4 with red→green tests (125 unit, 19 cocotb). Deferred minors are listed in
the ledger (`.superpowers/sdd/.../progress.md`, lines `Final: minor (deferred)`).

## Bitstream attempt 2 (2026-10-23, hdl-dev-2, Vivado 2025.1) — BUILT, timing met
`evidence/bitstream-attempt2/` (local, gitignored): `verify_evidence.py bitstream` OK. 1 h 23 min.
Routed WNS +0.057 ns, WHS +0.010 ns, 0 failing endpoints; `check_hop_netlist` OK (controller, Tick and
framer instantiated, fabric nets present, `adrv9009_gpio_00_a = tick_fhm_gpio[0]`).
bitstream sha256 `fdfa3e783e750ce632b63ea4e49a7852d21c7f42303243a5ee178625c5aa949e`.
Caveat: built from the pre-review BD fragment (`delete_bd_objs` on the fabric nets, which also
undrives the framer's legacy PTP inputs). Usable for bring-up; not release-grade.

## Boot set from attempt 2 (2026-10-23, hdl-dev-2) — BUILT and VERIFIED
`evidence/boot-attempt1-hdl-dev-2/` (local, gitignored; also `hdl-dev-2:/mnt/disk2/scratch/hopcap/boot-attempt1`):
`verify_evidence.py boot` OK. BOOT.BIN (ATF 2025.1 + ADI u-boot + attempt-2 bitstream via xsct/bootgen),
Image (ADI 2e89089 with Tick + vrt49 drivers in-tree), system.dtb (Q1B QSFP refclk, framer/deframer,
Tick @0x86000000, hop ctrl @0x84AC0000), both overlays. 9/9 fdtget checks. SHA-256s in `SHA256SUMS`.
This trio is what `VRT49_CORUNDUM_BOOT=sd` staging expects; it is ready for a tron bring-up session
(bring-up only: the bitstream is attempt 2, pre-review fragment).

## Bitstream attempt 3 (2026-10-23, fixed fragment) — BUILT, timing met, release candidate
`evidence/bitstream-attempt3/`: `verify_evidence.py bitstream` OK. Routed WNS 0.000 ns (met, zero slack),
WHS +0.008 ns, 0 failing endpoints; `check_hop_netlist` OK. bitstream sha256
`e77d095f6dc4fd6bd9ea7a418ef9cdf002e8cdaba5aaf5fc7a8c58265cdf2a51`. Vivado BD 41-759 warnings dropped
from 5 unconnected framer pins (attempt 2, incl. `ptp_tai_seconds_in`/`ptp_sample_counter_in` — the
review finding) to the 3 the stock vrt49 design already has (`capture_loss_count`, `tx_link_status`,
`tx_throttled_frame_count`). Workspace `hdl-dev-2:/mnt/disk2/scratch/hopcap/ws-20261023T215508Z`.
Note the zero-slack WNS: re-check timing on any RTL change.

## Boot set from attempt 3 — BUILT and VERIFIED (release candidate for the first tron run)
`evidence/boot-attempt3-hdl-dev-2/` and `hdl-dev-2:/mnt/disk2/scratch/hopcap/boot-attempt3`:
`verify_evidence.py boot` OK, 9/9 DTB checks. Serve this directory over HTTP for
`VRT49_CORUNDUM_{BOOT_BIN,KERNEL,DTB}_URL/_SHA256` (hashes in its `SHA256SUMS`).

## In flight
- Nothing. Next step is a lab session on tron (see "Not done").

## Not done
- No hardware run yet. Hard-fail table lives in `tests/hardware/test_hw_hopcap.py`.
- Upstream PRs (vrt49 `VRT_FABRIC_HOP_ENABLE`, Tick `--system-top`); `overlays/patches/` is empty
  because the assembler currently edits the staged tree directly.
- Talise settle time is a knob, not a measured figure.
