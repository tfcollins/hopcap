# SPDX-License-Identifier: LGPL-2.1-or-later
# Adapted from vrt49 tests/integration/conftest.py (corundum_shell fixture, LGPL-2.1-or-later).
"""Boot tron with exact artifacts: SD-strapped (BOOT.BIN+Image+system.dtb replace the stock
trio, backed up once and restored at the end) or JTAG (PL + U-Boot over JTAG, test kernel/DTB
staged beside stock).  The labgrid-dependent parts import lazily; the command builders and the
environment contract are pure and unit-tested.

Environment contract (same names as vrt49 so one operator shell serves both repos):
  LG_ENV, VRT49_CORUNDUM_BOOT=sd|jtag,
  VRT49_CORUNDUM_{BOOT_BIN,KERNEL,DTB}_URL / _SHA256   (BOOT_BIN only for sd)
  VRT49_CORUNDUM_BITSTREAM_REMOTE, VRT49_CORUNDUM_PSU_INIT_REMOTE   (jtag)
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass

CORUNDUM_KERNEL_NAME = "Image.vrt49.test"
CORUNDUM_DTB_NAME = "system.vrt49.dtb"
CORUNDUM_SD_BOOT_COMMAND = (
    "setenv partid 1; run loadbootenv && run importbootenv; setenv uenvcmd; "
    f"load mmc $sdbootdev:$partid $fdt_addr {CORUNDUM_DTB_NAME}; "
    f"load mmc $sdbootdev:$partid $kernel_addr {CORUNDUM_KERNEL_NAME}; "
    "booti $kernel_addr - $fdt_addr"
)
CORUNDUM_SD_STOCK_DIR = "/boot/vrt49-stock"
CORUNDUM_SD_FILES = ("BOOT.BIN", "Image", "system.dtb")


class BootConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BootArtifacts:
    env_path: str
    mode: str
    kernel: tuple[str, str] | None
    dtb: tuple[str, str] | None
    boot_bin: tuple[str, str] | None
    bitstream_remote: str | None
    psu_init_remote: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BootArtifacts:
        env_path = env.get("LG_ENV")
        if not env_path:
            raise BootConfigError("LG_ENV is required")
        mode = env.get("VRT49_CORUNDUM_BOOT", "sd")
        if mode not in ("sd", "jtag"):
            raise BootConfigError("VRT49_CORUNDUM_BOOT must be sd or jtag")

        def pair(prefix: str) -> tuple[str, str] | None:
            url, sha = env.get(f"{prefix}_URL"), env.get(f"{prefix}_SHA256")
            if bool(url) != bool(sha):
                raise BootConfigError(f"{prefix}_URL and {prefix}_SHA256 must be supplied together")
            return (url, sha) if url else None

        kernel, dtb, boot_bin = (
            pair("VRT49_CORUNDUM_KERNEL"),
            pair("VRT49_CORUNDUM_DTB"),
            pair("VRT49_CORUNDUM_BOOT_BIN"),
        )
        present = [p is not None for p in (kernel, dtb, *((boot_bin,) if mode == "sd" else ()))]
        if any(present) and not all(present):
            raise BootConfigError(
                "kernel/DTB (and SD BOOT.BIN) URLs and SHA-256 values must be supplied together"
            )
        bitstream, psu = (
            env.get("VRT49_CORUNDUM_BITSTREAM_REMOTE"),
            env.get("VRT49_CORUNDUM_PSU_INIT_REMOTE"),
        )
        if mode == "jtag" and (not bitstream or not psu):
            raise BootConfigError(
                "jtag boot needs VRT49_CORUNDUM_BITSTREAM_REMOTE and VRT49_CORUNDUM_PSU_INIT_REMOTE"
            )
        return cls(
            env_path=env_path,
            mode=mode,
            kernel=kernel,
            dtb=dtb,
            boot_bin=boot_bin,
            bitstream_remote=bitstream,
            psu_init_remote=psu,
        )

    @property
    def exact(self) -> bool:
        return self.kernel is not None

    def staged_names(self) -> dict[str, tuple[str, str]]:
        if not self.exact:
            return {}
        if self.mode == "sd":
            return {"BOOT.BIN": self.boot_bin, "Image": self.kernel, "system.dtb": self.dtb}
        return {CORUNDUM_KERNEL_NAME: self.kernel, CORUNDUM_DTB_NAME: self.dtb}


def stage_command(url: str, sha256: str, name: str) -> str:
    destination = f"/boot/{name}"
    staging = destination + ".new"
    return (
        "set -e; wget -q -O " + shlex.quote(staging) + " " + shlex.quote(url) + "; "
        "echo " + shlex.quote(f"{sha256}  {staging}") + " | sha256sum -c -; "
        "mv " + shlex.quote(staging) + " " + shlex.quote(destination) + "; sync"
    )


def sd_backup_command() -> str:
    stock = shlex.quote(CORUNDUM_SD_STOCK_DIR)
    files = " ".join(shlex.quote(f"/boot/{n}") for n in CORUNDUM_SD_FILES)
    return (
        f"set -e; if [ ! -d {stock} ]; then rm -rf {stock}.new; mkdir {stock}.new; "
        f"cp {files} {stock}.new/; sync; mv {stock}.new {stock}; sync; fi"
    )


def sd_restore_command() -> str:
    stock = shlex.quote(CORUNDUM_SD_STOCK_DIR)
    files = " ".join(shlex.quote(f"{CORUNDUM_SD_STOCK_DIR}/{n}") for n in CORUNDUM_SD_FILES)
    return f"set -e; cp {files} /boot/; sync; rm -rf {stock}; sync"


def staged_sums_command(names) -> str:
    return "sha256sum " + " ".join(shlex.quote(f"/boot/{n}") for n in names)


def parse_shell_value(lines: list[str]) -> str:
    return "\n".join(ln for ln in lines if ln.strip()).strip()


# ---------------------------------------------------------------------------
# labgrid-driven flow (imports lazily; only runs inside an acquired reservation)
# ---------------------------------------------------------------------------


class ShellAdapter:
    """hopcap.transport.Shell over a labgrid ADIShellDriver."""

    def __init__(self, shell):
        self.shell = shell

    def run(self, cmd: str, *, timeout_s: float = 15.0) -> str:
        out, _err, rc = self.shell.run(cmd, timeout=timeout_s)
        if rc != 0:
            raise RuntimeError(f"target command failed ({rc}): {cmd[:80]}")
        return parse_shell_value(list(out)) + "\n"


class TronSession:
    def __init__(self, artifacts: BootArtifacts, *, log=print):
        self.a = artifacts
        self.log = log
        self.target = None
        self.strategy = None
        self.shell = None
        self._sd_staged = False
        self.stock_sums_before: str | None = None
        self.staged_sums: str | None = None
        self.restored_sums: str | None = None
        self.restore_error: str | None = None

    def _new_target(self, *, exact_files: bool = False):
        import adi_lg_plugins.strategies.bootzynqmpjtag  # noqa: F401
        from labgrid import Environment

        env = Environment(self.a.env_path)
        target = env.get_target("main")
        strategy = target.get_driver("BootZynqMPJTAG")
        if self.a.bitstream_remote:
            strategy.bitstream_path = self.a.bitstream_remote
        if self.a.psu_init_remote:
            strategy.psu_init_tcl = self.a.psu_init_remote
        if exact_files and self.a.mode == "jtag":
            strategy.sd_boot_command = CORUNDUM_SD_BOOT_COMMAND
        return target, strategy

    def _power_off(self):
        try:
            self.strategy.transition("powered_off")
        except Exception:
            self.target.activate(self.strategy.power)
            self.strategy.power.off()

    def _boot(self):
        if self.a.mode == "sd":
            from adi_lg_plugins.strategies.bootzynqmpjtag import Status

            self._power_off()
            shell = self.strategy._activate_shell_bypass()
            # Powering on outside transition() leaves the strategy's status at powered_off, and its
            # transition("powered_off") is then a no-op: record the outlet first so a failed boot
            # still powers the board off.
            self.strategy.status = Status.powered_on
            self.strategy.power.on()
            shell.console.expect(
                self.strategy.kuiper_kernel_marker, timeout=self.strategy.kuiper_boot_timeout
            )
            shell.console.expect(
                self.strategy.kuiper_shell_marker, timeout=self.strategy.kuiper_boot_timeout
            )
            shell.prompt = self.strategy.kuiper_shell_marker
            shell._check_prompt()
            shell._inject_run()
            self.strategy.status = Status.kuiper_shell
        else:
            self.strategy.transition("shell")
            shell = self.target.get_driver("ADIShellDriver")
        shell.run("dmesg -n 1")
        return shell

    def _value(self, cmd: str, timeout: float = 30) -> str:
        out, _err, rc = self.shell.run(cmd, timeout=timeout)
        if rc != 0:
            raise RuntimeError(f"target command failed ({rc}): {cmd[:80]}")
        return parse_shell_value(list(out))

    def boot_exact(self) -> ShellAdapter:
        self.target, self.strategy = self._new_target()
        self.shell = self._boot()
        if self.a.exact:
            self.stock_sums_before = self._value(
                "sha256sum /boot/BOOT.BIN /boot/Image /boot/system.dtb"
            )
            self.log(f"stock /boot before staging:\n{self.stock_sums_before}")
            if self.a.mode == "sd":
                self._value(sd_backup_command(), timeout=120)
                self._sd_staged = True
            for name, (url, sha) in self.a.staged_names().items():
                self._value(stage_command(url, sha, name), timeout=180)
            self._power_off()
            self.target.cleanup()
            self.target, self.strategy = self._new_target(exact_files=True)
            self.shell = self._boot()
            live = self._value(staged_sums_command(self.a.staged_names()))
            self.staged_sums = live
            for name, (_url, sha) in self.a.staged_names().items():
                if sha not in live:
                    raise RuntimeError(
                        f"{name} on /boot does not carry the expected sha256 after reboot"
                    )
        return ShellAdapter(self.shell)

    def restore_and_power_off(self):
        try:
            if self._sd_staged and self.shell is not None:
                self.log("restoring stock /boot trio from " + CORUNDUM_SD_STOCK_DIR)
                try:
                    self._value(sd_restore_command(), timeout=120)
                    self.restored_sums = self._value(
                        "sha256sum /boot/BOOT.BIN /boot/Image /boot/system.dtb"
                    )
                    self.log("restored: " + self.restored_sums)
                except Exception as exc:
                    self.restore_error = str(exc)
                    self.log(f"WARNING: restore failed: {exc}")
            elif self._sd_staged:
                self.restore_error = "no shell available to restore staged files"
                self.log("WARNING: SD files were staged but no shell is available to restore them")
        finally:
            if self.strategy is not None:
                self._power_off()
            if self.target is not None:
                self.target.cleanup()


def session_record(session: TronSession) -> dict:
    """Durable record of what the session did to the card (written by the test fixture)."""
    return {
        "mode": session.a.mode,
        "staged": {k: {"url": v[0], "sha256": v[1]} for k, v in session.a.staged_names().items()},
        "stock_sums_before": session.stock_sums_before,
        "staged_sums_live": session.staged_sums,
        "restored_sums": session.restored_sums,
        "restore_error": session.restore_error,
        "restored_matches_stock": (session.restored_sums == session.stock_sums_before)
        if (session.restored_sums and session.stock_sums_before)
        else None,
    }
