# -*- coding: utf-8 -*-
"""CS2 (Source 2 / valve_demo_2) demo container parsing & rebuilding.

File layout:
  [0x00..0x08)  magic "PBDEMS2\\0"
  [0x08..0x0C)  u32  file-length-related field
  [0x0C..0x10)  u32  second file-length-related field
  [0x10..)      frame stream: varint(cmd) varint(tick) varint(size) data[size]
                cmd bit 6 (0x40) = compressed (snappy), low 6 bits = EDemoCommands
  (no directory table at EOF; the stream is read sequentially to the end --
   DEM_Stop is not a reliable terminator, real frames follow it)
"""
from __future__ import annotations

import struct

import snappy

MAGIC = b"PBDEMS2\x00"
HEADER_LEN = 16

COMPRESS_FLAG = 0x40


class DemoFormatError(Exception):
    pass


def read_varint(data, ptr):
    shift = 0
    val = 0
    while True:
        if ptr >= len(data):
            raise DemoFormatError("varint out of bounds")
        b = data[ptr]
        ptr += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise DemoFormatError("varint too long")
    return val, ptr


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


class Frame:
    """One demo frame. `data is None` means "keep original bytes on rebuild"."""

    __slots__ = ("cmd", "tick", "size", "data_off", "data", "recompress", "head_len")

    def __init__(self, cmd, tick, size, data_off, head_len=0):
        self.cmd = cmd
        self.tick = tick
        self.size = size
        self.data_off = data_off
        self.data = None
        self.recompress = False
        self.head_len = head_len

    @property
    def msg_type(self):
        return self.cmd & ~COMPRESS_FLAG

    @property
    def compressed(self):
        return bool(self.cmd & COMPRESS_FLAG)


class DemoFile:
    def __init__(self, path):
        self.path = path
        self.frames = []
        self.header = None
        self._data = None
        self._read()

    def _read(self):
        with open(self.path, "rb") as f:
            blob = f.read()
        if len(blob) < HEADER_LEN or blob[:8] != MAGIC:
            raise DemoFormatError("not a CS2 demo file")
        self.header = blob[:HEADER_LEN]
        self._data = blob
        ptr = HEADER_LEN
        n = len(blob)
        while ptr < n:
            start = ptr
            cmd, ptr = read_varint(blob, ptr)
            tick, ptr = read_varint(blob, ptr)
            size, ptr = read_varint(blob, ptr)
            if size < 0 or ptr + size > n:
                raise DemoFormatError(f"bad frame size {size} at {ptr:#x}")
            self.frames.append(Frame(cmd, tick, size, ptr, head_len=ptr - start))
            ptr += size

    def payload(self, frame) -> bytes:
        """Return frame payload bytes (decompressed if the frame is compressed)."""
        raw = self._data[frame.data_off:frame.data_off + frame.size]
        if not frame.compressed:
            return raw
        return snappy.decompress(raw)

    def set_payload(self, frame, payload: bytes, recompress=True):
        """Replace a frame's payload with `payload`.

        The caller passes *uncompressed* data; if the frame was compressed the
        payload is snappy-compressed again. With `recompress=False` the payload
        is treated as the final raw bytes.
        """
        if frame.compressed and recompress:
            payload = snappy.compress(payload)
        frame.data = payload
        frame.recompress = True

    def rebuild(self) -> tuple[bytes, int]:
        """Serialize the demo back to bytes.

        Returns (blob, delta) where delta = new_size - original_size; the header
        length fields are updated by delta.
        """
        parts = []
        delta = HEADER_LEN - len(self._data)
        for fr in self.frames:
            if fr.data is None:
                raw = self._data[fr.data_off - fr.head_len:fr.data_off + fr.size]
            else:
                payload = fr.data
                cmd = fr.cmd
                if fr.recompress and fr.compressed:
                    cmd |= COMPRESS_FLAG
                elif fr.recompress:
                    cmd &= ~COMPRESS_FLAG
                head = write_varint(cmd) + write_varint(fr.tick) + write_varint(len(payload))
                raw = head + payload
            delta += len(raw)
            parts.append(raw)
        blob = patch_header_lengths(self.header, delta) + b"".join(parts)
        return blob, delta

    def write(self, out_path: str) -> int:
        blob, delta = self.rebuild()
        with open(out_path, "wb") as f:
            f.write(blob)
        return delta


def patch_header_lengths(header: bytes, delta: int) -> bytes:
    """Add `delta` to the two u32 length fields at 0x08 and 0x0C."""
    h = bytearray(header[:HEADER_LEN])
    for off in (0x08, 0x0C):
        v = struct.unpack_from("<I", h, off)[0]
        struct.pack_into("<I", h, off, (v + delta) & 0xFFFFFFFF)
    return bytes(h)
