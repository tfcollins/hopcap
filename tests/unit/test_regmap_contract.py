# SPDX-License-Identifier: MIT
"""hdl/hopctrl_regmap.json is the single source for the controller register map.
The RTL's ID/VERSION words and every register offset the RTL decodes must match it."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAP = json.loads((ROOT / "hdl/hopctrl_regmap.json").read_text())
RTL = (ROOT / "hdl/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v").read_text()


def test_id_and_version_words_match_rtl():
    assert f"ID_WORD      = 32'h{int(MAP['id_word'], 16):08X}" in RTL
    assert f"VERSION_WORD = 32'h{int(MAP['version_word'], 16):08X}" in RTL
    assert bytes.fromhex(MAP["id_word"][2:]).decode() == MAP["id_ascii"]


def test_every_scalar_register_offset_is_decoded_by_rtl():
    decoded = {int(m, 16) for m in re.findall(r"8'h([0-9a-fA-F]{2}):", RTL)}
    for reg in MAP["registers"]:
        off = int(reg["offset"], 16)
        if off >= 0x40:
            continue  # slot table is decoded by address-bit slicing
        assert off in decoded, f"{reg['name']} @ {reg['offset']} not decoded in RTL"


def test_slot_table_is_contiguous_lo_hi_pairs():
    slots = [r for r in MAP["registers"] if r["name"].startswith("SLOT")]
    assert len(slots) == 2 * MAP["slot_count"]
    for i in range(MAP["slot_count"]):
        lo = next(r for r in slots if r["name"] == f"SLOT{i}_LO")
        hi = next(r for r in slots if r["name"] == f"SLOT{i}_HI")
        assert int(lo["offset"], 16) == 0x40 + 8 * i
        assert int(hi["offset"], 16) == 0x44 + 8 * i


def test_states_match_rtl_localparams():
    for name, value in MAP["states"].items():
        assert re.search(rf"\b{name}=3'd{value}\b", RTL), name


def test_offsets_shared_with_ad9361_bridge_are_unchanged():
    """0x00-0x14 and 0x40-0x7C keep the vrt49_ad9361_hop_ctrl meaning so its tooling carries over."""
    by_off = {int(r["offset"], 16): r["name"] for r in MAP["registers"]}
    assert by_off[0x00] == "ID"
    assert by_off[0x04] == "VERSION"
    assert by_off[0x08] == "CTRL_STATUS"
    assert by_off[0x0C] == "SETTLE_TICKS"
    assert by_off[0x10] == "GPIO_SEEN"
    assert by_off[0x14] == "EPOCH"
    assert by_off[0x40] == "SLOT0_LO"
