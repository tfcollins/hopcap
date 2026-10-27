#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract the JTAG boot inputs (system_top.bit, psu_init.tcl) from a Vivado XSA.

    scripts/extract_jtag_inputs.py system_top.xsa OUT_DIR

Prints "sha256  name" for each file written. Both come from the one XSA so the PL image and the
PS init that the JTAG boot loads always belong to the same build (vrt49's Corundum CI takes
psu_init.tcl from its XSA the same way and applies the same sanity check)."""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

MEMBERS = ("system_top.bit", "psu_init.tcl")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    xsa, out = Path(argv[1]), Path(argv[2])
    with zipfile.ZipFile(xsa) as z:
        data = {m: z.read(m) for m in MEMBERS}
    if b"proc psu_init" not in data["psu_init.tcl"]:
        print(f"{xsa}: psu_init.tcl does not define psu_init", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for name, blob in data.items():
        (out / name).write_bytes(blob)
        print(f"{hashlib.sha256(blob).hexdigest()}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
