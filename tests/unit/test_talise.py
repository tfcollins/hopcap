# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from hopcap.talise import FhmStager, TaliseError


class FakeLo:
    def __init__(self, init_hz=2_450_000_000):
        self.attrs = {
            "frequency_hopping_mode_enable": "0",
            "frequency_hopping_mode": str(init_hz),
            "frequency": str(init_hz),
        }
        self.writes = []

    def read(self, name):
        return self.attrs[name]

    def write(self, name, value):
        self.writes.append((name, value))
        self.attrs[name] = value


def test_enable_stage_readback_disable_sequence():
    lo = FakeLo()
    st = FhmStager(lo.read, lo.write)
    assert st.readback_hz() == 2_450_000_000
    st.enable()
    st.stage(2_410_000_000)
    assert lo.writes == [
        ("frequency_hopping_mode_enable", "1"),
        ("frequency_hopping_mode", "2410000000"),
    ]
    assert st.staged_hz == 2_410_000_000
    st.disable()
    assert lo.writes[-1] == ("frequency_hopping_mode_enable", "0")


def test_stage_rejects_out_of_band():
    lo = FakeLo()
    st = FhmStager(lo.read, lo.write, band_hz=(2_400_000_000, 2_500_000_000))
    with pytest.raises(TaliseError, match="band"):
        st.stage(2_600_000_000)


def test_readback_parses_trailing_tokens_and_rejects_garbage():
    lo = FakeLo()
    lo.attrs["frequency_hopping_mode"] = "  2450000000 \n"
    assert FhmStager(lo.read, lo.write).readback_hz() == 2_450_000_000
    lo.attrs["frequency_hopping_mode"] = "ERROR"
    with pytest.raises(TaliseError):
        FhmStager(lo.read, lo.write).readback_hz()
