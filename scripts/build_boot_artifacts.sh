#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the hopcap boot set for tron: Image + system.dtb (+ overlays) and BOOT.BIN.
#
#   scripts/build_boot_artifacts.sh --xsa evidence/<bitstream>/system_top.xsa --out evidence/boot-<stamp>
#         [--port sfp|qsfp] [--profile default|timed-dma] [--work build/boot] [--skip-boot-bin] [--jobs N]
#
# --port picks the cage the bitstream drives (build_bitstream.sh --port must match): sfp (default,
# the SFP+ cage on AD9545 OUT1_A/Q1A) or qsfp (QSFP lane 0 on OUT1_B/Q1B). The DTB programs that
# output, as vrt49's kernel-control job does for the same PHYSICAL_PORT.
#
# --profile must match the bitstream's workspace (assemble_workspace.py --profile):
#   default    vrt49 control overlay (framer + deframer) + hopcap overlay; the deframer owns the
#              TX DMA (patch_adrv9009_tx_dma_owner.py)
#   timed-dma  libdatax P3b image, SFP only. vrt49 is built with VRT_DAC_TX_ENABLE=0, so there is
#              no vrt49_dac_tx at 0x84AB0000: the adapted control overlay without the deframer
#              node, the stock TX DMA left on axi-adrv9009-tx-hpc (no DMA-owner patch), and the
#              timed-dma hopcap overlay whose Tick node carries adi,stream-gate-map.
#   The output overlay names (vrt49-framer-control.dtbo, hopcap.dtbo) are the same for both;
#   boot-provenance.json records the profile and the source DTS of each.
#
# With --xsa, system_top.bit and psu_init.tcl (the JTAG boot inputs, scripts/extract_jtag_inputs.py)
# and a copy of the XSA are written next to the kernel and DTB, so the set boots over JTAG without
# a BOOT.BIN (--skip-boot-bin) as well as from SD.
#
# Codifies the pinned vrt49 kernel-control job and BOOT.BIN step (hdl-corundum.yml at the
# submodule pin) plus the hopcap additions:
#   * Tick driver + UAPI header built IN-TREE (obj-y) in drivers/misc, exactly as vrt49's
#     ADRV9361 workflow does, so there is no module/vermagic step;
#   * kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts applied after vrt49's control overlay;
#   * fdtget assertions for the cage refclk, framer, Tick and hop-controller nodes.
# Kernel/DTB need aarch64-linux-gnu-gcc, dtc, fdtoverlay, fdtget; BOOT.BIN additionally
# needs xsct + bootgen (Vivado/Vitis 2025.1 host) — pass --skip-boot-bin elsewhere.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
vrt49="$here/third_party/vrt49"
tick="$here/third_party/tick"
xsa=""
out=""
work="$here/build/boot"
skip_boot_bin=0
port=sfp
profile=default
jobs=$(nproc)
while [ $# -gt 0 ]; do
  case "$1" in
    --xsa) xsa=$(readlink -f "$2"); shift 2 ;;
    --out) out=$2; shift 2 ;;
    --work) work=$2; shift 2 ;;
    --port) port=$2; shift 2 ;;
    --profile) profile=$2; shift 2 ;;
    --skip-boot-bin) skip_boot_bin=1; shift ;;
    --jobs) jobs=$2; shift 2 ;;
    -h|--help) sed -n 2,32p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
test -n "$out" || { echo "--out required" >&2; exit 2; }
case "$port" in
  sfp)  clock_output=q1a; gt_output=6; clock_source=AD9545_OUT1_A_Q1A ;;
  qsfp) clock_output=q1b; gt_output=8; clock_source=AD9545_OUT1_B_Q1B ;;
  *) echo "--port must be sfp or qsfp" >&2; exit 2 ;;
esac
case "$profile" in
  default)
    control_dts="$vrt49/kernel/iio-vrt49-buffer/zynqmp-adrv9009zu11eg-vrt49-control-overlay.dts"
    hopcap_dts="$here/kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts" ;;
  timed-dma)
    [ "$port" = sfp ] || { echo "--profile timed-dma supports --port sfp only" >&2; exit 2; }
    control_dts="$here/kernel/dts/zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts"
    hopcap_dts="$here/kernel/dts/zynqmp-adrv9009zu11eg-hopcap-timed-dma-overlay.dts" ;;
  *) echo "--profile must be default or timed-dma" >&2; exit 2 ;;
