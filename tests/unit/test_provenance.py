# SPDX-License-Identifier: MIT
"""PROVENANCE.md hashes must match the upstream files at the recorded revisions."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROW = re.compile(
    r"^\| `(?P<local>[^`]+)` \| `(?P<repo>vrt49|tick):(?P<upstream>[^`]+)` \| (?P<rev>[^|]+) \| "
    r"`(?P<sha>[0-9a-f]{64})` \|"
)


def rows():
    out = []
    for line in (ROOT / "PROVENANCE.md").read_text().splitlines():
        m = ROW.match(line)
        if m:
            out.append(m.groupdict())
    return out


def upstream_bytes(upstream: str, rev: str, repo: str = "vrt49") -> bytes:
    sub = ROOT / "third_party" / repo
    if "submodule pin" in rev:
        return (sub / upstream).read_bytes()
    sha = re.search(r"[0-9a-f]{40}", rev).group(0)
    return subprocess.check_output(["git", "-C", str(sub), "show", f"{sha}:{upstream}"])


def test_provenance_table_has_rows():
    assert len(rows()) >= 2
    assert any(r["repo"] == "tick" for r in rows())


def test_every_adapted_file_exists_locally_and_upstream_hash_matches():
    for r in rows():
        assert (ROOT / r["local"]).exists(), r["local"]
        got = hashlib.sha256(upstream_bytes(r["upstream"], r["rev"], r["repo"])).hexdigest()
        assert got == r["sha"], f"{r['upstream']} changed upstream; re-review {r['local']}"


def test_adapted_files_declare_their_upstream_license():
    licences = {
        "hdl/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v": "BSD-2-Clause",
        "src/hopcap/native/udp_spool.c": "LGPL-2.1-or-later",
        "kernel/dts/zynqmp-adrv9009zu11eg-vrt49-control-timed-dma-overlay.dts": "GPL-2.0",
        "kernel/dts/zynq-adrv9361z7035-hopcap-overlay.dts": "GPL-2.0",
        "scripts/lablp_hopcap_runtime_load.sh": "MIT",
    }
    for local, lic in licences.items():
        head = "\n".join((ROOT / local).read_text().splitlines()[:2])  # after a shebang
        assert f"SPDX-License-Identifier: {lic}" in head, local
