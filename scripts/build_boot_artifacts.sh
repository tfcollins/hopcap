#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the hopcap boot set for tron (QSFP): Image + system.dtb (+ overlays) and BOOT.BIN.
#
#   scripts/build_boot_artifacts.sh --xsa evidence/<bitstream>/system_top.xsa --out evidence/boot-<stamp>
#         [--work build/boot] [--skip-boot-bin] [--jobs N]
#
# Codifies the pinned vrt49 kernel-control job and BOOT.BIN step (hdl-corundum.yml at the
# submodule pin) plus the hopcap additions:
#   * Tick driver + UAPI header built IN-TREE (obj-y) in drivers/misc, exactly as vrt49's
#     ADRV9361 workflow does, so there is no module/vermagic step;
#   * kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts applied after vrt49's control overlay;
#   * fdtget assertions for the Q1B refclk, framer, Tick and hop-controller nodes.
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
jobs=$(nproc)
while [ $# -gt 0 ]; do
  case "$1" in
    --xsa) xsa=$(readlink -f "$2"); shift 2 ;;
    --out) out=$2; shift 2 ;;
    --work) work=$2; shift 2 ;;
    --skip-boot-bin) skip_boot_bin=1; shift ;;
    --jobs) jobs=$2; shift 2 ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
test -n "$out" || { echo "--out required" >&2; exit 2; }
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
QSFP_BASE_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-vrt49-qsfp-base
QSFP_DTS=zynqmp-adrv9009-zu11eg-reva-adrv2crr-fmc-reva-vrt49-qsfp
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
cp "$dts_dir/$BOARD_DTS.dts" "$dts_dir/$QSFP_BASE_DTS.dts"
python3 "$vrt49/kernel/iio-vrt49-buffer/patch_ad9545_sfp_clock.py" --output q1b "$dts_dir/$QSFP_BASE_DTS.dts"
cp "$dts_dir/$FSM_DTS.dts" "$dts_dir/$QSFP_DTS.dts"
python3 - "$dts_dir/$QSFP_DTS.dts" "$BOARD_DTS" "$QSFP_BASE_DTS" <<'PY'
import sys
from pathlib import Path
path, board, base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
old, new = f'#include "{board}.dts"', f'#include "{base}.dts"'
if text.count(old) != 1:
    raise SystemExit(f"expected exactly one FSM board include {old!r}")