esac
# libdatax tick_ops.c datax_tick_parse_gate_map reads this exact list (fdtget prints it space-separated)
TDMA_GATE_MAP="rx-dma tx-dma rx-mode tx-mode fhm-b tx-start-fullmask-only"
for tool in aarch64-linux-gnu-gcc dtc fdtoverlay fdtget flex bison bc make git python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done
pins() { python3 -c "import json,sys; d=json.load(open('$here/pins.json')); print(eval('d'+sys.argv[1]))" "$1"; }
linux_repo=$(pins "['linux']['repository']"); linux_sha=$(pins "['linux']['revision']")
atf_repo=$(pins "['arm_trusted_firmware']['repository']"); atf_sha=$(pins "['arm_trusted_firmware']['revision']")
uboot_url=$(pins "['u_boot']['url']"); uboot_sha=$(pins "['u_boot']['sha256']")
corundum_sha=$(pins "['corundum']['revision']")
hopcap_sha=$(git -C "$here" rev-parse HEAD)
mkdir -p "$work" "$out"

fetch_pinned() { # repo url sha dest
  if [ ! -d "$3/.git" ]; then git init -q "$3"; git -C "$3" remote add origin "$1"; fi
  if [ "$(git -C "$3" rev-parse HEAD 2>/dev/null || true)" != "$2" ]; then
    GIT_TERMINAL_PROMPT=0 git -C "$3" fetch --depth=1 origin "$2"
    git -C "$3" checkout -q --detach FETCH_HEAD
  fi
  test "$(git -C "$3" rev-parse HEAD)" = "$2"
}

# ---------------------------------------------------------------- kernel + DTB
BOARD_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva
FSM_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-jesd204-fsm
PORT_BASE_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-vrt49-$port-base
PORT_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-vrt49-$port
linux="$work/linux"
fetch_pinned "$linux_repo" "$linux_sha" "$linux"
git -C "$linux" checkout -q -- . && git -C "$linux" clean -qfd -- arch/arm64/boot/dts/xilinx drivers/iio/buffer drivers/misc include/uapi/linux || true
python3 "$vrt49/kernel/iio-vrt49-buffer/patch_adrv9009_fsm_start.py" "$linux"
install -m 0644 "$vrt49/kernel/iio-vrt49-buffer/iio-vrt49-buffer.c" "$linux/drivers/iio/buffer/iio-vrt49-buffer.c"
printf '\n# VRT49 control-only framer/UDP driver\nobj-y += iio-vrt49-buffer.o\n' >> "$linux/drivers/iio/buffer/Makefile"
# hopcap: Tick scheduler driver in-tree (vrt49 ADRV9361 precedent)
install -m 0644 "$tick/kernel/axi_timed_command_scheduler.c" "$linux/drivers/misc/"
install -m 0644 "$tick/kernel/uapi/linux/tick_timed_command.h" "$linux/include/uapi/linux/"
printf '\n# hopcap: Tick timed-command scheduler\nobj-y += axi_timed_command_scheduler.o\n' >> "$linux/drivers/misc/Makefile"
dts_dir="$linux/arch/arm64/boot/dts/xilinx"
cp "$dts_dir/$BOARD_DTS.dts" "$dts_dir/$PORT_BASE_DTS.dts"
python3 "$vrt49/kernel/iio-vrt49-buffer/patch_ad9545_sfp_clock.py" --output "$clock_output" "$dts_dir/$PORT_BASE_DTS.dts"
cp "$dts_dir/$FSM_DTS.dts" "$dts_dir/$PORT_DTS.dts"
python3 - "$dts_dir/$PORT_DTS.dts" "$BOARD_DTS" "$PORT_BASE_DTS" <<'PY'
import sys
from pathlib import Path
path, board, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
old, new = f'#include "{board}.dts"', f'#include "{base}.dts"'
if text.count(old) != 1:
    raise SystemExit(f"expected exactly one FSM board include {old!r}")
path.write_text(text.replace(old, new, 1))
PY
if [ "$profile" = default ]; then
  # the vrt49 deframer node takes the TX DMA from the stock DAC-TPL node
  python3 "$vrt49/kernel/iio-vrt49-buffer/patch_adrv9009_tx_dma_owner.py" "$dts_dir/$PORT_DTS.dts"
