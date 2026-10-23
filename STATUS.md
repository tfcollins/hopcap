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

## In flight
- Bitstream attempt 3 from the fixed fragment (`disconnect_bd_net`), hdl-dev-2.
- Boot set on hdl-dev-2 from attempt 2's XSA (`/mnt/disk2/scratch/hopcap/boot-attempt1`): kernel with
  Tick in-tree, Q1B QSFP DTB + overlays, BOOT.BIN via xsct/bootgen.

## Not done
- No hardware run yet. Hard-fail table lives in `tests/hardware/test_hw_hopcap.py`.
- Upstream PRs (vrt49 `VRT_FABRIC_HOP_ENABLE`, Tick `--system-top`); `overlays/patches/` is empty
  because the assembler currently edits the staged tree directly.
- Talise settle time is a knob, not a measured figure.
