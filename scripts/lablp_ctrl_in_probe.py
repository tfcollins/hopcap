#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Diagnose which AD9361 CTRL_IN pins select the FastLock profile on lablp.

Runs as the ``adi-lg request --run`` command (IIO_URI exported). Brings the hopcap image up
with the lablp session, stores eight RX+TX FastLock profiles whose loopback tones are all
different, enables Profile Pin Select, then for each PS level of CTRL_IN3 (EMIO 43) and each
Tick code 0..7 on gpio_out[2:0]: writes the code immediately, reads the pins back (EMIO
40..43) and measures the RX1 loopback tone on the board from the cf-ad9361-lpc buffer. The
profile actually selected is the slot whose predicted tone matches. Prints a JSON table.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

from tick import CommandDescriptor, CommandID, Flag, TargetFamily

from hopcap.fastlock import FastLockBank, PhyRegs
from hopcap.lab.lablp import Artifacts, LablpSession, target_from_uri
from hopcap.plan import FastLockSlot
from hopcap.transport import (
    PasswordSshShell,
    iio_channel_cli_io,
    iio_debug_cli_write,
    iio_device_cli_io,
)

F_DDS = 1_000_093
SLOTS = [  # tone = tx - rx + f_dds: +1, +3, -2, +5, -4, +2, -3, +4.5 MHz
    (2_400_000_000, 2_400_000_000),
    (2_400_000_000, 2_402_000_000),
    (2_405_000_000, 2_402_000_000),
    (2_410_000_000, 2_414_000_000),
    (2_410_000_000, 2_405_000_000),
    (2_404_000_000, 2_405_000_000),
    (2_420_000_000, 2_416_000_000),
    (2_420_000_000, 2_423_500_000),
]
TONES = [tx - rx + F_DDS for rx, tx in SLOTS]

# On-board capture: read RX1 I/Q from the already-enabled cf-ad9361-lpc buffer (4 channels x
# int16 per sample) and print the dominant tone.
BOARD_FFT = r"""
import numpy as np, subprocess
subprocess.run('iio_readdev -u local: -b 16384 -s 16384 cf-ad9361-lpc > /tmp/probe.bin', shell=True, check=True, timeout=30)
raw = np.fromfile('/tmp/probe.bin', dtype='<i2').reshape(-1, 4)[-8192:]
z = raw[:, 0].astype(float) + 1j * raw[:, 1]
S = np.abs(np.fft.fft(z * np.hanning(len(z))))
fr = np.fft.fftfreq(len(z), 1 / 15.36e6)
i = int(np.argmax(S))
print('TONE', fr[i], 20 * np.log10(S[i] / np.median(S)))
"""


def main() -> int:
    uri = os.environ["IIO_URI"]
    ev = Path(os.environ["HOPCAP_OUT"]) / "probe"
    shell = PasswordSshShell(f"root@{target_from_uri(uri)}", work_dir=ev / ".ssh")
    session = LablpSession(
        shell=shell,
        iio_uri=uri,
        artifacts=Artifacts(
            Path(os.environ["HOPCAP_LABLP_BIN"]),
            Path(os.environ["HOPCAP_TICK_MODULE"]),
            Path(os.environ["HOPCAP_VRT49_MODULE"]),
        ),
        evidence=ev,
    )
    try:
        session.bring_up()
        tick_read, tick_write = iio_device_cli_io(uri, "axi-timed-command-scheduler")
        writers = {
            ch: iio_channel_cli_io(uri, "ad9361-phy", ch, output=True)[1]
            for ch in ("altvoltage0", "altvoltage1")
        }
        bank = FastLockBank(
            write_channel=lambda ch, attr, value: writers[ch](attr, value),
            write_debug=iio_debug_cli_write(uri, "ad9361-phy"),
            regs=PhyRegs(shell, device="ad9361-phy"),
        )
        slots = [FastLockSlot(rx_lo_hz=rx, tx_lo_hz=tx) for rx, tx in SLOTS]
        load = bank.load(slots)
        base = int(
            shell.run(
                "for c in /sys/class/gpio/gpiochip*; do "
                '[ "$(cat $c/label)" = zynq_gpio ] && cat $c/base; done'
            ).split()[-1]
        )
        emio = {k: base + 54 + 40 + k for k in range(4)}  # CTRL_IN0..3 = EMIO 40..43
        shell.run(
            " ".join(
                f"[ -d /sys/class/gpio/gpio{g} ] || echo {g} > /sys/class/gpio/export;"
                for g in emio.values()
            )
        )

        def tick_code(code: int, seq: int) -> None:
            now = int(tick_read("current_time_ticks"))
            d = CommandDescriptor(
                command_id=CommandID.GPIO_WRITE,
                target_id=TargetFamily.GPIO,
                timestamp_ticks=now + 5_000_000,
                flags=Flag.LATE_EXECUTE_NOW,
                sequence_id=seq,
                args=(0xF, code, 0, 0),
            )
            p = d.pack()
            tick_write(
                "command_descriptor",
                ",".join(
                    f"0x{int.from_bytes(p[o : o + 4], 'little'):08x}" for o in range(0, 48, 4)
                ),
            )
            time.sleep(0.3)

        tick_code(0, 0x7000)
        pin = bank.enable_pin_select()
        # the session holds the RX buffer through sysfs; iio_readdev needs it released
        shell.run(
            'for d in /sys/bus/iio/devices/iio:device*; do [ "$(cat $d/name)" = cf-ad9361-lpc ] && echo 0 > $d/buffer/enable; done; true'
        )
        rows = []
        for in3 in (0, 1):
            g3 = emio[3]
            shell.run(
                f"echo out > /sys/class/gpio/gpio{g3}/direction; echo {in3} > /sys/class/gpio/gpio{g3}/value"
            )
            for code in range(8):
                tick_code(code, 0x7100 + 8 * in3 + code)
                pins = shell.run(
                    "for g in "
                    + " ".join(str(emio[k]) for k in range(4))
                    + "; do cat /sys/class/gpio/gpio$g/value; done"
                ).split()
                out = shell.run("python3 -c " + shlex.quote(BOARD_FFT), timeout_s=60)
                tone = next(
                    float(line.split()[1]) for line in out.splitlines() if line.startswith("TONE")
                )
                profile = min(range(8), key=lambda k: abs(TONES[k] - tone))
                rows.append(
                    {
                        "ctrl_in3": in3,
                        "code": code,
                        "pins_in0_in3": pins,
                        "tone_hz": round(tone),
                        "profile": profile,
                        "profile_err_hz": round(tone - TONES[profile]),
                    }
                )
                print(json.dumps(rows[-1]), flush=True)
        (ev / "ctrl_in_probe.json").write_text(
            json.dumps({"load": load, "pin_select": pin, "rows": rows}, indent=1, default=str)
        )
    finally:
        shell.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
