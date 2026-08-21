# -*- coding: utf-8 -*-
"""Self-tests: build synthetic demos containing tags at every bit alignment
and verify the strip pipeline removes them without corrupting the container.

Run:  python tests.py
"""
from __future__ import annotations

import os
import random
import struct
import sys
import tempfile

import snappy
from demo_frames import COMPRESS_FLAG, MAGIC, DemoFile, patch_header_lengths, write_varint
from proto_utils import BitWriter
from tag_strip import DEFAULT_TAG, find_tag_positions, strip_file


def _bit(data: bytes, i: int) -> int:
    return (data[i >> 3] >> (i & 7)) & 1


def insert_bits(data: bytes, bit_off: int, insert: bytes) -> bytes:
    """Insert the bytes `insert` at bit offset `bit_off` (LSB-first)."""
    ins = [(b >> i) & 1 for b in insert for i in range(8)]
    total = len(data) * 8 + len(ins)
    out = bytearray((total + 7) // 8)
    for bit in range(total):
        if bit < bit_off:
            v = _bit(data, bit)
        elif bit - bit_off < len(ins):
            v = ins[bit - bit_off]
        else:
            v = _bit(data, bit - len(ins))
        if v:
            out[bit >> 3] |= 1 << (bit & 7)
    return bytes(out)


def random_stream(rng: random.Random, n: int) -> bytes:
    """Random bytes with a zero prefix, so the blob deterministically fails
    protobuf/container parsing and is treated as a raw bit stream."""
    return b"\x00" * 8 + bytes(rng.randrange(256) for _ in range(n))


def proto_wrap(inner: bytes) -> bytes:
    """Clean protobuf message: field 1 varint, field 2 bytes=inner, field 3 varint."""
    return b"\x08\x2a" + b"\x12" + write_varint(len(inner)) + inner + b"\x18\x07"


def container_wrap(inner: bytes) -> bytes:
    """CDemoPacket-style container: u_bit_var msg type + varint size + data, twice."""
    out = BitWriter()
    out.write_u_bit_var(3)
    out.write_varint(len(inner))
    out.write_bytes(inner)
    tail = b"\xde\xad\xbe\xef"
    out.write_u_bit_var(1)
    out.write_varint(len(tail))
    out.write_bytes(tail)
    return out.to_bytes()


def build_payload(rng: random.Random, structure: str, shift: int, tag: bytes) -> bytes:
    inner = insert_bits(random_stream(rng, 300), 41 + shift, tag + b"\x00")
    if structure == "proto":
        return proto_wrap(inner)
    if structure == "container":
        return container_wrap(inner)
    return inner


def make_frame(cmd, tick, payload, compress=True):
    data = snappy.compress(payload) if compress else payload
    c = cmd | COMPRESS_FLAG if compress else cmd
    return write_varint(c) + write_varint(tick) + write_varint(len(data)) + data


def make_demo(frames) -> bytes:
    header = MAGIC + struct.pack("<II", 0, 0)
    body = b"".join(frames)
    return patch_header_lengths(header, len(body)) + body


def run_strip(src_data, tmp, name, **kw):
    """Write `src_data` (bytes) or reuse `src_data` (path) as input, strip it.
    Returns ((n, removed, delta), out_blob, dst_path)."""
    if isinstance(src_data, bytes):
        src = os.path.join(tmp, name + "_in.dem")
        with open(src, "wb") as f:
            f.write(src_data)
    else:
        src = src_data
    dst = os.path.join(tmp, name + "_out.dem")
    result = strip_file(src, dst, **kw)
    with open(dst, "rb") as f:
        return result, f.read(), dst


def assert_tag_free(demo_path, tag):
    demo = DemoFile(demo_path)
    for fr in demo.frames:
        payload = demo.payload(fr)
        assert not find_tag_positions(payload, tag), f"tag still present in {demo_path}"


def test_roundtrip_no_tag(tmp):
    rng = random.Random(1)
    payload = random_stream(rng, 300)
    blob = make_demo([
        make_frame(7, 0, proto_wrap(payload), compress=True),
        make_frame(2, 1, payload, compress=True),
        make_frame(8, 2, payload, compress=False),
    ])
    (n, removed, delta), out, _ = run_strip(blob, tmp, "rt")
    assert (n, removed, delta) == (0, 0, 0), (n, removed, delta)
    assert out == blob, "tag-free demo must round-trip byte-identically"


def test_removal_structures(tmp, structure):
    for shift in range(8):
        rng = random.Random(100 + shift)
        payload = build_payload(rng, structure, shift, DEFAULT_TAG)
        assert find_tag_positions(payload, DEFAULT_TAG), "fixture must contain the tag"
        blob = make_demo([
            make_frame(7, 0, payload, compress=True),
            make_frame(2, 1, b"\x00" * 40, compress=True),
        ])
        (n, removed, _), _, dst = run_strip(blob, tmp, f"{structure}{shift}")
        assert n == 1, (structure, shift, "frames_modified", n)
        assert removed == 1, (structure, shift, "tags_removed", removed)
        demo = DemoFile(dst)
        assert len(demo.frames) == 2, (structure, shift, "frame count changed")
        assert_tag_free(dst, DEFAULT_TAG)


def test_deep_mode(tmp):
    rng = random.Random(7)
    blob = make_demo([
        # tag inside a compressed Packet frame: only found in deep mode
        make_frame(2, 0, build_payload(rng, "proto", 5, DEFAULT_TAG), compress=True),
        # tag inside an uncompressed frame: also deep-mode only
        make_frame(2, 1, build_payload(rng, "raw", 2, DEFAULT_TAG), compress=False),
    ])
    (n, removed, delta), out, _ = run_strip(blob, tmp, "deep_off")
    assert (n, removed, delta) == (0, 0, 0), (n, removed, delta)
    assert out == blob, "default mode must not touch non-entity frames"
    (n, removed, _), _, dst = run_strip(blob, tmp, "deep_on", deep=True)
    assert n == 2 and removed == 2, (n, removed)
    assert_tag_free(dst, DEFAULT_TAG)


def test_custom_tags(tmp):
    rng = random.Random(9)
    inner = insert_bits(random_stream(rng, 400), 53, b"MYTAG\x00")
    inner = insert_bits(inner, 700, DEFAULT_TAG + b"\x00")
    blob = make_demo([make_frame(7, 0, proto_wrap(inner), compress=True)])
    # remove only the custom tag
    (n, removed, _), _, dst = run_strip(blob, tmp, "tag1", tags=[b"MYTAG"])
    assert n == 1 and removed == 1, (n, removed)
    demo = DemoFile(dst)
    payload = demo.payload(demo.frames[0])
    assert not find_tag_positions(payload, b"MYTAG")
    assert find_tag_positions(payload, DEFAULT_TAG), "default tag must survive"
    # then remove the default tag from the already-stripped file
    (n, removed, _), _, dst2 = run_strip(dst, tmp, "tag2", tags=[DEFAULT_TAG])
    assert n == 1 and removed == 1, (n, removed)
    assert_tag_free(dst2, DEFAULT_TAG)


def test_multiple_occurrences(tmp):
    rng = random.Random(11)
    inner = random_stream(rng, 600)
    for off in (100, 400, 800):  # bit offsets inside the container message data
        inner = insert_bits(inner, off, DEFAULT_TAG + b"\x00")
    payload = container_wrap(inner)
    assert len(find_tag_positions(payload, DEFAULT_TAG)) == 3
    blob = make_demo([make_frame(13, 0, payload, compress=True)])
    (n, removed, _), _, dst = run_strip(blob, tmp, "multi")
    assert n == 1 and removed == 3, (n, removed)
    assert_tag_free(dst, DEFAULT_TAG)


def main():
    tests = [
        ("roundtrip_no_tag", test_roundtrip_no_tag),
        ("proto_all_bit_shifts", lambda tmp: test_removal_structures(tmp, "proto")),
        ("container_all_bit_shifts", lambda tmp: test_removal_structures(tmp, "container")),
        ("raw_all_bit_shifts", lambda tmp: test_removal_structures(tmp, "raw")),
        ("deep_mode", test_deep_mode),
        ("custom_tags", test_custom_tags),
        ("multiple_occurrences", test_multiple_occurrences),
    ]
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, fn in tests:
            try:
                fn(tmp)
                print(f"PASS  {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"ERROR {name}: {exc!r}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print("\nall tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
