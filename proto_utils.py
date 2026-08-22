# -*- coding: utf-8 -*-
"""Bit-level and protobuf primitives for CS2 demo surgery.

Three layers are involved, from outside in:

1. protobuf messages (``CDemoPacket``, ``CDemoFullPacket``, ``CSVCMsg_*``) --
   :func:`parse_fields` / :func:`encode_fields`.
2. the ``CDemoPacket.data`` *container*: a bit-packed sequence of
   ``u_bit_var(msg type) + varint(byte size) + payload`` --
   :func:`parse_container` / :func:`encode_container`.  The container is not
   byte-aligned, so the few leftover bits at its end are carried over verbatim.
3. the raw entity bit stream inside ``CSVCMsg_PacketEntities.entity_data`` --
   :func:`find_bit_pattern` / :func:`pattern_hits` locate a byte pattern at any
   bit alignment, :func:`delete_bits_from_bitstream` cuts bits out of it.

All bit orders are LSB-first, which matches little-endian integer semantics:
bit *i* of the stream is bit *i* of ``int.from_bytes(data, "little")``.
"""
from __future__ import annotations


class ProtoError(Exception):
    pass


class BitReader:
    """LSB-first bit reader."""

    __slots__ = ("data", "bit", "_end")

    def __init__(self, data):
        self.data = data
        self.bit = 0
        self._end = len(data) * 8

    def read_nbits(self, n):
        start = self.bit
        end = start + n
        if end > self._end:
            raise IndexError("bit read past end of stream")
        if n == 0:
            return 0
        p0 = start >> 3
        v = int.from_bytes(self.data[p0:(end + 7) >> 3], "little") >> (start - (p0 << 3))
        self.bit = end
        return v & ((1 << n) - 1)

    def read_u_bit_var(self):
        bits = self.read_nbits(6)
        m = bits & 0b110000
        if m == 0b010000:
            return (bits & 0b1111) | (self.read_nbits(4) << 4)
        if m == 0b100000:
            return (bits & 0b1111) | (self.read_nbits(8) << 4)
        if m == 0b110000:
            return (bits & 0b1111) | (self.read_nbits(28) << 4)
        return bits

    def read_varint(self):
        result = 0
        count = 0
        while count < 5:
            b = self.read_nbits(8)
            result |= (b & 127) << (7 * count)
            count += 1
            if not b & 0x80:
                break
        return result

    def read_bytes(self, n):
        return self.read_nbits(n * 8).to_bytes(n, "little") if n else b""

    def bits_left(self):
        return self._end - self.bit


class BitWriter:
    """LSB-first bit writer."""

    __slots__ = ("acc", "bit_len")

    def __init__(self):
        self.acc = 0
        self.bit_len = 0

    def write(self, value, n):
        if n:
            self.acc |= (value & ((1 << n) - 1)) << self.bit_len
            self.bit_len += n

    def write_u_bit_var(self, value):
        if value < 16:
            self.write(value, 6)
        elif value < 1 << 8:
            self.write(0b010000 | (value & 0b1111), 6)
            self.write(value >> 4, 4)
        elif value < 1 << 12:
            self.write(0b100000 | (value & 0b1111), 6)
            self.write(value >> 4, 8)
        elif value < 1 << 32:
            self.write(0b110000 | (value & 0b1111), 6)
            self.write(value >> 4, 28)
        else:
            raise ProtoError(f"u_bit_var out of range: {value}")

    def write_varint(self, value):
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                self.write(b | 0x80, 8)
            else:
                self.write(b, 8)
                break

    def write_bytes(self, data):
        self.write(int.from_bytes(data, "little"), len(data) * 8)

    def to_bytes(self):
        return self.acc.to_bytes((self.bit_len + 7) >> 3, "little")


def read_varint(data, pos):
    shift = 0
    val = 0
    while True:
        if pos >= len(data):
            raise ProtoError("varint out of bounds")
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")
    return val, pos


def write_varint(val):
    if val < 0:
        raise ProtoError(f"negative varint: {val}")
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


def parse_fields(data):
    """Parse the top-level protobuf fields of `data`.

    Returns a list of (field_no, wire_type, value) in wire order, where value is
    an int for varints and bytes for length-delimited / fixed fields.
    Raises ProtoError if `data` is not a clean protobuf message.
    """
    fields = []
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = read_varint(data, pos)
        field_no = tag >> 3
        wt = tag & 7
        if field_no == 0:
            raise ProtoError("field 0")
        if wt == 0:
            v, pos = read_varint(data, pos)
            fields.append((field_no, wt, v))
        elif wt == 2:
            ln, p2 = read_varint(data, pos)
            if p2 + ln > n:
                raise ProtoError("length-delimited out of bounds")
            fields.append((field_no, wt, bytes(data[p2:p2 + ln])))
            pos = p2 + ln
        elif wt == 1:
            if pos + 8 > n:
                raise ProtoError("fixed64 out of bounds")
            fields.append((field_no, wt, bytes(data[pos:pos + 8])))
            pos += 8
        elif wt == 5:
            if pos + 4 > n:
                raise ProtoError("fixed32 out of bounds")
            fields.append((field_no, wt, bytes(data[pos:pos + 4])))
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wt}")
    return fields


