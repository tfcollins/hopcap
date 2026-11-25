# SPDX-License-Identifier: MIT
"""hopcap board session on lablp (ADRV9361-Z7035 on the ADRV1CRR-FMC, stock Kuiper 2023_R2_P1).

Runs inside ``adi-lg request --part adrv9361z7035 --carrier adrv1crr-fmc --mode uri
--power-down --run ...`` (``examples/hardware/adrv9361_hopcap.py``), which has reserved and
booted the board's own SD card and exports ``IIO_URI=ip:<board>``; adi-lg releases and powers the
board off afterwards, which also undoes everything below (the SD card is never written).

Stages (:class:`LablpSession`), each recorded in ``<evidence>/session.json``:

1. ``connect``   root ssh (password-only on Kuiper) through one askpass-authenticated master;
                 the kernel release and both modules' vermagic must match;
2. ``push``      bitstream (.bin), overlay (.dtbo, compiled here with ``dtc -@``), the Tick and
                 vrt49 modules and ``scripts/lablp_hopcap_runtime_load.sh``, sha256-checked;
3. ``load``      the runtime-load script: AD9517 OUT4 = 125 MHz + enabled BEFORE the PL load,
                 unbind, FPGA manager, rebind, overlay, modules, iiod restart;
4. ``devices``   network IIO shows Tick, the framer and the AD9361 devices; Tick at 50 MHz;
5. ``radio``     15.36 MSPS, RF bandwidth, manual RX gain, TX attenuation, the TX1 DDS tone at
                 ``f_dds`` (the loopback the hop proof reads);
6. ``rx_buffer`` all four RX converter channels (``in_voltage0..3``) and the ``cf-ad9361-lpc``
                 buffer on: the VRT tap and the time base count ``packed_fifo_wr_en`` beats,
                 which fire at fs only with all four enabled (D1 concern 1);
7. ``link``      framer endpoint = nemo enp7s0 (192.168.2.10, 68:05:ca:19:88:8c), PCS resetdone
                 (framer 0x78[16]);
8. ``timebase``  time base LOCKED on "tick", measured beats per PPS within margin of 15360000,
                 Tick epoch aligned to the framer second (:class:`hopcap.timebase.TickAligner`);
9. ``run``       optional: :func:`hopcap.run.run_live` with a FastLock plan -> a run bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from hopcap.platform import ADRV9361, Platform

ROOT = Path(__file__).resolve().parents[3]
LOAD_SCRIPT = ROOT / "scripts" / "lablp_hopcap_runtime_load.sh"
OVERLAY_DTS = ROOT / "kernel" / "dts" / "zynq-adrv9361z7035-hopcap-overlay.dts"
DEFAULT_PLAN = ROOT / "examples" / "plans" / "adrv9361-loopback-8slot.json"
KERNEL_RELEASE = "6.1.70-284114-g2e8908932dfd"  # lablp stock Kuiper 2023_R2_P1
REMOTE_DIR = "/tmp/hopcap"
RX_CHANNELS = ("voltage0", "voltage1", "voltage2", "voltage3")  # RX1 I/Q, RX2 I/Q
REQUIRED_DEVICES = (
    "axi-timed-command-scheduler",
    "vrt49_framer",
    "ad9361-phy",
    "cf-ad9361-lpc",
    "cf-ad9361-dds-core-lpc",
)


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Artifacts:
    bitstream_bin: Path
    tick_module: Path
    vrt49_module: Path
    overlay_dts: Path = OVERLAY_DTS
    overlay_dtbo: Path | None = None  # precompiled; else compiled from overlay_dts

    def check(self) -> None:
        for name in ("bitstream_bin", "tick_module", "vrt49_module", "overlay_dts"):
            p = getattr(self, name)
            if not Path(p).is_file() or Path(p).stat().st_size == 0:
                raise SessionError(f"{name}: {p} is missing or empty")


@dataclass(frozen=True)
class RadioConfig:
    sample_rate_hz: int = 15_360_000
    rf_bandwidth_hz: int = 12_000_000
    rx_gain_db: int = 10  # manual: the TX1->RX1 coupling is strong (95 dB tone on lablp)
    tx_attenuation_db: float = -30.0  # out_voltage0_hardwaregain (attenuation is negative)
    f_dds_hz: int = 1_000_000
    dds_scale: float = 0.25


def target_from_uri(uri: str) -> str:
    if not uri.startswith("ip:") or not uri[3:]:
        raise SessionError(f"IIO_URI {uri!r} is not ip:<board>")
    return uri[3:]


def parse_kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and " " not in key:
            out[key] = value
    return out


def sysfs_write_snippet(device: str, items: list[tuple[str, str]]) -> str:
    """Board shell: write ``items`` to the IIO device's sysfs files, failing on a missing file,
    and print ``ATTR <name> <readback>`` for each."""
    from hopcap.timebase import iio_dir_snippet

    prologue = "set -e; " + iio_dir_snippet("D", device)
    parts = []
    for attr, value in items:
        a = shlex.quote(attr)
        parts.append(
            f'[ -e "$D/{attr}" ] || {{ echo "{device}: no attribute {attr}" >&2; exit 3; }}; '
            f'echo {shlex.quote(str(value))} > "$D/"{a}; echo "ATTR {attr} $(cat "$D/"{a})"'
        )
    return prologue + "; ".join(parts)


def parse_attrs(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.startswith("ATTR "):
            _, name, *rest = line.split(" ", 2)
            out[name] = rest[0].strip() if rest else ""
    return out


def radio_items(cfg: RadioConfig) -> dict[str, list[tuple[str, str]]]:
    """sysfs writes per device. DDS: tone F1 on TX1 I and Q (I leads Q by 90 degrees in ADI's
    default phases, so a single sideband at +f_dds), F2 and TX2 silent."""
    phy = [
        ("in_voltage_sampling_frequency", str(cfg.sample_rate_hz)),
        ("in_voltage_rf_bandwidth", str(cfg.rf_bandwidth_hz)),
        ("out_voltage_rf_bandwidth", str(cfg.rf_bandwidth_hz)),
        ("in_voltage0_gain_control_mode", "manual"),
        ("in_voltage0_hardwaregain", str(cfg.rx_gain_db)),
        ("out_voltage0_hardwaregain", f"{cfg.tx_attenuation_db:g}"),
    ]
    dds = [
        ("out_altvoltage0_TX1_I_F1_frequency", str(cfg.f_dds_hz)),
        ("out_altvoltage2_TX1_Q_F1_frequency", str(cfg.f_dds_hz)),
        ("out_altvoltage0_TX1_I_F1_scale", f"{cfg.dds_scale:g}"),
        ("out_altvoltage2_TX1_Q_F1_scale", f"{cfg.dds_scale:g}"),
        ("out_altvoltage1_TX1_I_F2_scale", "0"),
        ("out_altvoltage3_TX1_Q_F2_scale", "0"),
        ("out_altvoltage4_TX2_I_F1_scale", "0"),
        ("out_altvoltage5_TX2_I_F2_scale", "0"),
        ("out_altvoltage6_TX2_Q_F1_scale", "0"),
        ("out_altvoltage7_TX2_Q_F2_scale", "0"),
        ("out_altvoltage0_TX1_I_F1_raw", "1"),
    ]
    return {"ad9361-phy": phy, "cf-ad9361-dds-core-lpc": dds}


def rx_buffer_snippet(device: str, *, length: int = 65536) -> str:
    """Enable in_voltage0..3 and the buffer (all other scan elements off). Nobody reads the
    buffer: the DMA stalls once its blocks fill, but util_cpack2 keeps packing and
    packed_fifo_wr_en keeps firing, which is all the tap and the time base need."""
    from hopcap.timebase import iio_dir_snippet

    en = " ".join(f"{c}" for c in RX_CHANNELS)
    return (
        "set -e; "
        + iio_dir_snippet("D", device)
        + 'S="$D/scan_elements"; B="$D/buffer"; '
        + 'echo 0 > "$B/enable"; '
        + 'for f in "$S"/*_en; do echo 0 > "$f"; done; '
        + f'for c in {en}; do [ -e "$S/in_${{c}}_en" ] || {{ echo "no in_$c" >&2; exit 3; }}; '
        + 'echo 1 > "$S/in_${c}_en"; done; '
        + f'echo {int(length)} > "$B/length"; echo 1 > "$B/enable"; '
        + 'echo "BUFFER $(cat "$B/enable") $(cat "$B/length")"; '
        + 'for f in "$S"/*_en; do echo "SCAN $(basename "$f" _en) $(cat "$f")"; done'
    )


def rx_buffer_off_snippet(device: str) -> str:
    from hopcap.timebase import iio_dir_snippet

    return "set -e; " + iio_dir_snippet("D", device) + 'echo 0 > "$D/buffer/enable"'


def parse_buffer(text: str) -> dict:
    out: dict = {"enabled": False, "length": None, "scan": {}}
    for line in text.splitlines():
        parts = line.split()
        if parts[:1] == ["BUFFER"] and len(parts) == 3:
            out["enabled"], out["length"] = parts[1] == "1", int(parts[2])
        elif parts[:1] == ["SCAN"] and len(parts) == 3:
            out["scan"][parts[1]] = parts[2] == "1"
    return out


def module_vermagic(path: Path, *, run=subprocess.run) -> str:
    out = run(["modinfo", "-F", "vermagic", str(path)], capture_output=True, text=True, check=True)
    return out.stdout.split()[0] if out.stdout.split() else ""


def compile_overlay(dts: Path, dtbo: Path, *, run=subprocess.run) -> Path:
    run(["dtc", "-@", "-q", "-I", "dts", "-O", "dtb", "-o", str(dtbo), str(dts)], check=True)
    return dtbo


@dataclass
class LablpSession:
    shell: object  # PasswordSshShell (connect/push/run/close) or a fake
    iio_uri: str
    artifacts: Artifacts
    evidence: Path
    plat: Platform = ADRV9361
    radio: RadioConfig = field(default_factory=RadioConfig)
    iio_list: Callable[[], str] | None = None  # `iio_attr -u URI -d` output
    iio_read: Callable[[str, str], str] | None = None  # (device, attr) -> value
    run_cmd: Callable = subprocess.run  # host commands (modinfo, dtc)
    # () -> (framer sysfs, framer MMIO); default: the on-target helper over ``shell``
    framer_io: Callable[[], tuple] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    record: dict = field(default_factory=dict)

    def __post_init__(self):
        self.evidence = Path(self.evidence)
        self.evidence.mkdir(parents=True, exist_ok=True)
        if self.iio_list is None:
            self.iio_list = lambda: (
                subprocess.run(
                    ["iio_attr", "-u", self.iio_uri, "-d"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stdout
            )
        if self.iio_read is None:
            from hopcap.transport import parse_iio_attr_value

            def _read(dev: str, attr: str) -> str:
                out = subprocess.run(
                    ["iio_attr", "-u", self.iio_uri, "-d", dev, attr],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                ).stdout
                return parse_iio_attr_value(out)

            self.iio_read = _read

    def _save(self) -> None:
        (self.evidence / "session.json").write_text(
            json.dumps(self.record, indent=2, default=str) + "\n"
        )

    def stage(self, name: str, fn: Callable[[], dict]) -> dict:
        self.log(f"lablp: {name}")
        try:
            self.record[name] = fn()
        except Exception as exc:
            self.record[name] = {"error": f"{type(exc).__name__}: {exc}"}
            self._save()
            raise
        self._save()
        return self.record[name]

    # -- stages ----------------------------------------------------------------------------
    def connect(self) -> dict:
        release = self.shell.run("uname -r").strip().split()[-1]
        if release != KERNEL_RELEASE:
            raise SessionError(f"board runs {release}, expected stock Kuiper {KERNEL_RELEASE}")
        magic = {
            p.name: module_vermagic(p, run=self.run_cmd)
            for p in (self.artifacts.tick_module, self.artifacts.vrt49_module)
        }
        bad = {k: v for k, v in magic.items() if v != release}
        if bad:
            raise SessionError(f"module vermagic {bad} does not match the board's {release}")
        return {"target": self.shell.target, "kernel_release": release, "vermagic": magic}

    def push(self) -> dict:
        a = self.artifacts
        dtbo = a.overlay_dtbo or compile_overlay(
            a.overlay_dts, self.evidence / "hopcap-adrv9361.dtbo", run=self.run_cmd
        )
        files = {
            "system_top.bin": a.bitstream_bin,
            "hopcap.dtbo": dtbo,
            "axi_timed_command_scheduler.ko": a.tick_module,
            "iio-vrt49-buffer.ko": a.vrt49_module,
            "lablp_hopcap_runtime_load.sh": LOAD_SCRIPT,
        }
        return {
            name: {"local": str(src), "sha256": self.shell.push(Path(src), f"{REMOTE_DIR}/{name}")}
            for name, src in files.items()
        }

    def load(self) -> dict:
        d = REMOTE_DIR
        out = self.shell.run(
            f"sh {d}/lablp_hopcap_runtime_load.sh {d}/system_top.bin {d}/hopcap.dtbo "
            f"{d}/axi_timed_command_scheduler.ko {d}/iio-vrt49-buffer.ko 2>&1",
            timeout_s=300,
        )
        (self.evidence / "runtime-load.log").write_text(out)
        kv = parse_kv(out)
        if kv.get("hopcap_runtime_load") != "ok":
            raise SessionError("runtime load did not finish (see runtime-load.log)")
        if kv.get("ad9517_out4_frequency") != "125000000" or kv.get("ad9517_out4_raw") != "1":
            raise SessionError("AD9517 OUT4 is not 125 MHz / enabled")
        if kv.get("fpga_manager_state") != "operating":
            raise SessionError(f"FPGA manager state {kv.get('fpga_manager_state')}")
        if kv.get("bin_sha256") != self.record.get("push", {}).get("system_top.bin", {}).get(
            "sha256"
        ):
            raise SessionError("the FPGA manager loaded a bitstream other than the pushed one")
        return kv

    def devices(self, *, attempts: int = 30, poll_s: float = 2.0) -> dict:
        listing = ""
        for _ in range(attempts):
            listing = self.iio_list() or ""
            if all(d in listing for d in REQUIRED_DEVICES):
                break
            self.sleep(poll_s)
        (self.evidence / "iio-devices.log").write_text(listing)
        missing = [d for d in REQUIRED_DEVICES if d not in listing]
        if missing:
            raise SessionError(f"not on network IIO after the load: {', '.join(missing)}")
        tick_hz = int(self.iio_read(self.plat.tick_iio_name, "time_tick_hz").split()[-1], 0)
        self.plat.check_tick_hz(tick_hz)
        return {"devices": list(REQUIRED_DEVICES), "time_tick_hz": tick_hz}

    def configure_radio(self) -> dict:
        out = {}
        for device, items in radio_items(self.radio).items():
            got = parse_attrs(self.shell.run(sysfs_write_snippet(device, items)))
            out[device] = got
        fs = int(out["ad9361-phy"].get("in_voltage_sampling_frequency", "0").split()[0])
        if fs != self.radio.sample_rate_hz:
            raise SessionError(f"sample rate reads {fs}, expected {self.radio.sample_rate_hz}")
        return {"config": self.radio.__dict__, "readback": out}

    def rx_buffer(self) -> dict:
        got = parse_buffer(self.shell.run(rx_buffer_snippet(self.plat.rx_iio_name)))
        on = sorted(k for k, v in got["scan"].items() if v)
        want = sorted(f"in_{c}" for c in RX_CHANNELS)
        if not got["enabled"] or on != want:
            raise SessionError(f"{self.plat.rx_iio_name} buffer {got}: need {want} and enable=1")
        return got

    def link(self, sysfs, mmio, framer_cfg) -> dict:
        from hopcap import timebase

        sysfs.write("v49_enable", "0")
        sysfs.write_many(
            [
                ("v49_packet_mtu", str(framer_cfg.mtu_bytes)),
                ("v49_udp_dest_ip", framer_cfg.dest_ip),
                ("v49_udp_source_ip", framer_cfg.source_ip),
                ("v49_udp_dest_port", str(framer_cfg.dest_port)),
                ("v49_udp_source_port", str(framer_cfg.source_port)),
                ("v49_udp_dest_mac", framer_cfg.dest_mac),
                ("v49_udp_source_mac", framer_cfg.source_mac),
            ]
        )
        sysfs.write("v49_udp_apply", "1")
        readback = sysfs.read_many(["v49_udp_dest_ip", "v49_udp_dest_mac", "v49_udp_source_ip"])
        if readback["v49_udp_dest_ip"].strip() != framer_cfg.dest_ip:
            raise SessionError(f"framer dest ip reads {readback['v49_udp_dest_ip']!r}")
        if readback["v49_udp_dest_mac"].strip().lower() != framer_cfg.dest_mac.lower():
            raise SessionError(f"framer dest mac reads {readback['v49_udp_dest_mac']!r}")
        pcs = timebase.check_pcs(mmio.read32(timebase.PCS_STATUS))
        return {"udp": readback, "pcs": pcs}

    def timebase(self, sysfs, mmio, framer_cfg) -> dict:
        from hopcap import framer, timebase

        armed = framer.arm_time_base(sysfs, framer_cfg, sleep=self.sleep, monotonic=self.monotonic)
        period = timebase.check_pps_period(**timebase.read_pps_period(mmio))
        aligned = timebase.TickAligner(
            self.shell,
            self.plat.tick_hz,
            tick_name=self.plat.tick_iio_name,
            framer_name=self.plat.framer_iio_name,
            sleep=self.sleep,
        ).align()
        return {"armed": armed, "pps_period": period, "tick_alignment": aligned}

    def run_plan(self, plan_path: Path, *, rx_cpu: int | None = None) -> dict:
        from hopcap.cli import _git_sha, load_fastlock_plan
        from hopcap.framer import FramerConfig
        from hopcap.run import RunConfig, build_components_adrv9361, run_live

        plan = load_fastlock_plan(plan_path)
        out = self.evidence / "run"
        comps = build_components_adrv9361(
            self.shell, iio_uri=self.iio_uri, out_dir=out, plat=self.plat, rx_cpu=rx_cpu
        )
        fcfg = FramerConfig.for_platform(self.plat)
        cfg = RunConfig(
            plan=plan,
            out_dir=out,
            framer=fcfg,
            provenance={
                "hopcap_commit": _git_sha(),
                "iio_uri": self.iio_uri,
                "platform": self.plat.name,
                "plan_file": str(plan_path),
                "artifacts": self.record.get("push"),
            },
            expected_peer=(fcfg.source_ip, fcfg.source_port),
            lookahead_hops=7,
            platform=self.plat,
        )
        report = run_live(cfg, comps)
        return {"disposition": report.disposition, "out_dir": str(out)}

    # -- the whole session -----------------------------------------------------------------
    def _framer_io(self) -> tuple:
        from hopcap.transport import HelperMmio, HelperSysfs, find_iio_device_dir

        helper = HelperMmio(self.shell, base=self.plat.framer_base)
        helper.ensure_uploaded()
        sysfs = HelperSysfs(
            self.shell, device_dir=find_iio_device_dir(self.shell, self.plat.framer_iio_name)
        )
        return sysfs, helper

    def bring_up(self) -> dict:
        from hopcap.framer import FramerConfig

        self.artifacts.check()
        self.stage("connect", self.connect)
        self.stage("push", self.push)
        self.stage("load", self.load)
        self.stage("devices", self.devices)
        self.stage("radio", self.configure_radio)
        self.stage("rx_buffer", self.rx_buffer)
        sysfs, helper = (self.framer_io or self._framer_io)()
        fcfg = FramerConfig.for_platform(self.plat)
        self.stage("link", lambda: self.link(sysfs, helper, fcfg))
        self.stage("timebase", lambda: self.timebase(sysfs, helper, fcfg))
        return self.record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--bin", type=Path, default=os.environ.get("HOPCAP_LABLP_BIN"))
    ap.add_argument("--tick-module", type=Path, default=os.environ.get("HOPCAP_TICK_MODULE"))
    ap.add_argument("--vrt49-module", type=Path, default=os.environ.get("HOPCAP_VRT49_MODULE"))
    ap.add_argument("--dtbo", type=Path, default=None, help="precompiled overlay (default: dtc)")
    ap.add_argument("--plan", type=Path, default=os.environ.get("HOPCAP_PLAN") or DEFAULT_PLAN)
    ap.add_argument("--no-run", action="store_true", help="bring-up only, no hop run")
    ap.add_argument("--rx-cpu", type=int, default=None)
    args = ap.parse_args(argv)
    uri = os.environ.get("IIO_URI", "")
    try:
        if not (args.bin and args.tick_module and args.vrt49_module):
            raise SessionError("need --bin, --tick-module and --vrt49-module (or HOPCAP_* env)")
        from hopcap.transport import PasswordSshShell

        host = target_from_uri(uri)
        shell = PasswordSshShell(f"root@{host}", work_dir=args.evidence / ".ssh")
        session = LablpSession(
            shell=shell,
            iio_uri=uri,
            artifacts=Artifacts(
                args.bin, args.tick_module, args.vrt49_module, overlay_dtbo=args.dtbo
            ),
            evidence=args.evidence,
        )
        session.record["environment"] = {
            "iio_uri": uri,
            "lg_place": os.environ.get("LG_PLACE"),
            "plan": str(args.plan),
        }
        try:
            session.bring_up()
            if not args.no_run:
                session.stage("run", lambda: session.run_plan(args.plan, rx_cpu=args.rx_cpu))
        finally:
            shell.close()
    except Exception as exc:
        print(f"lablp session: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    result = session.record.get("run", {}).get("disposition", "bring-up ok")
    print(f"lablp session: {result}; evidence {args.evidence}")
    return 0 if result in ("ok", "bring-up ok") else 1


if __name__ == "__main__":
    sys.exit(main())