fi
corundum_drv="$work/corundum-driver"
fetch_pinned https://github.com/ucsdsysnet/corundum.git "$corundum_sha" "$corundum_drv"
python3 "$vrt49/kernel/sfp-network/integrate.py" "$linux" "$corundum_drv" "$dts_dir/$PORT_DTS.dts"
grep -q "$PORT_DTS.dtb" "$dts_dir/Makefile" || printf '\ndtb-$(CONFIG_ARCH_ZYNQMP) += %s.dtb\n' "$PORT_DTS" >> "$dts_dir/Makefile"
make -C "$linux" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- adi_zynqmp_defconfig
make -C "$linux" -j"$jobs" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- Image
make -C "$linux" -j"$jobs" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- DTC_FLAGS=-@ "xilinx/$PORT_DTS.dtb"
test -s "$linux/arch/arm64/boot/Image"
test -s "$linux/drivers/misc/axi_timed_command_scheduler.o"
test -s "$linux/drivers/net/ethernet/corundum/mqnic_main.o"
dtc -@ -I dts -O dtb -o "$out/vrt49-framer-control.dtbo" "$control_dts"
dtc -@ -I dts -O dtb -o "$out/hopcap.dtbo" "$hopcap_dts"
fdtoverlay -i "$dts_dir/$PORT_DTS.dtb" -o "$out/system.dtb" "$out/vrt49-framer-control.dtbo" "$out/hopcap.dtbo"
cp "$linux/arch/arm64/boot/Image" "$out/Image"
# fallback module from the same tree (vermagic matches by construction); stock-image experiments only
make -C "$linux" -j"$jobs" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- M="$tick/kernel" modules >/dev/null 2>&1 && cp "$tick/kernel/axi_timed_command_scheduler.ko" "$out/" || echo "note: fallback .ko not built"

# ---------------------------------------------------------------- DTB assertions
dtb="$out/system.dtb"
chk() { # name command...
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "$name=true"; else echo "$name=false"; fi
}
{
  chk ${clock_output}_output_clk sh -c "fdtget -t i '$dtb' /axi/i2c@ff030000/i2c-mux@70/i2c@1/ad9545@4a/output-clk@$gt_output reg | grep -qx $gt_output"
  chk ad9545_pll1 sh -c "fdtget -t i '$dtb' /axi/i2c@ff030000/i2c-mux@70/i2c@1/ad9545@4a/pll-clk@1 reg | grep -qx 1"
  chk refclk_rates sh -c "fdtget -t i '$dtb' /axi/i2c@ff030000/i2c-mux@70/i2c@1/ad9545@4a assigned-clock-rates | grep -qx '10000 1413120000 1875000000 30720000 156250000'"
  chk framer_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/iio_vrt49_framer@84a80000 compatible | grep -q adi,axi-vrt49-framer"
  if [ "$profile" = timed-dma ]; then
    chk deframer_absent sh -c "! fdtget -l '$dtb' /fpga-axi@0/iio_vrt49_deframer@84ab0000"
    chk tx_dma_stock sh -c "fdtget -t s '$dtb' /fpga-axi@0/axi-adrv9009-tx-hpc@84a04000 dma-names | grep -qx tx && fdtget -t x '$dtb' /fpga-axi@0/axi-adrv9009-tx-hpc@84a04000 dmas"
    chk tick_gate_map sh -c "fdtget -t s '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 adi,stream-gate-map | grep -qx '$TDMA_GATE_MAP'"
  else
    chk deframer_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/iio_vrt49_deframer@84ab0000 compatible | grep -qx adi,axi-vrt49-dac-tx-1.00.a"
    chk tick_gate_map_absent sh -c "! fdtget -t s '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 adi,stream-gate-map"
  fi
  chk corundum_node sh -c "fdtget -t s '$dtb' /axi/ethernet@a0000000 compatible | grep -qx corundum,mqnic"
  chk rx_filter_node sh -c "fdtget -t s '$dtb' /axi/rx-filter@84aa0000 compatible | grep -qx adi,vrt49-rx-filter-1.0"
  chk tick_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 compatible | grep -qx adi,axi-timed-command-scheduler-1.0"
  chk tick_hz sh -c "fdtget -t i '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 adi,time-tick-hz | grep -qx 100000000"
  chk hop_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/fhm-hop-controller@84ac0000 compatible | grep -qx nous,vrt49-fhm-hop-controller-1.0"
} > "$out/dtb-checks.txt"
cat "$out/dtb-checks.txt"
grep -q "=false" "$out/dtb-checks.txt" && { echo "DTB checks failed" >&2; exit 1; }