def encode_fields(fields):
    """Inverse of :func:`parse_fields` (byte-exact for canonically encoded input)."""
    out = bytearray()
    for field_no, wt, value in fields:
        out += write_varint((field_no << 3) | wt)
        if wt == 0:
            out += write_varint(value)
        elif wt == 2:
            out += write_varint(len(value))
            out += value
        elif wt in (1, 5):
            out += value
        else:
            raise ProtoError(f"unsupported wire type {wt}")
    return bytes(out)


def parse_container(blob):
    """Parse a ``CDemoPacket.data`` container.

    Returns (messages, tail, tail_bits) where messages is a list of
    (msg_type, payload) in order and tail/tail_bits are the leftover padding bits
    at the end of the blob (0..7 bits, kept so the container can be rebuilt
    bit-exactly).
    """
    br = BitReader(blob)
    total = len(blob) * 8
    messages = []
    while br.bits_left() >= 8:
        msg_type = br.read_u_bit_var()
        size = br.read_varint()
        if br.bit + size * 8 > total:
            raise ProtoError("container message overruns the blob")
        messages.append((msg_type, br.read_bytes(size)))
    if not messages:
        raise ProtoError("empty container")
    tail_bits = total - br.bit
    return messages, br.read_nbits(tail_bits), tail_bits


def encode_container(messages, tail, tail_bits):
    """Inverse of :func:`parse_container`."""
    out = BitWriter()
    for msg_type, payload in messages:
        out.write_u_bit_var(msg_type)
        out.write_varint(len(payload))
        out.write_bytes(payload)
    if (out.bit_len + tail_bits) % 8:
        raise ProtoError("rebuilt container is no longer byte-aligned")
    out.write(tail, tail_bits)
    return out.to_bytes()


def delete_bits_from_bitstream(data, bit_offset, bit_len):
    """Delete `bit_len` bits at `bit_offset` (LSB-first), shifting the rest down."""
    n_bits = len(data) * 8
    if bit_offset < 0 or bit_len < 0 or bit_offset + bit_len > n_bits:
        raise ProtoError("bit range out of bounds")
    value = int.from_bytes(data, "little")
    low = value & ((1 << bit_offset) - 1)
    high = value >> (bit_offset + bit_len)
    return (low | (high << bit_offset)).to_bytes((n_bits - bit_len + 7) >> 3, "little")


def _pattern_bits(pattern):
    """LSB-first bit list of `pattern`."""
    return [(b >> i) & 1 for b in pattern for i in range(8)]


def _suffix_patterns(pattern):
    """Return 8 (shift, suffix_bytes) pairs, one per bit alignment.

    If `pattern` starts at bit offset B with ``B % 8 == s``, the bytes at
    positions ``B//8 + 1 .. B//8 + len(pattern) - 1`` equal ``suffix_bytes``.
    Those bytes are determined by the pattern alone, so they can be located with
    C-level :meth:`bytes.find` whatever surrounds the pattern. The pattern's
    leading ``8 - s`` bits and trailing ``s`` bits share a byte with the
    neighbouring data and are therefore not part of the searched suffix.
    """
    pb = _pattern_bits(pattern)
    whole = len(pattern) - 1
    out = []
    for s in range(8):
        bits = pb[8 - s:8 - s + whole * 8]
        buf = bytearray(whole)
        for k, b in enumerate(bits):
            if b:
                buf[k >> 3] |= 1 << (k & 7)
        out.append((s, bytes(buf)))
    return out


def _matches_at(data, start_bit, pat_bits):
    if start_bit < 0 or start_bit + len(pat_bits) > len(data) * 8:
        return False
    for k, pb in enumerate(pat_bits):
        bit = start_bit + k
        if ((data[bit >> 3] >> (bit & 7)) & 1) != pb:
            return False
    return True


def find_bit_pattern(data: bytes, pattern: bytes) -> list[int]:
    """Bit offsets at which `pattern` occurs in `data`, at any bit alignment."""
    pat_bits = _pattern_bits(pattern)
    hits = []
    for s, suffix in _suffix_patterns(pattern):
        pos = 0
        while True:
            idx = data.find(suffix, pos)
            if idx < 0:
                break
            start_bit = idx * 8 - (8 - s)
            if _matches_at(data, start_bit, pat_bits):
                hits.append(start_bit)
            pos = idx + 1
    return hits


def pattern_hits(data: bytes, patterns) -> list[tuple[int, int]]:
    """All (start_bit, bit_len) occurrences of any of `patterns` in `data`.

    Ordered from the end of the stream towards its start and with overlapping
    ranges dropped, so the hits can be deleted one after another without
    invalidating the offsets that are still pending.
    """
    hits = []
    for pattern in patterns:
        hits.extend((start_bit, len(pattern) * 8)
                    for start_bit in find_bit_pattern(data, pattern))
    hits.sort(reverse=True)
    out = []
    prev_start = None
    for start_bit, bit_len in hits:
        if prev_start is not None and start_bit + bit_len > prev_start:
            continue
        out.append((start_bit, bit_len))
        prev_start = start_bit
    return out
