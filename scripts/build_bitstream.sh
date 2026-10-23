#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the hopcap composite bitstream from an assembled workspace.
#
#   scripts/build_bitstream.sh --ws build/ws [--vivado-settings PATH] [--evidence DIR] [--jobs N]
#
# Runs vrt49's CI make invocation (QSFP_ENABLE=1, 156.25 MHz refclk, the fixed UDP tuple —
# the tuple is runtime-programmable anyway), then the timing and netlist checks, and copies
# the artifacts + SHA256SUMS + summary.txt into the evidence directory.  Vivado 2025.1 is
# required (pins.json "vivado").  Remote use: run this script on the build host over ssh
# with the workspace rsync'd there (see docs/how-to/build.md).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
ws="$here/build/ws"
settings=""
evidence=""
jobs=$(nproc)
while [ $# -gt 0 ]; do
  case "$1" in
    --ws) ws=$(cd "$2" && pwd); shift 2 ;;
    --vivado-settings) settings=$2; shift 2 ;;
    --evidence) evidence=$2; shift 2 ;;
    --jobs) jobs=$2; shift 2 ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
hdl="$ws/hdl"
project="$hdl/projects/adrv9009zu11eg_vrt49_corundum"
test -f "$ws/MANIFEST.json" || { echo "no MANIFEST.json in $ws; run scripts/assemble_workspace.py first" >&2; exit 2; }
test -d "$ws/corundum" || { echo "no corundum checkout next to $hdl" >&2; exit 2; }
if [ -z "$settings" ]; then
  for cand in /tools/Xilinx/2025.1/Vivado/settings64.sh /opt/Xilinx/2025.1/Vivado/settings64.sh; do
    [ -f "$cand" ] && settings=$cand && break
  done
fi
test -n "$settings" -a -f "$settings" || { echo "Vivado 2025.1 settings64.sh not found; pass --vivado-settings" >&2; exit 2; }
# Vivado's settings scripts reference unset variables (PYTHONPATH); relax -u around them.
set +u
# shellcheck disable=SC1090
source "$settings"
set -u
vivado -version | head -1
evidence=${evidence:-$here/evidence/bitstream-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$evidence"
log="$evidence/make.log"
echo "build: ws=$ws evidence=$evidence" | tee "$evidence/summary.txt"
(
  cd "$project"
  export QSFP_ENABLE=1 SFP_REFCLK_MHZ=156.25
  export VRT_SOURCE_MAC=0x020000000001 VRT_DEST_MAC=0x020000000002
  export VRT_SOURCE_IP=0x0a000001 VRT_DEST_IP=0x0a000002 VRT_SOURCE_PORT=4991 VRT_DEST_PORT=4991
  make -j"$jobs"
) 2>&1 | tee "$log"
python3 "$here/third_party/vrt49/scripts/check-vivado-timing.py" "$project" 2>&1 | tee -a "$evidence/summary.txt"
python3 "$here/third_party/vrt49/scripts/check-sfp-rx-netlist.py" "$hdl" 2>&1 | tee -a "$evidence/summary.txt" || true
python3 "$here/scripts/check_hop_netlist.py" "$hdl" 2>&1 | tee -a "$evidence/summary.txt"
bit=$(find "$project" -name system_top.bit | head -1)
xsa=$(find "$project" -name system_top.xsa | head -1)
test -n "$bit" -a -n "$xsa" || { echo "bitstream/xsa not produced" >&2; exit 1; }
cp "$bit" "$xsa" "$evidence/"
find "$project" -name '*timing_summary_routed.rpt' -exec cp {} "$evidence/" \; 2>/dev/null || true
cp "$ws/MANIFEST.json" "$evidence/"
( cd "$evidence" && sha256sum system_top.bit system_top.xsa MANIFEST.json > SHA256SUMS )
{
  echo "adi_hdl=$(git -C "$hdl" rev-parse HEAD)"
  echo "corundum=$(git -C "$ws/corundum" rev-parse HEAD)"
  echo "hopcap=$(git -C "$here" rev-parse HEAD)"
  echo "bitstream_sha256=$(sha256sum "$evidence/system_top.bit" | cut -d' ' -f1)"
  echo "vivado=$(vivado -version | head -1)"
} >> "$evidence/summary.txt"
echo "build: done -> $evidence"
