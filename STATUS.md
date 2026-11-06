# STATUS — 2026-11-05 — END TO END on tron over SFP+ (default port) with real VRT timestamps: Tick→FHM→hop controller→Context→SFP+→nemo 82599 captures tagged with hop frequency; QSFP lane 0 still selectable

## Tick PPS fallback (2026-11-05, datax P3a) — RTL, BD, checks done; bitstream + hardware PENDING
Owner request: 1PPS may be unconnected, so Tick needs a fallback to an internal clock.
Tick's `pps_in` was a constant (`tick_pps_tieoff`). It is now driven by `tick_pps_select_0`
(`hdl/tick_pps_select/`): a qualified external PPS on PMOD0_D4 (`gpio_i[16]`, vrt49's
existing slice), otherwise Corundum's `ptp_pps_str` (the net vrt49's time base uses).
External qualifies after 2 consecutive periods in 0.9..1.1 s with an edge in the last 1.25 s.
`pps_out` is regenerated, with a 0.9 s minimum edge spacing and an in-window period gate on
every external edge. A switch, a glitch or a late edge therefore never gives Tick a spurious
edge; at most one good edge is lost per switch. Switches happen at the target input's first
quiet sample. An external that stops costs 1-2 Tick seconds, whether it goes low or sticks
high. PMOD0_D4 gets a hopcap-only `PULLTYPE PULLDOWN`.
`sel_ext`/`ext_alive` go to EMIO `gpio_i[20]`/`[21]` (gpio lines 98/99, a PS loopback in every
top, unused by DT); evidence is in `docs/explanation/pps.md`. CDC: `system_tick_pps_cdc.xdc`.
- cocotb `TOP=tick_pps_select`: 12/12 (PR #9 review I1: a late edge and a glitch after a dropped
  edge reached Tick; both are now cases, and both fail on the pre-fix RTL). Eight mutants
  killed (period gate, spacing guard, switch rule, one-period, reset, timeout, min and max
  bound). Verilator `-Wall` clean. Elaboration fails on a bad parameter order.
- `check_hop_netlist` now requires `pps_in` = selector `pps_out` (not a constant),
  `int_pps_async` on Corundum `ptp_pps_str` and `ext_pps_async` on `vrt49_external_pps_slice`.
- Assembler runs on the pinned ADI + Corundum trees (sfp 100 files, qsfp 101), with contract
  checks passing.
- Pending (controller): bitstream build (expect a new `PPS CDC` false path; `check_pps_cdc.tcl`
  covers only vrt49's time base), `check_hop_netlist`, then on tron: `PPS_STATUS.pps_seen`=1 with
  nothing on PMOD0_D4, and `gpioget` lines 98/99 = 0/0.

## Physical port parameter (2026-11-05) — SFP+ default, 4/4 on tron
datax_sdk_sdr#25 gave nemo's XL710 QSFP cage to mini2's 4x10G breakout and cabled tron's SFP+ cage to
nemo's 82599 `enp2s0`. The cage is now a build parameter (`--port sfp|qsfp`, `VRT49_PHYSICAL_PORT`,
receivers in `src/hopcap/link.py`): assembler, bitstream, boot set (Q1A vs Q1B refclk), evidence
checks, preflight, framer MAC, CLI and hw-test all follow it.
- Bitstream (hdl-dev-2, `physical_port=sfp`, hopcap 76cdc95): routed WNS +0.173 ns, WHS +0.010 ns;
  SFP RX-tap and hop netlist checks PASS; bitstream sha256 `3a24e0c3…62c5`, XSA `dc3f4265…1d771a84`.
- Boot set: `verify_evidence boot` OK (Image `18e592b5…`, system.dtb `1acf636a…`).
- hw-test 42072932538 (JTAG boot, `-slow` plan): 4/4. Run 42070293721 on the same artifacts failed
  only `test_hop_capture_plan`: dispatched with the 50 ms default plan, below the 6×RTT staging
  floor (RTT 24 ms that run). hw-test now defaults to the `-slow` plan.

## Previous headline (2026-10-26)
STATUS — 2026-10-26 — END TO END on tron with real VRT timestamps: Tick→FHM→hop controller→Context→QSFP→host captures tagged with hop frequency, sample-exact timestamps

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

