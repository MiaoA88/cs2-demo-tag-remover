# -*- coding: utf-8 -*-
"""Minimal protobuf walker used to delete bits inside a nested bytes field.

CS2 demo frames are snappy-compressed protobuf messages. Entity data (e.g.
the ClassInfo field-3 blob or string-table baseline user data) lives inside
nested `bytes` fields.  Deleting bits from that inner bit stream shrinks the
blob, so every enclosing length varint must be rewritten -- this module does
that.

A payload is treated as a protobuf message only while it parses cleanly;
otherwise it is treated as a CDemoPacket-style container or a raw entity bit
stream (the innermost layer).
"""
from __future__ import annotations

MAX_DEPTH = 12


class ProtoError(Exception):
    pass


class BitReader:
    """LSB-first bit reader."""

    def __init__(self, data):
        self.data = data
        self.bit = 0

    def read_nbits(self, n):
        v = 0
        for i in range(n):
            v |= ((self.data[(self.bit + i) >> 3] >> ((self.bit + i) & 7)) & 1) << i
        self.bit += n
        return v

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
        while True:
            if count >= 5:
                return result
            b = self.read_nbits(8)
            result |= (b & 127) << (7 * count)
            count += 1
            if b & 0x80 == 0:
                break
        return result

    def read_bytes(self, n):
        return bytes(self.read_nbits(8) for _ in range(n))

    def bits_left(self):
        return len(self.data) * 8 - self.bit


