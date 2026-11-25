# SPDX-License-Identifier: MIT
"""PasswordSshShell: one askpass-authenticated master, every call over its control socket,
run-local known_hosts, streamed pushes with a sha256 check. The runner is faked."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from hopcap.transport import PasswordSshShell, ShellError


class Runner:
    def __init__(self, fail_master: bool = False):
        self.calls = []
        self.fail_master = fail_master
        self.pushed = b""

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        rc, out = 0, ""
        if "ControlMaster=yes" in argv and self.fail_master:
            rc = 255
        if argv[-1].startswith("cat > "):
            self.pushed = kw["stdin"].read()
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="denied" if rc else "")


def test_master_authenticates_once_through_askpass(tmp_path, monkeypatch):
    monkeypatch.delenv("HOPCAP_TARGET_PASSWORD", raising=False)
    r = Runner()
    sh = PasswordSshShell("root@10.9.9.9", work_dir=tmp_path, runner=r)
    sh.run("uname -r")
    sh.run("true")
    masters = [c for c in r.calls if "ControlMaster=yes" in c[0]]
    assert len(masters) == 1
    argv, kw = masters[0]
    assert kw["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert kw["env"]["HOPCAP_TARGET_PASSWORD"] == "analog"  # Kuiper default
    assert "analog" not in " ".join(argv), "password never on the command line"
    assert (tmp_path / "askpass.sh").read_text() == (
        "#!/bin/sh\nprintf '%s\\n' \"$HOPCAP_TARGET_PASSWORD\"\n"
    )
    assert f"UserKnownHostsFile={tmp_path / 'target_known_hosts'}" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    for argv, _kw in r.calls[1:]:
        assert "ControlMaster=no" in argv and "BatchMode=yes" in argv
        assert f"ControlPath={tmp_path / 'ssh-control.sock'}" in argv
    sh.close()
    assert "-O" in r.calls[-1][0] and "exit" in r.calls[-1][0]


def test_master_failure_is_reported(tmp_path):
    with pytest.raises(ShellError, match="denied"):
        PasswordSshShell("root@x", work_dir=tmp_path, runner=Runner(fail_master=True)).run("true")


def test_push_streams_then_checks_sha256(tmp_path):
    r = Runner()
    sh = PasswordSshShell("root@x", work_dir=tmp_path / "ssh", runner=r)
    f = tmp_path / "system_top.bin"
    f.write_bytes(b"\x00\x01" * 100_000)
    digest = sh.push(f, "/tmp/hopcap/system_top.bin")
    assert digest == hashlib.sha256(f.read_bytes()).hexdigest()
    assert r.pushed == f.read_bytes()
    cmds = [c[0][-1] for c in r.calls if "ControlMaster=no" in c[0]]
    assert cmds[0] == "mkdir -p /tmp/hopcap"
    assert cmds[1] == "cat > /tmp/hopcap/system_top.bin.part"
    assert f"{digest}  /tmp/hopcap/system_top.bin.part" in cmds[2] and "sha256sum -c" in cmds[2]
    assert cmds[2].endswith("mv /tmp/hopcap/system_top.bin.part /tmp/hopcap/system_top.bin")


def test_helper_upload_uses_push_when_the_shell_streams(tmp_path):
    from hopcap.transport import HELPER_SRC, HelperMmio

    r = Runner()
    sh = PasswordSshShell("root@x", work_dir=tmp_path, runner=r)
    HelperMmio(sh, base=0x44A80000).ensure_uploaded()
    assert r.pushed == HELPER_SRC.read_bytes()
    cmds = [c[0][-1] for c in r.calls if "ControlMaster=no" in c[0]]
    assert cmds[-1] == "chmod +x /tmp/hopcap_target.py"
    assert not any(c.startswith("printf %s") for c in cmds)
