#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# ADRV9361-Z7035 hop HIL on labgrid place lablp: N independent boots, each one gated and one
# gapless FastLock run (examples/hardware/adrv9361_hopcap.py, which books lablp with
# `adi-lg request --power-down`), then tests/hardware/test_hw_adrv9361.py over the evidence.
# Runs on nemo (the SFP is cabled to nemo enp7s0). The SD card is never written.
set -euo pipefail

usage() {
  echo "usage: $0 --bin <system_top.bin> --tick-module <.ko> --vrt49-module <.ko> --evidence-dir <dir> [--boots N] [--plan <json>]" >&2
  exit 2
}
bin="" tick_ko="" vrt49_ko="" evidence="" boots=3 plan=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin) bin=$2; shift 2 ;;
    --tick-module) tick_ko=$2; shift 2 ;;
    --vrt49-module) vrt49_ko=$2; shift 2 ;;
    --evidence-dir) evidence=$2; shift 2 ;;
    --boots) boots=$2; shift 2 ;;
    --plan) plan=$2; shift 2 ;;
    *) usage ;;
  esac
done
[[ -s "$bin" && -s "$tick_ko" && -s "$vrt49_ko" && -n "$evidence" ]] || usage
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$evidence"
export HOPCAP_LABLP_BIN=$(realpath "$bin") HOPCAP_TICK_MODULE=$(realpath "$tick_ko")
export HOPCAP_VRT49_MODULE=$(realpath "$vrt49_ko") HOPCAP_LABLP_MODE=both
[[ -n "$plan" ]] && export HOPCAP_PLAN=$(realpath "$plan")
export LG_COORDINATOR=${LG_COORDINATOR:-10.0.0.41:20408}

sessions=()
status=0
for i in $(seq 1 "$boots"); do
  out="$evidence/boot$i"
  echo "=== lablp boot $i/$boots -> $out"
  if ! HOPCAP_OUT="$out" python3 "$repo/examples/hardware/adrv9361_hopcap.py" --run \
      2>&1 | grep -viE 'token|bearer|eyJ[A-Za-z0-9_-]{8,}' | tee "$evidence/boot$i.log"; then
    status=1
  fi
  sessions+=("$out")
done

joined=$(IFS=,; echo "${sessions[*]}")
HOPCAP_ADRV9361_EVIDENCE="$joined" python3 -m pytest -v -p no:cacheprovider \
  "$repo/tests/hardware/test_hw_adrv9361.py" --junitxml="$evidence/hw-adrv9361.xml" \
  2>&1 | tee "$evidence/hw-adrv9361-pytest.log" || status=1
python3 - "$evidence" "${sessions[@]}" <<'PY' | tee "$evidence/summary.json"
import json, sys
from pathlib import Path
out = {"boots": []}
for s in sys.argv[2:]:
    p = Path(s) / "gapless" / "manifest.json"
    if p.is_file():
        m = json.loads(p.read_text())
        out["boots"].append({"session": s, "disposition": m["disposition"],
                             "latency": m["hop_timing_summary"]})
print(json.dumps(out, indent=1))
PY
exit $status
