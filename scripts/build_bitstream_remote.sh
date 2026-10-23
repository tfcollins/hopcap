#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run scripts/build_bitstream.sh on a Vivado host (default hdl-dev-2.local) by rsyncing the
# assembled workspace + this repo's scripts there, then pulling the evidence directory back.
#
#   scripts/build_bitstream_remote.sh --ws build/ws [--host hdl-dev-2.local] [--remote-root /mnt/disk2/scratch/hopcap]
#                                     [--vivado-settings /opt/Xilinx/2025.1/Vivado/settings64.sh] [--evidence DIR]
#
# Pattern: tick/scripts/build_adi_hdl_tick_remote.sh. Serialise with the lab's HDL builder
# lease (labgrid place hdl-dev-2-hdl-builder) before invoking; this script does not acquire it.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
ws="$here/build/ws"
host=${HOPCAP_REMOTE_BUILD_HOST:-hdl-dev-2.local}
remote_root=${HOPCAP_REMOTE_BUILD_ROOT:-/mnt/disk2/scratch/hopcap}
settings=${HOPCAP_REMOTE_VIVADO_SETTINGS:-/opt/Xilinx/2025.1/Vivado/settings64.sh}
evidence=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ws) ws=$(cd "$2" && pwd); shift 2 ;;
    --host) host=$2; shift 2 ;;
    --remote-root) remote_root=$2; shift 2 ;;
    --vivado-settings) settings=$2; shift 2 ;;
    --evidence) evidence=$2; shift 2 ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
test -f "$ws/MANIFEST.json" || { echo "assemble first: $ws/MANIFEST.json missing" >&2; exit 2; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
rdir="$remote_root/ws-$stamp"
evidence=${evidence:-$here/evidence/bitstream-$stamp}
echo "remote build: $host:$rdir"
ssh "$host" "mkdir -p '$rdir/hopcap/scripts' '$rdir/hopcap/third_party/vrt49/scripts'"
# workspace (ADI tree + corundum, both plain dirs on the far side; .git is not needed to build)
rsync -a --delete --exclude .git "$ws/hdl/" "$host:$rdir/hdl/"
rsync -a --delete --exclude .git "$ws/corundum/" "$host:$rdir/corundum/"
rsync -a "$ws/MANIFEST.json" "$host:$rdir/"
# just enough of this repo for build_bitstream.sh and the checks
rsync -a "$here/scripts/build_bitstream.sh" "$here/scripts/check_hop_netlist.py" "$host:$rdir/hopcap/scripts/"
rsync -a "$here/third_party/vrt49/scripts/check-vivado-timing.py" "$here/third_party/vrt49/scripts/check-sfp-rx-netlist.py" "$host:$rdir/hopcap/third_party/vrt49/scripts/"
# build_bitstream.sh derives $here from its own location and wants a git repo for the hopcap sha; fake it
hopcap_sha=$(git -C "$here" rev-parse HEAD)
ssh "$host" "cd '$rdir/hopcap' && git init -q . 2>/dev/null || true; git -C '$rdir/hopcap' -c user.email=hopcap@build -c user.name=hopcap commit -q --allow-empty -m 'hopcap $hopcap_sha' 2>/dev/null || true; cd '$rdir/hdl' && git init -q . 2>/dev/null; git -C '$rdir/hdl' -c user.email=h@b -c user.name=h commit -q --allow-empty -m 'adi $(git -C "$ws/hdl" rev-parse HEAD)' 2>/dev/null || true; cd '$rdir/corundum' && git init -q . 2>/dev/null; git -C '$rdir/corundum' -c user.email=h@b -c user.name=h commit -q --allow-empty -m 'corundum $(git -C "$ws/corundum" rev-parse HEAD)' 2>/dev/null || true"
# One Vivado build per host at a time (same lock file convention as picard's HDL builds).
ssh "$host" "cd '$rdir' && flock /tmp/hdl-builder.lock env HOPCAP_SOURCE_SHA='$hopcap_sha' bash hopcap/scripts/build_bitstream.sh --ws '$rdir' --vivado-settings '$settings' --evidence '$rdir/evidence'"
mkdir -p "$evidence"
rsync -a "$host:$rdir/evidence/" "$evidence/"
echo "hopcap_source_sha=$hopcap_sha" >> "$evidence/summary.txt"
echo "remote build: evidence -> $evidence"