## First tron run (2026-10-23) — see docs/reports/2026-10-23-tron-first-run.md
FPGA/radio chain fully working: 5/5 Tick edges at 0-tick skew, Talise readback exactly per the pipeline
model, hop controller 5 events / 5 captures / 0 superseded, framer 3420 packets 0 drops. Host side:
0 datagrams spooled — nemo dropped them in TcpExtIPReversePathFilter (XL710 address-less, rp_filter=2).
**Needs root on nemo:** `sysctl -w net.ipv4.conf.enp4s0f0np0.rp_filter=0` (+ ufw allow 4991/udp on it),
then rerun `scripts/run-tron-hopcap.sh … --plan examples/plans/band-2400-2500-4slot-slow.json`.

## Run 7 (2026-10-23) — end to end
After `rp_filter=0`/ufw on nemo: 5/5 hops received with Context RF == Talise readback == planned,
epochs 1..5, 0-tick skew, FPGA 0 drops; 3/5 captures complete, 2 short by ~90 packets each from XL710
ring `rx_missed_errors` (178). Plan test fails only the ≥95 % completeness gate. Report:
`docs/reports/2026-10-23-tron-first-run.md`. Next: `ethtool -G enp4s0f0np0 rx 8160` on nemo (root).

## VRT timestamps (2026-10-25) — fixed, verified on tron
Every VRT timestamp up to run 7 was 0: vrt49's overlay boots the framer's PPS time base disabled
and hopcap never enabled it. `framer.configure()` now arms it (`ee31887`) and `hopcap.stamps`
checks every in-capture step. Run `tsfix-20261025-2130`: 4/4 hardware tests, 5/5 hops `ok`,
3,410/3,410 steps sample-exact, 0 zero stamps, lock held, 0 NIC loss. tron no longer boots from
SD; JTAG boot added (`147bc66`). Report: `docs/reports/2026-10-25-tron-vrt-timestamps.md`.

## Boot mode follows the rig (2026-10-26)
`--boot auto` (the default in the wrapper, the example launcher and `hw-test.yml`) reads tron's
`boot-mode` place tag (`jtag`|`sd`; tron set to `boot-mode=jtag` on 2026-10-26); with no tag, jtag
is assumed and recorded as such. `boot-strategy` must stay `BootZynqMPJTAG`: adi-lg renders LG_ENV from it and both paths
need that driver. A bad tag stops the run before staging; an explicit `--boot` overrides the tag
with a warning. `board-session.json` records `mode_source`. Unit-tested and resolved read-only
against the live place (`jtag place tron boot-mode=jtag`); no hardware boot since this change, and the SD path is not re-exercised.

## Time-base arm fix and bitstream tbfix (2026-11-04/05) — verified on tron
`test_hop_capture_plan` failed `TimeBaseError: armed seconds ... did not load` (hw-test 41956678880).
A timed probe on tron found two causes. (1) The arm check required the first changed seconds value
to be armed/armed+1, but each sysfs access is a board-side python3 over serial (0.6 s, 2-4 s when it
stalls), so the load edge passed unseen; it now uses vrt49's elapsed-time bound. (2) The pinned vrt49
`eae8aa0` predates vrt49 #62, and one arm in six loaded a torn seconds value; the pin moved to
`5e20550` (main, #53). Both in #5 (`2760d09`). Bitstream from `ed086ef`:
`hdl-dev-2:/mnt/disk2/scratch/hopcap/ws-tbfix-20261105T032218Z`, `verify_evidence.py bitstream` OK,
WNS +0.135 ns, WHS +0.010 ns, bit `d470d24438eb...`, XSA
`6b02535c8e693c546f0edbf94753be0229297e1098de1c2a02febb03393e32ce` (nemo
`~/hopcap-artifacts/bitstream-tbfix/`). Kernel/DTB unchanged (boot-attempt3). hw-test 41972349787:
4/4 passed, time base locked, 0 missed edges, 3,415 packets with 0 zero stamps and 0 step errors.
Use this XSA for JTAG runs; attempt 3 has the torn-seconds RTL.

## Not done
- Hard-fail table lives in `tests/hardware/test_hw_hopcap.py`.
- The 62.8 µs excess on the priming hop's VRT-vs-Tick spacing is unexplained; no PPS edge has
  yet fallen inside a capture on hardware.
- Upstream PRs (vrt49 `VRT_FABRIC_HOP_ENABLE`, Tick `--system-top`); `overlays/patches/` is empty
  because the assembler currently edits the staged tree directly.
- Talise settle time is a knob, not a measured figure. The analysis exists (`hopcap settle`,
  `src/hopcap/settle.py`, plan `examples/plans/settle-measure.json`, how-to
  `docs/how-to/measure-settle.md`; datax_sdk_sdr#9). The hardware measurement waits on the RF
  reference cabling (chip B fixed-LO TX or a signal generator into chip A RX1); none is recorded on tron.
