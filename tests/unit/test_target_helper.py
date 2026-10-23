# SPDX-License-Identifier: MIT
"""hopcap_target.py runs on the board with only the stdlib (Kuiper python3). It is exercised
here against a file-backed memory image instead of /dev/mem."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "src/hopcap/target/hopcap_target.py"


def run_helper(*args, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, "-I", str(HELPER), *args], input=stdin, capture_output=True, text=True
    )


def test_helper_is_stdlib_only_and_python39_compatible():
    src = HELPER.read_text()
    assert "import numpy" not in src and "import iio" not in src
    assert "match " not in src  # 3.10 syntax
    assert "from __future__ import annotations" in src


def test_mmio_batch_reads_and_writes_file_backed_memory(tmp_path):
    mem = tmp_path / "mem.bin"
    mem.write_bytes(bytes(0x1000))
    base = 0x84AC0000
    ops = {
        "ops": [
            {"op": "w", "off": 0x0C, "val": 0x7D0},
            {"op": "w", "off": 0x44, "val": 0xDEADBEEF},
            {"op": "r", "off": 0x0C},
            {"op": "r", "off": 0x44},
            {"op": "r", "off": 0x00},
        ]
    }
    r = run_helper(
        "mmio", "--base", hex(base), "--mem", str(mem), "--map-offset", "0", stdin=json.dumps(ops)
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["results"] == [None, None, 0x7D0, 0xDEADBEEF, 0]
    raw = mem.read_bytes()
    assert int.from_bytes(raw[0x0C:0x10], "little") == 0x7D0
    assert int.from_bytes(raw[0x44:0x48], "little") == 0xDEADBEEF


def test_mmio_rejects_unaligned_or_out_of_range(tmp_path):
    mem = tmp_path / "mem.bin"
    mem.write_bytes(bytes(0x1000))
    r = run_helper(
        "mmio",
        "--base",
        "0x0",
        "--mem",
        str(mem),
        "--map-offset",
        "0",
        stdin=json.dumps({"ops": [{"op": "r", "off": 0x1002}]}),
    )
    assert r.returncode != 0 and "aligned" in r.stderr
    r = run_helper(
        "mmio",
        "--base",
        "0x0",
        "--mem",
        str(mem),
        "--map-offset",
        "0",
        "--span",
        "0x100",
        stdin=json.dumps({"ops": [{"op": "r", "off": 0x200}]}),
    )
    assert r.returncode != 0 and "range" in r.stderr


def test_sysfs_batch_reads_and_writes_files(tmp_path):
    dev = tmp_path / "iio:device3"
    dev.mkdir()
    (dev / "v49_enable").write_text("0\n")
    (dev / "v49_tx_dropped_packets").write_text("17\n")
    ops = {
        "ops": [
            {"op": "w", "attr": "v49_enable", "val": "1"},
            {"op": "r", "attr": "v49_enable"},
            {"op": "r", "attr": "v49_tx_dropped_packets"},
        ]
    }
    r = run_helper("sysfs", "--dir", str(dev), stdin=json.dumps(ops))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["results"] == [None, "1", "17"]
    assert (dev / "v49_enable").read_text() == "1"


def test_sysfs_find_device_by_name(tmp_path):
    sysfs = tmp_path / "iio" / "devices"
    for i, name in enumerate(["adrv9009-phy", "axi-vrt49-framer", "axi-timed-command-scheduler"]):
        d = sysfs / f"iio:device{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
    r = run_helper("find", "--sysfs", str(sysfs), "--name", "axi-vrt49-framer")
    assert r.returncode == 0
    assert json.loads(r.stdout)["path"] == str(sysfs / "iio:device1")
    r = run_helper("find", "--sysfs", str(sysfs), "--name", "nope")
    assert r.returncode == 1
