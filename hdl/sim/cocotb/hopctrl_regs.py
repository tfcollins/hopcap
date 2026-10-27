# SPDX-License-Identifier: MIT
"""Register offsets for vrt49_fhm_hop_ctrl, loaded from hdl/hopctrl_regmap.json so the
testbench can never drift from the published map."""
from __future__ import annotations

import json
from pathlib import Path

_MAP = json.loads((Path(__file__).resolve().parents[2] / "hopctrl_regmap.json").read_text())
REG = {r["name"]: int(r["offset"], 16) for r in _MAP["registers"]}
STATE = _MAP["states"]
ID_WORD = int(_MAP["id_word"], 16)
VERSION_WORD = int(_MAP["version_word"], 16)
SLOT_COUNT = _MAP["slot_count"]


def slot_lo(i: int) -> int:
    return REG["SLOT0_LO"] + 8 * i


def slot_hi(i: int) -> int:
    return REG["SLOT0_HI"] + 8 * i