path.write_text(text.replace(old, new, 1))
PY
python3 "$vrt49/kernel/iio-vrt49-buffer/patch_adrv9009_tx_dma_owner.py" "$dts_dir/$QSFP_DTS.dts"
corundum_drv="$work/corundum-driver"
fetch_pinned https://github.com/ucsdsysnet/corundum.git "$corundum_sha" "$corundum_drv"
python3 "$vrt49/kernel/sfp-network/integrate.py" "$linux" "$corundum_drv" "$dts_dir/$QSFP_DTS.dts"
grep -q "$QSFP_DTS.dtb" "$dts_dir/Makefile" || printf '\ndtb-$(CONFIG_ARCH_ZYNQMP) += %s.dtb\n' "$QSFP_DTS" >> "$dts_dir/Makefile"
make -C "$linux" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- adi_zynqmp_defconfig
make -C "$linux" -j"$jobs" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- Image
make -C "$linux" -j"$jobs" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- DTC_FLAGS=-@ "xilinx/$QSFP_DTS.dtb"
test -s "$linux/arch/arm64/boot/Image"
test -s "$linux/drivers/misc/axi_timed_command_scheduler.o"
test -s "$linux/drivers/net/ethernet/corundum/mqnic_main.o"
dtc -@ -I dts -O dtb -o "$out/vrt49-framer-control.dtbo" "$vrt49/kernel/iio-vrt49-buffer/zynqmp-adrv9009zu11eg-vrt49-control-overlay.dts"
dtc -@ -I dts -O dtb -o "$out/hopcap.dtbo" "$here/kernel/dts/zynqmp-adrv9009zu11eg-hopcap-overlay.dts"
fdtoverlay -i "$dts_dir/$QSFP_DTS.dtb" -o "$out/system.dtb" "$out/vrt49-framer-control.dtbo" "$out/hopcap.dtbo"
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
  chk q1b_output_clk sh -c "fdtget -t i '$dtb' /axi/i2c@ff030000/i2c-mux@70/i2c@1/ad9545@4a/output-clk@8 reg | grep -qx 8"
  chk q1b_rates sh -c "fdtget -t i '$dtb' /axi/i2c@ff030000/i2c-mux@70/i2c@1/ad9545@4a assigned-clock-rates | grep -qx '10000 1413120000 1875000000 30720000 156250000'"
  chk framer_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/iio_vrt49_framer@84a80000 compatible | grep -q adi,axi-vrt49-framer"
  chk deframer_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/iio_vrt49_deframer@84ab0000 compatible | grep -qx adi,axi-vrt49-dac-tx-1.00.a"
  chk corundum_node sh -c "fdtget -t s '$dtb' /axi/ethernet@a0000000 compatible | grep -qx corundum,mqnic"
  chk rx_filter_node sh -c "fdtget -t s '$dtb' /axi/rx-filter@84aa0000 compatible | grep -qx adi,vrt49-rx-filter-1.0"
  chk tick_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 compatible | grep -qx adi,axi-timed-command-scheduler-1.0"
  chk tick_hz sh -c "fdtget -t i '$dtb' /fpga-axi@0/timed-command-scheduler@86000000 adi,time-tick-hz | grep -qx 100000000"
  chk hop_node sh -c "fdtget -t s '$dtb' /fpga-axi@0/fhm-hop-controller@84ac0000 compatible | grep -qx nous,vrt49-fhm-hop-controller-1.0"
} > "$out/dtb-checks.txt"
cat "$out/dtb-checks.txt"
grep -q "=false" "$out/dtb-checks.txt" && { echo "DTB checks failed" >&2; exit 1; }

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
  python3 "$vrt49/scripts/build-xilinx-boot-bin.py" --arch zynqmp --platform adrv9009-zu11eg-qsfp --xsa "$xsa" \
    --uboot "$work/platform-inputs/u-boot.elf" --uboot-sha256 "$uboot_sha" --atf "$bl31" --output "$work/platform-boot" --source-sha "$hopcap_sha"
  cp "$work/platform-boot/BOOT.BIN" "$out/BOOT.BIN"
fi

# ---------------------------------------------------------------- provenance
python3 - "$out" "$linux_sha" "$hopcap_sha" "$xsa" <<'PY'
import json, sys, hashlib
from pathlib import Path
out, linux_sha, hopcap_sha, xsa = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
checks = dict(line.split("=") for line in (out / "dtb-checks.txt").read_text().split())
prov = {
    "platform": "adrv9009-zu11eg-qsfp", "linux_source_sha": linux_sha, "hopcap_source_sha": hopcap_sha,
    "xsa": xsa, "xsa_sha256": hashlib.sha256(Path(xsa).read_bytes()).hexdigest() if xsa and Path(xsa).exists() else None,
    "qsfp_clock_hz": 156250000, "qsfp_clock_source": "AD9545_OUT1_B_Q1B",
    "tick_driver": "in-tree obj-y", "overlays": ["vrt49-framer-control.dtbo", "hopcap.dtbo"],
    "dtb_checks": {k: v == "true" for k, v in checks.items()},
}
(out / "boot-provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
PY
( cd "$out" && sha256sum $(ls BOOT.BIN Image system.dtb hopcap.dtbo vrt49-framer-control.dtbo axi_timed_command_scheduler.ko 2>/dev/null) > SHA256SUMS )
echo "boot artifacts -> $out"
