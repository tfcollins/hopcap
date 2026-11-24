#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build the hopcap composite bitstream from an assembled workspace.
#
#   scripts/build_bitstream.sh --ws build/ws [--port sfp|qsfp] [--vivado-settings PATH] [--evidence DIR] [--jobs N]
#
# The port comes from the workspace (MANIFEST.json physical_port, set by assemble_workspace.py
# --port): sfp -> QSFP_ENABLE=0 (the SFP+ cage), qsfp -> QSFP_ENABLE=1 (QSFP lane 0). --port, if
# given, must agree with it. The profile (MANIFEST profile, assemble_workspace.py --profile)
# selects the netlist checks, and MANIFEST build_env is exported before make (timed-dma:
# VRT_DAC_TX_ENABLE=0). On timed-dma, vrt49's check-sfp-rx-netlist.py is skipped: it asserts
# cpack feeds the RX DMA directly and shares adc_valid_0 with the VRT tap, which the profile
# replaces by design; check_hop_netlist.py --profile timed-dma re-asserts the tap invariants.
# Runs vrt49's CI make invocation (QSFP_ENABLE, 156.25 MHz refclk, the fixed UDP tuple —
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
post_only=0
port=""
while [ $# -gt 0 ]; do
  case "$1" in
    --post-only) post_only=1; shift ;;
    --ws) ws=$(cd "$2" && pwd); shift 2 ;;
    --port) port=$2; shift 2 ;;
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
# a workspace assembled before the port option is a QSFP one
ws_port=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('physical_port', 'qsfp'))" "$ws/MANIFEST.json")
test -z "$port" -o "$port" = "$ws_port" || { echo "workspace was assembled for --port $ws_port, not $port" >&2; exit 2; }
port=$ws_port
case "$port" in sfp) qsfp_enable=0 ;; qsfp) qsfp_enable=1 ;; *) echo "MANIFEST physical_port $port is not sfp or qsfp" >&2; exit 2 ;; esac
profile=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('profile', 'default'))" "$ws/MANIFEST.json")
case "$profile" in default|timed-dma) ;; *) echo "MANIFEST profile $profile is not default or timed-dma" >&2; exit 2 ;; esac
# KEY=VALUE lines from MANIFEST build_env (keys are shell identifiers; values have no spaces)
build_env=$(python3 -c "
import json, re, sys
env = json.load(open(sys.argv[1])).get('build_env', {})
for k, v in env.items():
    assert re.fullmatch(r'[A-Z][A-Z0-9_]*', k) and re.fullmatch(r'[0-9A-Za-z_.x]+', str(v)), (k, v)
    print(f'{k}={v}')
" "$ws/MANIFEST.json")
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
echo "build: ws=$ws port=$port profile=$profile build_env=$(echo $build_env) evidence=$evidence" | tee "$evidence/summary.txt"
if [ "$post_only" = 0 ]; then
(
  cd "$project"
  export QSFP_ENABLE=$qsfp_enable SFP_REFCLK_MHZ=156.25
  export VRT_SOURCE_MAC=0x020000000001 VRT_DEST_MAC=0x020000000002
  export VRT_SOURCE_IP=0x0a000001 VRT_DEST_IP=0x0a000002 VRT_SOURCE_PORT=4991 VRT_DEST_PORT=4991
  for kv in $build_env; do export "${kv?}"; done
  make -j"$jobs"
) 2>&1 | tee "$log"
fi
bit=$(find "$project" -name system_top.bit | head -1)
xsa=$(find "$project" -name system_top.xsa | head -1)
timing=$(find "$project" -name 'system_top_timing_summary_routed.rpt' | head -1)
netlist=$(find "$project" -path '*/synth/system.v' | head -1)
test -n "$bit" -a -n "$xsa" -a -n "$timing" || { echo "bitstream/xsa/timing report not produced" >&2; exit 1; }
# vrt49's checks take the routed timing report and the synthesized BD netlist (as its CI does)
{ echo -n "check-vivado-timing: "; python3 "$here/third_party/vrt49/scripts/check-vivado-timing.py" "$timing" && echo "Timing constraints are met"; } 2>&1 | tee -a "$evidence/summary.txt"
# The RX tap check is port-agnostic (it passed on the QSFP shell too), so a failure fails the build.
if [ "$profile" = timed-dma ]; then
  echo "check-sfp-rx-netlist: SKIPPED (profile timed-dma: cpack -> RX DMA goes through tick_rx_tag_adapter and cpack fifo_wr_en through tick_rx_dma_gate by design; check_hop_netlist --profile timed-dma asserts the VRT tap invariants)" | tee -a "$evidence/summary.txt"
else
{ echo -n "check-sfp-rx-netlist: "; python3 "$here/third_party/vrt49/scripts/check-sfp-rx-netlist.py" "$netlist"; } 2>&1 | tee -a "$evidence/summary.txt"
fi
python3 "$here/scripts/check_hop_netlist.py" "$hdl" --port "$port" --profile "$profile" 2>&1 | tee -a "$evidence/summary.txt"
cp "$bit" "$xsa" "$timing" "$evidence/"
echo "timing_report=$(basename "$timing")" | tee -a "$evidence/summary.txt"
# Routed period of core_clk_a's clock (verify_evidence.py: timed-dma redefines it at 8.138 ns).
echo "tx_dev_clk_period_ns=$(awk '$1=="tx_dev_clk" && $(NF-1) ~ /^[0-9.]+$/ {print $(NF-1); exit}' "$timing")" | tee -a "$evidence/summary.txt"
cp "$ws/MANIFEST.json" "$evidence/"
( cd "$evidence" && sha256sum system_top.bit system_top.xsa MANIFEST.json > SHA256SUMS )
{
  echo "physical_port=$port"
  echo "profile=$profile"
  echo "adi_hdl=${ADI_HDL_SHA:-$(git -C "$hdl" rev-parse HEAD)}"
  echo "corundum=${CORUNDUM_SHA:-$(git -C "$ws/corundum" rev-parse HEAD)}"
  echo "hopcap=${HOPCAP_SOURCE_SHA:-$(git -C "$here" rev-parse HEAD)}"
  echo "bitstream_sha256=$(sha256sum "$evidence/system_top.bit" | cut -d' ' -f1)"
  echo "vivado=$(vivado -version | head -1)"
} >> "$evidence/summary.txt"
echo "build: done -> $evidence"