class BitWriter:
    def __init__(self):
        self.bits = []

    def write(self, value, n):
        for i in range(n):
            self.bits.append((value >> i) & 1)

    def write_u_bit_var(self, value):
        if value < 16:
            self.write(value, 6)
        elif value < 16 + (16 << 4):
            self.write(0b010000 | (value & 0b1111), 6)
            self.write(value >> 4, 4)
        elif value < 16 + (16 << 8):
            self.write(0b100000 | (value & 0b1111), 6)
            self.write(value >> 4, 8)
        else:
            self.write(0b110000 | (value & 0b1111), 6)
            self.write(value >> 4, 28)

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
        for b in data:
            self.write(b, 8)

    def to_bytes(self):
        out = bytearray((len(self.bits) + 7) // 8)
        for i, b in enumerate(self.bits):
            if b:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)


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
        val &= (1 << 64) - 1
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
    """Parse top-level protobuf fields of `data`.

    Returns list of (field_no, wire_type, value) where value is an int for
    varint fields and bytes for length-delimited / fixed fields.
    Raises ProtoError if the data is not a clean protobuf message.
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
        if wt == 0:  # varint
            v, pos = read_varint(data, pos)
            fields.append((field_no, wt, v))
        elif wt == 2:  # length-delimited
            ln, p2 = read_varint(data, pos)
            if p2 + ln > n:
                raise ProtoError("length-delimited out of bounds")
            fields.append((field_no, wt, bytes(data[p2:p2 + ln])))
            pos = p2 + ln
        elif wt == 1:  # fixed64
            if pos + 8 > n:
                raise ProtoError("fixed64 out of bounds")
            fields.append((field_no, wt, bytes(data[pos:pos + 8])))
            pos += 8
        elif wt == 5:  # fixed32
            if pos + 4 > n:
                raise ProtoError("fixed32 out of bounds")
            fields.append((field_no, wt, bytes(data[pos:pos + 4])))
            pos += 4
        else:
            raise ProtoError(f"unsupported wire type {wt}")
    return fields


def encode_fields(fields):
    out = bytearray()
    for field_no, wt, value in fields:
        tag = write_varint((field_no << 3) | wt)
        out += tag
        if wt == 0:
            out += write_varint(value)
        elif wt == 2:
            out += write_varint(len(value))
            out += value
        elif wt in (1, 5):
            out += value
        else:  # pragma: no cover
            raise ProtoError(f"unsupported wire type {wt}")
    return bytes(out)


def delete_bits_from_bitstream(data: bytes, bit_offset: int, bit_len: int) -> bytes:
    """Delete `bit_len` bits at `bit_offset` (LSB-first) from a raw byte blob,
    shifting the remaining bits left. Returns the shorter blob."""
    n_bits = len(data) * 8
    if bit_offset < 0 or bit_offset + bit_len > n_bits:
        raise ProtoError("bit range out of bounds")
    new_n_bits = n_bits - bit_len
    out = bytearray((new_n_bits + 7) // 8)
    for i in range(bit_offset):
        if (data[i >> 3] >> (i & 7)) & 1:
            out[i >> 3] |= 1 << (i & 7)
    for i in range(bit_offset + bit_len, n_bits):
        if (data[i >> 3] >> (i & 7)) & 1:
            j = i - bit_len
            out[j >> 3] |= 1 << (j & 7)
    return bytes(out)


def container_delete_bits(blob, bit_offset, bit_len, _depth=0):
    """Delete bits inside a CDemoPacket-style container (u_bit_var msg type +
    varint byte size + payload, repeated). Rewrites the target message's size
    and rebuilds the container bit-exactly (headers may be bit-aligned)."""
    if _depth > MAX_DEPTH:
        raise ProtoError("too deep")
    msgs = []
    br = BitReader(blob)
    while br.bits_left() >= 8:
        start = br.bit
        mt = br.read_u_bit_var()
        sz = br.read_varint()
        if sz == 0 or br.bit + sz * 8 > len(blob) * 8:
            raise ProtoError("oversized message -> not a container")
        hdr_bits = br.bit - start
        data = br.read_bytes(sz)
        msgs.append((mt, sz, start, hdr_bits, data))
    if not msgs:
        raise ProtoError("empty container")
    for i, (mt, sz, start, hdr_bits, data) in enumerate(msgs):
        data_start = start + hdr_bits
        if data_start <= bit_offset < data_start + sz * 8 and bit_offset + bit_len <= data_start + sz * 8:
            inner = bit_offset - data_start
            new_data = proto_delete_bits(data, inner, bit_len, _depth + 1)
            out = BitWriter()
            for j, (mt2, sz2, _s2, _h2, d2) in enumerate(msgs):
                if j == i:
                    out.write_u_bit_var(mt2)
                    out.write_varint(len(new_data))
                    out.write_bytes(new_data)
                else:
                    out.write_u_bit_var(mt2)
                    out.write_varint(sz2)
                    out.write_bytes(d2)
            return out.to_bytes()
    raise ProtoError("bit range not inside any container message")


def proto_delete_bits(data: bytes, bit_offset: int, bit_len: int, _depth: int = 0) -> bytes:
    """Delete bits at `bit_offset` (relative to `data`) from the innermost bytes
    field containing them, rewriting all enclosing length varints."""
    if _depth > MAX_DEPTH:
        raise ProtoError("too deep")
    try:
        fields = parse_fields(data)
    except ProtoError:
        # not a protobuf message -> CDemoPacket-style container or raw bit stream
        try:
            return container_delete_bits(data, bit_offset, bit_len, _depth)
        except ProtoError:
            return delete_bits_from_bitstream(data, bit_offset, bit_len)

    out_fields = []
    deleted = False
    for field_no, wt, value in fields:
        if wt == 2 and not deleted:
            start_byte = _field_value_start(data, fields, field_no, wt, value)
            # value occupies [start_byte, start_byte + len(value))
            v_start_bits = start_byte * 8
            v_end_bits = v_start_bits + len(value) * 8
            if v_start_bits <= bit_offset < v_end_bits and bit_offset + bit_len <= v_end_bits:
                new_value = proto_delete_bits(value, bit_offset - v_start_bits, bit_len, _depth + 1)
                out_fields.append((field_no, wt, new_value))
                deleted = True
                continue
        out_fields.append((field_no, wt, value))
    if not deleted:
        raise ProtoError("bit range not inside any bytes field")
    return encode_fields(out_fields)


def _field_value_start(data, fields, field_no, wt, value):
    """Byte offset where a given length-delimited field's value starts in `data`.

    Re-walks the fields, accumulating offsets (parse_fields does not track them).
    """
    pos = 0
    for fno, fwt, fval in fields:
        tag_len = _varint_len(data, pos)
        pos += tag_len
        if fwt == 0:
            _, pos = read_varint(data, pos)
        elif fwt == 2:
            ln, p2 = read_varint(data, pos)
            if fno == field_no and fwt == wt and fval == value:
                return p2
            pos = p2 + ln
        elif fwt == 1:
            pos += 8
        elif fwt == 5:
            pos += 4
        else:  # pragma: no cover
            raise ProtoError("bad wire type")
    raise ProtoError("field not found")


def _varint_len(data, pos):
    n = 0
    while True:
        if data[pos] & 0x80:
            n += 1
            pos += 1
        else:
            return n + 1
