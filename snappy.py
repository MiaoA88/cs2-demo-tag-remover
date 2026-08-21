# -*- coding: utf-8 -*-
"""Pure-Python Snappy (raw block) decompress + compress.

Snappy raw-block format (https://github.com/google/snappy/blob/main/format_description.txt):
  stream = tag* ; tag = literal | copy1 | copy2 | copy4
"""
from __future__ import annotations


class SnappyError(Exception):
    pass


def decompress(data: bytes) -> bytes:
    """Decompress a snappy block with the standard varint length prefix
    (Google snappy framing used by CS2 demo frames)."""
    # read varint uncompressed length
    length = 0
    shift = 0
    pos = 0
    while True:
        if pos >= len(data):
            raise SnappyError("truncated length prefix")
        b = data[pos]
        pos += 1
        length |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    body = decompress_raw(data[pos:])
    if length != len(body):
        raise SnappyError(f"length mismatch: prefix {length} vs actual {len(body)}")
    return body


def decompress_raw(data: bytes) -> bytes:
    """Decompress a snappy raw block (no length prefix)."""
    out = bytearray()
    pos = 0
    n = len(data)
    while pos < n:
        tag = data[pos]
        pos += 1
        t = tag & 0x03
        if t == 0:  # literal
            length = (tag >> 2) + 1
            if length > 60:
                extra = length - 60
                if pos + extra > n:
                    raise SnappyError("truncated literal length")
                length = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            if pos + length > n:
                raise SnappyError("truncated literal")
            out += data[pos:pos + length]
            pos += length
        elif t == 1:  # copy with 1-byte offset
            length = ((tag >> 2) & 0x07) + 4
            if pos >= n:
                raise SnappyError("truncated copy1")
            offset = ((tag & 0xE0) << 3) | data[pos]
            pos += 1
            _copy_overlap(out, offset, length)
        elif t == 2:  # copy with 2-byte offset
            length = (tag >> 2) + 1
            if pos + 2 > n:
                raise SnappyError("truncated copy2")
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
            _copy_overlap(out, offset, length)
        else:  # copy with 4-byte offset
            length = (tag >> 2) + 1
            if pos + 4 > n:
                raise SnappyError("truncated copy4")
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            _copy_overlap(out, offset, length)
    return bytes(out)


def _copy_overlap(out: bytearray, offset: int, length: int) -> None:
    if offset <= 0 or offset > len(out):
        raise SnappyError(f"invalid copy offset {offset} (output len {len(out)})")
    start = len(out) - offset
    for i in range(length):
        out.append(out[start + i])


def compress(data: bytes) -> bytes:
    """Compress to a snappy block with varint length prefix (demo format)."""
    return _varint(len(data)) + compress_raw(data)


def _varint(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def compress_raw(data: bytes) -> bytes:
    """Compress to a snappy raw block (no length prefix)."""
    out = bytearray()
    n = len(data)
    pos = 0
    literal_start = 0
    # 4-byte hash table: bytes -> last position
    hash_tab = {}
    HASH_BYTES = 4

    def emit_literal(start: int, end: int) -> None:
        nonlocal out
        length = end - start
        while length > 0:
            chunk = min(length, 0x1000000)
            l = chunk - 1
            if l < 60:
                out.append(l << 2)
            else:
                extra = 1 if l < (1 << 8) else (2 if l < (1 << 16) else (3 if l < (1 << 24) else 4))
                out.append((59 + extra) << 2)
                out += l.to_bytes(extra, "little")
            out += data[start:start + chunk]
            start += chunk
            length -= chunk

    def emit_copy(offset: int, length: int) -> None:
        nonlocal out
        if length >= 4 and length <= 11 and offset < 2048:
            out.append(0x01 | ((length - 4) << 2) | ((offset >> 8) << 5))
            out.append(offset & 0xFF)
        elif offset < 65536:
            out.append(0x02 | ((length - 1) << 2))
            out += offset.to_bytes(2, "little")
        else:
            out.append(0x03 | ((length - 1) << 2))
            out += offset.to_bytes(4, "little")

    while pos + HASH_BYTES <= n:
        key = data[pos:pos + HASH_BYTES]
        prev = hash_tab.get(key)
        hash_tab[key] = pos
        match_len = 0
        if prev is not None:
            dist = pos - prev
            if 0 < dist <= 65536:
                # extend match
                ml = HASH_BYTES
                max_ml = min(n - pos, 64)
                while ml < max_ml and data[prev + ml] == data[pos + ml]:
                    ml += 1
                if ml >= 4:
                    match_len = ml
                    emit_literal(literal_start, pos)
                    emit_copy(dist, ml)
                    # update hash table for positions inside the match
                    for k in range(1, ml):
                        if pos + k + HASH_BYTES <= n:
                            hash_tab[data[pos + k:pos + k + HASH_BYTES]] = pos + k
                    pos += ml
                    literal_start = pos
        if match_len == 0:
            pos += 1
    emit_literal(literal_start, n)
    return bytes(out)
