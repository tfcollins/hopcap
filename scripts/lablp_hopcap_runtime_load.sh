#!/bin/sh
# SPDX-License-Identifier: MIT
# Adapted from tick scripts/lablp_tick_runtime_load.sh (MIT, validated on lablp): same unbind,
# FPGA-manager load and rebind order; adds the AD9517 OUT4 SFP reference clock, vrt49's framer
# module and the hopcap overlay.
#
# Runs ON the ADRV9361-Z7035 (stock Kuiper 2023_R2_P1 boot, labgrid place `lablp`) as root.
# Loads the hopcap bitstream through the FPGA manager without touching the SD card, then adds
# the Tick scheduler, the vrt49 framer and the HOP2 node. A power cycle restores stock Kuiper.
#
#   lablp_hopcap_runtime_load.sh <system_top.bin> <hopcap.dtbo> \
#       <axi_timed_command_scheduler.ko> <iio-vrt49-buffer.ko>
#
# <system_top.bin> is the byte-swapped bitstream from scripts/build_bitstream.sh (the Zynq-7000
# FPGA manager rejects a raw .bit). <hopcap.dtbo> is kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts
# compiled with `dtc -@`. Order matters:
#   0. AD9517 OUT4 = 125 MHz and enabled BEFORE the PL load: the SFP GTX's reference (stock
#      Kuiper leaves OUT4 disabled at 500 MHz); PCS resetdone only asserts at 125 MHz;
#   1. unbind the AD9361 data path (DDS/ADC cores, the PHY, their DMAs) so no driver touches the
#      PL while it is reprogrammed;
#   2. program the PL;
#   3. rebind in reverse order: the PHY probe re-initialises the AD9361 and the ADC core re-tunes
#      the LVDS interface against the new bitstream;
#   4. apply the overlay, insert the two modules (dependencies through modprobe first), restart
#      iiod so its inventory includes the new devices.
set -eu

bin=$1
dtbo=$2
tick_ko=$3
vrt49_ko=$4

[ -s "$bin" ] && [ -s "$dtbo" ] && [ -s "$tick_ko" ] && [ -s "$vrt49_ko" ] || {
    echo "usage: $0 <system_top.bin> <hopcap.dtbo> <axi_timed_command_scheduler.ko> <iio-vrt49-buffer.ko>" >&2
    exit 2
}

mgr=/sys/class/fpga_manager/fpga0
[ "$(cat "$mgr/name")" = "Xilinx Zynq FPGA Manager" ] || {
    echo "error: no Zynq FPGA manager at $mgr" >&2
    exit 3
}
uname -r | sed 's/^/kernel_release=/'

iio_dir() {
    for f in /sys/bus/iio/devices/iio:device*/name; do
        [ "$(cat "$f")" = "$1" ] && { dirname "$f"; return 0; }
    done
    return 1
}

# 0. SFP reference clock.
clk=$(iio_dir ad9517-3) || { echo "error: no ad9517-3 IIO device" >&2; exit 3; }
echo 125000000 > "$clk/out_altvoltage4_frequency"
echo 1 > "$clk/out_altvoltage4_raw"
f4=$(cat "$clk/out_altvoltage4_frequency")
r4=$(cat "$clk/out_altvoltage4_raw")
echo "ad9517_out4_frequency=$f4"
echo "ad9517_out4_raw=$r4"
[ "$f4" = 125000000 ] && [ "$r4" = 1 ] || {
    echo "error: AD9517 OUT4 reads $f4 Hz raw $r4, need 125000000 and 1" >&2
    exit 3
}

DMAC=/sys/bus/platform/drivers/dma-axi-dmac
DDS=/sys/bus/platform/drivers/cf_axi_dds
ADC=/sys/bus/platform/drivers/cf_axi_adc
PHY=/sys/bus/spi/drivers/ad9361
DDS_DEV=79024000.cf-ad9361-dds-core-lpc
ADC_DEV=79020000.cf-ad9361-lpc
PHY_DEV=spi0.0
DMA_DEVS="7c400000.dma 7c420000.dma"
OVL=/sys/kernel/config/device-tree/overlays/hopcap

unbind() { [ -e "$1/$2" ] && echo "$2" > "$1/unbind" || true; }
bind() { [ -e "$1/$2" ] || echo "$2" > "$1/bind"; }
modname() { basename "$1" .ko | tr - _; }

# Leave nothing of a previous run behind.
systemctl stop iiod 2>/dev/null || true
for ko in "$tick_ko" "$vrt49_ko"; do rmmod "$(modname "$ko")" 2>/dev/null || true; done
rmdir "$OVL" 2>/dev/null || true

# 1. unbind
unbind "$DDS" "$DDS_DEV"
unbind "$ADC" "$ADC_DEV"
unbind "$PHY" "$PHY_DEV"
for d in $DMA_DEVS; do unbind "$DMAC" "$d"; done

# 2. program the PL
mkdir -p /lib/firmware
cp "$bin" /lib/firmware/hopcap-z7035.bin
echo 0 > "$mgr/flags"
echo hopcap-z7035.bin > "$mgr/firmware"
state=$(cat "$mgr/state")
echo "fpga_manager_state=$state"
[ "$state" = "operating" ] || { echo "error: FPGA manager state $state" >&2; exit 3; }
echo "bin_sha256=$(sha256sum /lib/firmware/hopcap-z7035.bin | cut -d' ' -f1)"

# 3. rebind
for d in $DMA_DEVS; do bind "$DMAC" "$d"; done
bind "$PHY" "$PHY_DEV"
bind "$ADC" "$ADC_DEV"
bind "$DDS" "$DDS_DEV"

# 4. overlay, modules, iiod
mkdir -p "$OVL"
cat "$dtbo" > "$OVL/dtbo"
ovl_status=$(cat "$OVL/status" 2>/dev/null || echo unknown)
echo "overlay_status=$ovl_status"
[ "$ovl_status" = applied ] || { echo "error: overlay status $ovl_status" >&2; exit 3; }
for ko in "$vrt49_ko" "$tick_ko"; do
    deps=$(modinfo -F depends "$ko" 2>/dev/null | tr ',' ' ')
    for dep in $deps; do modprobe "$dep"; done
    insmod "$ko"
    echo "module_loaded=$(modname "$ko") depends=${deps:-none}"
done
systemctl reset-failed iiod 2>/dev/null || true
systemctl start iiod

for f in /sys/bus/iio/devices/iio:device*/name; do echo "iio $(cat "$f")"; done
for need in ad9361-phy cf-ad9361-lpc cf-ad9361-dds-core-lpc axi-timed-command-scheduler vrt49_framer; do
    iio_dir "$need" >/dev/null || { echo "error: IIO device $need missing after the load" >&2; exit 3; }
done
dmesg | grep -E "axi-timed-command-scheduler|vrt49|ad9361 spi0.0|cf_axi_adc|cf_axi_dds" | tail -20
echo "hopcap_runtime_load=ok"
