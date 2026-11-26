# SPDX-License-Identifier: MIT
"""hw-test-adrv9361.yml: the lablp hop HIL must really run, and never for a fork's code."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WF = (ROOT / ".github/workflows/hw-test-adrv9361.yml").read_text()


def test_pull_requests_need_the_label_and_a_branch_of_this_repo():
    assert "contains(github.event.pull_request.labels.*.name, 'hw-test')" in WF
    assert "github.event.pull_request.head.repo.full_name == github.repository" in WF


def test_runs_on_nemo_and_serialises_lablp():
    assert "runs-on: [self-hosted, hw-nemo]" in WF
    assert "group: hopcap-lablp" in WF and "cancel-in-progress: false" in WF


def test_builds_the_image_it_tests():
    assert "--profile adrv9361" in WF
    assert "flock /tmp/hdl-builder.lock scripts/build_bitstream.sh" in WF
    assert "scripts/build_lablp_modules.sh" in WF
    assert "2e8908932dfd9faf40ec9220a508aa1ce02699a8" in WF


def test_build_trees_stay_off_the_root_disk():
    assert "BUILD_BASE: /mnt/onetb/scratch/hopcap-ci" in WF
    assert "build/ws-a9" not in WF


def test_runs_the_multi_boot_hil_and_its_proof_tests():
    assert "scripts/run_lablp_hopcap_hil.sh" in WF
    assert "BOOTS: ${{ inputs.boots || '3' }}" in WF
    hil = (ROOT / "scripts/run_lablp_hopcap_hil.sh").read_text()
    assert "tests/hardware/test_hw_adrv9361.py" in hil
    assert "HOPCAP_LABLP_MODE=both" in hil
    assert "exit $status" in hil


def test_latency_window_is_pinned():
    t = json.loads((ROOT / "tests/hardware/adrv9361_hop_timing.json").read_text())
    lo, hi = t["window_ns"]
    assert 0 < lo < t["mean_ns"] < hi
    assert t["min_tone_margin_db"] >= 10