# ---------------------------------------------------------------- JTAG inputs
if [ -n "$xsa" ]; then
  test -f "$xsa" || { echo "--xsa $xsa not found" >&2; exit 2; }
  python3 "$here/scripts/extract_jtag_inputs.py" "$xsa" "$out"
  cp "$xsa" "$out/system_top.xsa"
fi

# ---------------------------------------------------------------- BOOT.BIN
if [ "$skip_boot_bin" = 0 ]; then
  test -n "$xsa" -a -f "$xsa" || { echo "--xsa required for BOOT.BIN (or --skip-boot-bin)" >&2; exit 2; }
  for tool in xsct bootgen; do command -v "$tool" >/dev/null || { echo "missing $tool (Vivado/Vitis 2025.1); use --skip-boot-bin" >&2; exit 1; }; done
  mkdir -p "$work/platform-inputs"
  [ -f "$work/platform-inputs/u-boot.elf" ] || curl --fail --location --retry 3 --output "$work/platform-inputs/u-boot.elf" "$uboot_url"
  echo "$uboot_sha  $work/platform-inputs/u-boot.elf" | sha256sum -c -
  fetch_pinned "$atf_repo" "$atf_sha" "$work/platform-inputs/arm-trusted-firmware"
  make -C "$work/platform-inputs/arm-trusted-firmware" CROSS_COMPILE=aarch64-linux-gnu- PLAT=zynqmp RESET_TO_BL31=1 ZYNQMP_CONSOLE=cadence0
  bl31="$work/platform-inputs/arm-trusted-firmware/build/zynqmp/release/bl31/bl31.elf"
  python3 "$vrt49/scripts/build-xilinx-boot-bin.py" --arch zynqmp --platform "adrv9009-zu11eg-$port" --xsa "$xsa" \
    --uboot "$work/platform-inputs/u-boot.elf" --uboot-sha256 "$uboot_sha" --atf "$bl31" --output "$work/platform-boot" --source-sha "$hopcap_sha"
  cp "$work/platform-boot/BOOT.BIN" "$out/BOOT.BIN"
fi

# ---------------------------------------------------------------- provenance
python3 - "$out" "$linux_sha" "$hopcap_sha" "$xsa" "$port" "$clock_source" "$profile" "$control_dts" "$hopcap_dts" "$here" <<'PY'
import json, sys, hashlib
from pathlib import Path
out, linux_sha, hopcap_sha, xsa = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
port, clock_source, profile = sys.argv[5], sys.argv[6], sys.argv[7]
control_dts, hopcap_dts, here = Path(sys.argv[8]), Path(sys.argv[9]), Path(sys.argv[10])
checks = dict(line.split("=") for line in (out / "dtb-checks.txt").read_text().split())
prov = {
    "platform": f"adrv9009-zu11eg-{port}", "physical_port": port, "linux_source_sha": linux_sha, "hopcap_source_sha": hopcap_sha,
    "xsa": xsa, "xsa_sha256": hashlib.sha256(Path(xsa).read_bytes()).hexdigest() if xsa and Path(xsa).exists() else None,
    f"{port}_clock_hz": 156250000, f"{port}_clock_source": clock_source,
    "profile": profile,
    "tick_driver": "in-tree obj-y", "overlays": ["vrt49-framer-control.dtbo", "hopcap.dtbo"],
    "overlay_sources": {
        "vrt49-framer-control.dtbo": str(control_dts.relative_to(here)),
        "hopcap.dtbo": str(hopcap_dts.relative_to(here)),
    },
    "boot_modes": (["sd"] if (out / "BOOT.BIN").exists() else [])
    + (["jtag"] if (out / "system_top.bit").exists() and (out / "psu_init.tcl").exists() else []),
    "dtb_checks": {k: v == "true" for k, v in checks.items()},
}
(out / "boot-provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
PY
( cd "$out" && sha256sum $(ls BOOT.BIN system_top.bit psu_init.tcl system_top.xsa Image system.dtb hopcap.dtbo vrt49-framer-control.dtbo axi_timed_command_scheduler.ko 2>/dev/null) > SHA256SUMS )
echo "boot artifacts -> $out"
