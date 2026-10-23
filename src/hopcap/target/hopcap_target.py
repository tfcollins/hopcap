#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hopcap on-target helper. Runs on the ZU11EG with the stock Kuiper python3 (3.9+), stdlib
only. One invocation performs one JSON batch so the host pays one shell round trip per
batch instead of one per register.

  hopcap_target.py mmio  --base 0x84AC0000 [--span 0x1000] [--mem /dev/mem]   < {"ops":[...]}
      ops: {"op":"r","off":N} | {"op":"w","off":N,"val":V}      -> {"results":[...]}
  hopcap_target.py sysfs --dir /sys/bus/iio/devices/iio:deviceN                < {"ops":[...]}
      ops: {"op":"r","attr":NAME} | {"op":"w","attr":NAME,"val":STR}
  hopcap_target.py find  --name axi-vrt49-framer [--sysfs /sys/bus/iio/devices]  -> {"path":...}
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import struct
import sys


def fail(msg: str, code: int = 2) -> None:
    sys.stdout.write(json.dumps({"error": msg}) + "\n")
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def cmd_mmio(args: argparse.Namespace) -> None:
    batch = json.load(sys.stdin)
    span = int(args.span, 0)
    page = mmap.PAGESIZE
    base = int(args.base, 0)
    if args.map_offset is None:
        map_off = base & ~(page - 1)
        inner = base - map_off
    else:
        # Test hook: the mapped file *is* the register window, starting at --map-offset.
        map_off = int(args.map_offset, 0)
        inner = 0
    length = inner + span
    length = (length + page - 1) & ~(page - 1)
    fd = os.open(args.mem, os.O_RDWR | os.O_SYNC)
    try:
        if args.mem != "/dev/mem" and os.fstat(fd).st_size < map_off + length:
            os.ftruncate(fd, map_off + length)
        mm = mmap.mmap(
            fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=map_off
        )
    finally:
        os.close(fd)
    results = []
    try:
        for op in batch["ops"]:
            off = int(op["off"])
            if off % 4:
                fail(f"mmio offset 0x{off:x} not 4-byte aligned")
            if off < 0 or off + 4 > span:
                fail(f"mmio offset 0x{off:x} out of range (span 0x{span:x})")
            pos = inner + off
            if op["op"] == "r":
                results.append(struct.unpack_from("<I", mm, pos)[0])
            elif op["op"] == "w":
                struct.pack_into("<I", mm, pos, int(op["val"]) & 0xFFFFFFFF)
                results.append(None)
            else:
                fail(f"unknown op {op.get('op')!r}")
    finally:
        mm.close()
    sys.stdout.write(json.dumps({"results": results}) + "\n")


def cmd_sysfs(args: argparse.Namespace) -> None:
    batch = json.load(sys.stdin)
    results = []
    for op in batch["ops"]:
        name = op["attr"]
        if "/" in name or name.startswith("."):
            fail(f"bad attribute name {name!r}")
        path = os.path.join(args.dir, name)
        if op["op"] == "r":
            with open(path) as fp:
                results.append(fp.read().strip())
        elif op["op"] == "w":
            with open(path, "w") as fp:
                fp.write(str(op["val"]))
            results.append(None)
        else:
            fail(f"unknown op {op.get('op')!r}")
    sys.stdout.write(json.dumps({"results": results}) + "\n")


def cmd_find(args: argparse.Namespace) -> None:
    for entry in sorted(os.listdir(args.sysfs)):
        name_path = os.path.join(args.sysfs, entry, "name")
        try:
            with open(name_path) as fp:
                if fp.read().strip() == args.name:
                    sys.stdout.write(json.dumps({"path": os.path.join(args.sysfs, entry)}) + "\n")
                    return
        except OSError:
            continue
    sys.stdout.write(json.dumps({"error": f"no iio device named {args.name!r}"}) + "\n")
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="hopcap_target.py")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True
    m = sub.add_parser("mmio")
    m.add_argument("--base", required=True)
    m.add_argument("--span", default="0x1000")
    m.add_argument("--mem", default="/dev/mem")
    m.add_argument(
        "--map-offset", default=None, help="test hook: map a file as the register window"
    )
    m.set_defaults(func=cmd_mmio)
    s = sub.add_parser("sysfs")
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_sysfs)
    f = sub.add_parser("find")
    f.add_argument("--name", required=True)
    f.add_argument("--sysfs", default="/sys/bus/iio/devices")
    f.set_defaults(func=cmd_find)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
