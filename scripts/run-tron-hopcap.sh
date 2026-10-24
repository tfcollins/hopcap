#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Operator wrapper for a hopcap hardware run from nemo.
#
#   scripts/run-tron-hopcap.sh --artifacts ~/hopcap-artifacts/boot-attempt3 --url http://10.0.0.71:8749/boot-attempt3 \
#                              --out ~/hopcap-artifacts/run-<stamp> [--plan examples/plans/band-2400-2500-4slot.json] \
#                              [--nm-qsfp vrt49-qsfp --nm-sfp vrt49-sfp] [--python ~/hopcap-venv/bin/python]
#
# Derives the VRT49_CORUNDUM_*_URL/_SHA256 variables from the artifact directory's SHA256SUMS,
# brings the QSFP receiver profile up (and the SFP one down, they share 192.0.2.2) for the
# session and restores both afterwards, then runs examples/hardware/zu11eg_hopcap.py --run,
# which reserves tron via adi-lg, boots the exact artifacts, runs tests/hardware and verifies
# the evidence bundle.  Recipe mirrors vrt49's 2026-10-21 QSFP qualification run.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
artifacts=""; url=""; out=""; plan="examples/plans/band-2400-2500-4slot.json"
nm_qsfp=""; nm_sfp=""; py=python3; wait_s=900
while [ $# -gt 0 ]; do
  case "$1" in
    --artifacts) artifacts=$2; shift 2 ;;
    --url) url=$2; shift 2 ;;
    --out) out=$2; shift 2 ;;
    --plan) plan=$2; shift 2 ;;
    --nm-qsfp) nm_qsfp=$2; shift 2 ;;
    --nm-sfp) nm_sfp=$2; shift 2 ;;
    --python) py=$2; shift 2 ;;
    --wait) wait_s=$2; shift 2 ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
test -n "$artifacts" -a -n "$url" -a -n "$out" || { echo "--artifacts, --url and --out are required" >&2; exit 2; }
sums="$artifacts/SHA256SUMS"
test -f "$sums" || { echo "no $sums" >&2; exit 2; }
sha() { awk -v n="$1" '$2==n {print $1}' "$sums"; }
for f in BOOT.BIN Image system.dtb; do
  test -n "$(sha $f)" || { echo "$f missing from $sums" >&2; exit 2; }
  curl -sf -o /dev/null "$url/$f" || { echo "$url/$f not reachable" >&2; exit 2; }
done
export VRT49_CORUNDUM_BOOT=sd VRT49_PHYSICAL_PORT=qsfp
export VRT49_CORUNDUM_BOOT_BIN_URL="$url/BOOT.BIN" VRT49_CORUNDUM_BOOT_BIN_SHA256="$(sha BOOT.BIN)"
export VRT49_CORUNDUM_KERNEL_URL="$url/Image" VRT49_CORUNDUM_KERNEL_SHA256="$(sha Image)"
export VRT49_CORUNDUM_DTB_URL="$url/system.dtb" VRT49_CORUNDUM_DTB_SHA256="$(sha system.dtb)"
export HOPCAP_PLAN="$plan" HOPCAP_OUT="$out" HOPCAP_RX_INTERFACE="${HOPCAP_RX_INTERFACE:-enp4s0f0np0}"
export HOPCAP_COMMIT="$(git -C "$here" rev-parse HEAD)"
export LG_COORDINATOR=10.0.0.41:20408 ADI_LG_COORDINATOR=10.0.0.41:20408
mkdir -p "$out"
restore_nm() {
  if [ -n "$nm_qsfp" ]; then nmcli con down "$nm_qsfp" >/dev/null 2>&1 || true; fi
  if [ -n "$nm_sfp" ]; then nmcli con up "$nm_sfp" >/dev/null 2>&1 || true; fi
}
if [ -n "$nm_qsfp" ]; then
  trap restore_nm EXIT
  [ -n "$nm_sfp" ] && nmcli con down "$nm_sfp" >/dev/null 2>&1 || true
  nmcli con up "$nm_qsfp"
  ip -4 -o addr show dev "$HOPCAP_RX_INTERFACE" | tee "$out/receiver-nic.txt"
fi
cd "$here"
env | grep -E "^(VRT49_|HOPCAP_|LG_)" | sed -E 's/(SHA256=)(.{12}).*/\1\2…/' | tee "$out/run-env.txt"
"$py" examples/hardware/zu11eg_hopcap.py --run --junit "$out/junit-hopcap-tron.xml" --wait "$wait_s" 2>&1 | tee "$out/launcher.log"
