# SPDX-License-Identifier: MIT
"""Spool file format shared by the C receiver, the Python receiver and the decoder.

Record = ``<QQIIHH`` header (mono_ns, real_ns, len, peer_ipv4, peer_port, pad) + ``len`` bytes.
Arrival times are kept per datagram so the host clock domain survives into the manifest.
"""
from __future__ import annotations

import socket
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

HEADER_STRUCT = struct.Struct("<QQIIHH")
MAGIC_NOTE = "hopcap-spool/v1"


@dataclass(frozen=True)
class Record:
    mono_ns: int
    real_ns: int
    peer_ip: str
    peer_port: int
    data: bytes


def _ip_to_int(ip: str) -> int:
    return int.from_bytes(socket.inet_aton(ip), "big")


def _int_to_ip(v: int) -> str:
    return socket.inet_ntoa(v.to_bytes(4, "big"))


def write_records(fp: BinaryIO, records: Iterable[Record]) -> int:
    n = 0
    for r in records:
        fp.write(HEADER_STRUCT.pack(r.mono_ns, r.real_ns, len(r.data), _ip_to_int(r.peer_ip), r.peer_port, 0))
        fp.write(r.data)
        n += 1
    return n


def iter_records(fp: BinaryIO, *, strict: bool = True) -> Iterator[Record]:
    while True:
        head = fp.read(HEADER_STRUCT.size)
        if not head:
            return
        if len(head) < HEADER_STRUCT.size:
            if strict:
                raise ValueError("truncated spool header")
            return
        mono, real, length, ip, port, _pad = HEADER_STRUCT.unpack(head)
        data = fp.read(length)
        if len(data) < length:
            if strict:
                raise ValueError("truncated spool record")
            return
        yield Record(mono_ns=mono, real_ns=real, peer_ip=_int_to_ip(ip), peer_port=port, data=data)
