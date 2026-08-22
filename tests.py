# -*- coding: utf-8 -*-
"""Self-tests: build synthetic CS2 demos whose svc_PacketEntities messages look
like the real thing (delta headers + field data + serialized_entities size
table) and verify the strip pipeline removes tags without desynchronising the
entity stream.

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
from entity_data import (CREATE_HEADER_BITS, F_ENTITY_DATA, F_SERIALIZED_ENTITIES,
                         F_UPDATED_ENTRIES, PACKET_ENTITIES, entity_spans,
                         read_size_table, write_size_table)
from proto_utils import (BitReader, BitWriter, delete_bits_from_bitstream,
                         encode_fields, find_bit_pattern, parse_container,
                         parse_fields)
from tag_strip import strip_file

#: Fixture texts to remove. Any two distinct non-empty byte strings work.
TAG = b"SAMPLE NAME TAG"
OTHER_TAG = b"Second Sample"


def _rand_bits(writer, rng, n):
    if n:
        writer.write(rng.getrandbits(n), n)


def build_entity_data(rng, specs):
    """Build a plausible ``entity_data`` bit stream.

    `specs` is a list of (index_delta, kind, prefix_bits, tags, suffix_bits) with
    kind in {"create", "update", "leave"}. Every tag in `tags` is embedded in the
    entity's field data followed by its NUL terminator.

    Returns (blob, sizes, tag_offsets) where tag_offsets holds one (bit_offset,
    tag) pair per embedded tag.
    """
    out = BitWriter()
    sizes = []
    tag_offsets = []
    for delta, kind, prefix, tags, suffix in specs:
        out.write_u_bit_var(delta)
        if kind == "leave":
            out.write(1, 1)
            out.write(rng.randrange(2), 1)
            continue
        out.write(0, 1)
        out.write(1 if kind == "create" else 0, 1)
        if kind == "create":
            _rand_bits(out, rng, CREATE_HEADER_BITS)
        start = out.bit_len
        _rand_bits(out, rng, prefix)
        for k, tag in enumerate(tags):
            if k:
                _rand_bits(out, rng, 24)
            tag_offsets.append((out.bit_len, tag))
            out.write_bytes(tag + b"\x00")
        _rand_bits(out, rng, suffix)
        sizes.append(out.bit_len - start)
    _rand_bits(out, rng, (-out.bit_len) % 8)
    return out.to_bytes(), sizes, tag_offsets


def build_packet_entities(entity_data, sizes, updated_entries, with_table=True):
    """Encode a CSVCMsg_PacketEntities carrying `entity_data`."""
    fields = [(1, 0, 512), (F_UPDATED_ENTRIES, 0, updated_entries), (3, 0, 1),
              (F_ENTITY_DATA, 2, entity_data)]
    if with_table:
        fields.append((F_SERIALIZED_ENTITIES, 2, write_size_table(sizes)))
    fields.append((21, 0, 0))
    return encode_fields(fields)


def build_container(rng, messages):
    """Bit-pack container messages and fill the trailing padding with ones, so
    that losing those bits on rebuild is detectable."""
    out = BitWriter()
    for msg_type, payload in messages:
        out.write_u_bit_var(msg_type)
        out.write_varint(len(payload))
        out.write_bytes(payload)
    pad = (-out.bit_len) % 8
    out.write((1 << pad) - 1, pad)
    return out.to_bytes(), pad


def build_demo_packet(container):
    """CDemoPacket { flags = 1; data = 3 }."""
    return encode_fields([(1, 0, 0), (3, 2, container)])


def build_full_packet(container, string_tables):
    """CDemoFullPacket { string_table = 1; packet = 2 }."""
    return encode_fields([(1, 2, string_tables), (2, 2, build_demo_packet(container))])


def make_frame(cmd, tick, payload, compress=True):
    data = snappy.compress(payload) if compress else payload
    if compress:
        cmd |= COMPRESS_FLAG
    return write_varint(cmd) + write_varint(tick) + write_varint(len(data)) + data


def make_demo(frames):
    header = MAGIC + struct.pack("<II", 0, 0)
    body = b"".join(frames)
    return patch_header_lengths(header, len(body)) + body


def run_strip(src_data, tmp, name, **kw):
    """Strip `src_data` (bytes blob, or an existing path) into a temp file.
    Returns ((modified, removed, delta, left), out_blob, dst_path)."""
    kw.setdefault("tags", [TAG])
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


def demo_container(path, frame_index, full_packet=False):
    """Pull the container of one frame back out of a written demo."""
    demo = DemoFile(path)
    payload = demo.payload(demo.frames[frame_index])
    if full_packet:
        payload = dict((f, v) for f, _w, v in parse_fields(payload))[2]
    container = dict((f, v) for f, _w, v in parse_fields(payload))[3]
    return parse_container(container)


def packet_entities_of(messages):
    for msg_type, payload in messages:
        if msg_type == PACKET_ENTITIES:
            return dict((f, v) for f, _w, v in parse_fields(payload))
    raise AssertionError("no svc_PacketEntities in container")


def assert_tag_free(path, tag):
    demo = DemoFile(path)
    for frame in demo.frames:
        assert not find_bit_pattern(demo.payload(frame), tag), f"tag still in {path}"


def scenario(rng, shift=0, tagged=((3, (TAG,)),), with_table=True):
    """A container holding one svc_PacketEntities among two other messages.

    `tagged` maps entity position -> tags to embed. `shift` moves everything
    after the first entity, so the tag lands on a different bit alignment.
    Returns (container, sizes, messages, pad, offsets).
    """
    layout = [(0, "create", 61 + shift), (3, "update", 17), (1, "leave", 0),
              (5, "create", 23), (2, "update", 40)]
    tags_at = dict(tagged)
    specs = [(delta, kind, prefix, tags_at.get(i, ()), 90 + 30 * i)
             for i, (delta, kind, prefix) in enumerate(layout)]
    entity_data, sizes, offsets = build_entity_data(rng, specs)
    message = build_packet_entities(entity_data, sizes, len(specs), with_table)
    messages = [(40, bytes(rng.randrange(256) for _ in range(37))),
                (PACKET_ENTITIES, message),
                (7, b"\x01\x02\x03")]
    container, pad = build_container(rng, messages)
    return container, sizes, messages, pad, offsets


def check_container(dst, frame_index, messages, pad, full_packet=False):
    """Verify padding bits and untouched messages survived the rebuild."""
    rebuilt, tail, tail_bits = demo_container(dst, frame_index, full_packet)
    assert tail_bits == pad, (tail_bits, pad)
    assert tail == (1 << pad) - 1, "container trailing padding bits were lost"
    assert [m for m, _ in rebuilt] == [m for m, _ in messages], "message order changed"
    for (msg_type, new), (_mt, old) in zip(rebuilt, messages):
        if msg_type != PACKET_ENTITIES:
            assert new == old, f"container message {msg_type} was modified"
    return packet_entities_of(rebuilt)


def check_size_table(fields, sizes, shrunk):
    """`shrunk` maps size-table slot -> bits removed."""
    new_sizes = read_size_table(fields[F_SERIALIZED_ENTITIES])
    expect = [size - shrunk.get(slot, 0) for slot, size in enumerate(sizes)]
    assert new_sizes == expect, (new_sizes, expect)
    spans = entity_spans(fields[F_ENTITY_DATA], new_sizes, fields[F_UPDATED_ENTRIES])
    assert len(spans) == len(new_sizes)


def check_entity_data(fields, messages, offsets, tags):
    """The rebuilt ``entity_data`` must be the original with exactly the fixture's
    tag bits cut out -- catches a deletion applied at the wrong bit offset, which
    a "the tag is no longer findable" check happily accepts."""
    expect = packet_entities_of(messages)[F_ENTITY_DATA]
    for offset, tag in sorted(offsets, reverse=True):
        if tag in tags:
            expect = delete_bits_from_bitstream(expect, offset, len(tag) * 8)
    assert fields[F_ENTITY_DATA] == expect, "entity_data was cut at the wrong offset"


TAG_BITS = len(TAG) * 8


def test_u_bit_var_roundtrip(_tmp):
    for value in (0, 1, 15, 16, 17, 255, 256, 271, 272, 4095, 4096, 100000, 2 ** 32 - 1):
        out = BitWriter()
        out.write_u_bit_var(value)
        out.write(0, 8)  # padding so the reader never runs off the end
        assert BitReader(out.to_bytes()).read_u_bit_var() == value, value


def test_size_table_roundtrip(_tmp):
    sizes = [0, 1, 127, 128, 3487, 65535, 200000]
    assert read_size_table(write_size_table(sizes)) == sizes


def test_roundtrip_no_tag(tmp):
    rng = random.Random(1)
    container = scenario(rng, tagged=())[0]
    blob = make_demo([
        make_frame(1, 0, b"\x08\x01", compress=False),
        make_frame(7, 1, build_demo_packet(container), compress=True),
        make_frame(8, 2, build_demo_packet(container), compress=False),
    ])
    result, out, _dst = run_strip(blob, tmp, "rt")
    assert result == (0, 0, 0, 0), result
    assert out == blob, "a tag-free demo must round-trip byte-identically"


def test_alignments(tmp):
    alignments = set()
    for shift in range(8):
        rng = random.Random(200 + shift)
        container, sizes, messages, pad, offsets = scenario(rng, shift)
        payload = build_demo_packet(container)
        hits = find_bit_pattern(payload, TAG)
        assert len(hits) == 1, (shift, "fixture must contain the tag exactly once")
        alignments.add(hits[0] % 8)
        blob = make_demo([
            make_frame(1, 0, b"\x08\x01", compress=False),
            make_frame(7, 1, payload, compress=True),
            make_frame(4, 2, b"\x0a\x03abc", compress=True),
        ])
        (n, removed, delta, left), _out, dst = run_strip(blob, tmp, f"align{shift}")
        assert (n, removed, left) == (1, 1, 0), (shift, n, removed, left)
        assert delta < 0, (shift, delta)
        assert_tag_free(dst, TAG)
        fields = check_container(dst, 1, messages, pad)
        check_size_table(fields, sizes, {2: TAG_BITS})
        check_entity_data(fields, messages, offsets, (TAG,))
        demo = DemoFile(dst)
        assert len(demo.frames) == 3, (shift, "frame count changed")
        assert demo.payload(demo.frames[0]) == b"\x08\x01"
        assert demo.payload(demo.frames[2]) == b"\x0a\x03abc"
    assert alignments == set(range(8)), f"only covered bit alignments {sorted(alignments)}"


def test_full_packet(tmp):
    rng = random.Random(31)
    container, sizes, messages, pad, offsets = scenario(rng, shift=3)
    string_tables = b"\x0a\x05hello"
    blob = make_demo([make_frame(13, 0, build_full_packet(container, string_tables))])
    (n, removed, delta, left), _out, dst = run_strip(blob, tmp, "full")
    assert (n, removed, left) == (1, 1, 0), (n, removed, left)
    assert_tag_free(dst, TAG)
    fields = check_container(dst, 0, messages, pad, full_packet=True)
    check_size_table(fields, sizes, {2: TAG_BITS})
    check_entity_data(fields, messages, offsets, (TAG,))
    demo = DemoFile(dst)
    top = dict((f, v) for f, _w, v in parse_fields(demo.payload(demo.frames[0])))
    assert top[1] == string_tables, "CDemoFullPacket.string_table must survive untouched"


def test_uncompressed_frame(tmp):
    """Regression: uncompressed entity frames used to be skipped entirely."""
    rng = random.Random(41)
    container, sizes, messages, pad, offsets = scenario(rng, shift=5)
    blob = make_demo([make_frame(7, 0, build_demo_packet(container), compress=False)])
    (n, removed, _delta, left), _out, dst = run_strip(blob, tmp, "raw")
    assert (n, removed, left) == (1, 1, 0), (n, removed, left)
    assert not DemoFile(dst).frames[0].compressed, "must stay uncompressed"
    assert_tag_free(dst, TAG)
    fields = check_container(dst, 0, messages, pad)
    check_size_table(fields, sizes, {2: TAG_BITS})
    check_entity_data(fields, messages, offsets, (TAG,))


def test_multiple_tags(tmp):
    """Three occurrences across two entities: each size-table entry must shrink."""
    rng = random.Random(53)
    container, sizes, messages, pad, offsets = scenario(
        rng, shift=2, tagged=((1, (TAG,)), (3, (TAG, TAG))))
    blob = make_demo([make_frame(7, 0, build_demo_packet(container), compress=True)])
    (n, removed, _delta, left), _out, dst = run_strip(blob, tmp, "multi")
    assert (n, removed, left) == (1, 3, 0), (n, removed, left)
    assert_tag_free(dst, TAG)
    fields = check_container(dst, 0, messages, pad)
    check_size_table(fields, sizes, {1: TAG_BITS, 2: 2 * TAG_BITS})
    check_entity_data(fields, messages, offsets, (TAG,))


def test_without_size_table(tmp):
    """Skin-changer demos drop serialized_entities; plain deletion is correct there."""
    rng = random.Random(61)
    container, _sizes, messages, pad, offsets = scenario(rng, shift=6, with_table=False)
    original = packet_entities_of(messages)[F_ENTITY_DATA]
    blob = make_demo([make_frame(7, 0, build_demo_packet(container), compress=True)])
    (n, removed, _delta, left), _out, dst = run_strip(blob, tmp, "notable")
    assert (n, removed, left) == (1, 1, 0), (n, removed, left)
    assert_tag_free(dst, TAG)
    fields = check_container(dst, 0, messages, pad)
    assert F_SERIALIZED_ENTITIES not in fields, "no size table must be invented"
    assert len(fields[F_ENTITY_DATA]) == len(original) - len(TAG)
    check_entity_data(fields, messages, offsets, (TAG,))


def test_tag_outside_entity_data(tmp):
    """A tag that is not inside an entity's field data must be left alone."""
    rng = random.Random(77)
    entity_data, sizes, _offsets = build_entity_data(rng, [(0, "create", 40, (), 300)])
    message = build_packet_entities(entity_data, sizes, 1)
    other = b"\x0a" + write_varint(len(TAG)) + TAG
    container, _pad = build_container(rng, [(40, other), (PACKET_ENTITIES, message)])
    payload = build_demo_packet(container)
    assert len(find_bit_pattern(payload, TAG)) == 1
    blob = make_demo([make_frame(7, 0, payload, compress=True)])
    result, out, _dst = run_strip(blob, tmp, "outside")
    assert result == (0, 0, 0, 1), result
    assert out == blob, "the demo must be byte-identical when nothing is removed"


def test_broken_size_table(tmp):
    """A size table that does not match the stream must abort the frame, not corrupt it."""
    rng = random.Random(83)
    entity_data, sizes, _offsets = build_entity_data(
        rng, [(0, "create", 40, (TAG,), 300), (2, "update", 24, (), 120)])
    for broken in (lambda t: [t[0] + 4096, t[1]],  # first entity runs off the end
                   lambda t: t[:1]):               # one entry short
        message = build_packet_entities(entity_data, broken(list(sizes)), 2)
        container, _pad = build_container(rng, [(PACKET_ENTITIES, message)])
        blob = make_demo([make_frame(7, 0, build_demo_packet(container), compress=True)])
        result, out, _dst = run_strip(blob, tmp, "broken")
        assert result == (0, 0, 0, 1), result
        assert out == blob, "an unparseable frame must be left byte-identical"


def test_custom_tags(tmp):
    rng = random.Random(97)
    container, sizes, messages, pad, offsets = scenario(
        rng, shift=1, tagged=((1, (OTHER_TAG,)), (3, (TAG,))))
    blob = make_demo([make_frame(13, 0, build_full_packet(container, b"\x0a\x02hi"))])
    (n, removed, _delta, left), _out, dst = run_strip(blob, tmp, "tag1", tags=[OTHER_TAG])
    assert (n, removed, left) == (1, 1, 0), (n, removed, left)
    fields = check_container(dst, 0, messages, pad, full_packet=True)
    check_size_table(fields, sizes, {1: len(OTHER_TAG) * 8})
    check_entity_data(fields, messages, offsets, (OTHER_TAG,))
    assert_tag_free(dst, OTHER_TAG)
    demo = DemoFile(dst)
    assert find_bit_pattern(demo.payload(demo.frames[0]), TAG), \
        "the other tag must survive"
    (n, removed, _delta, left), _out, dst2 = run_strip(dst, tmp, "tag2", tags=[TAG])
    assert (n, removed, left) == (1, 1, 0), (n, removed, left)
    assert_tag_free(dst2, TAG)


def test_no_text_given(tmp):
    """Removing nothing is a mistake, not a silent no-op."""
    src = os.path.join(tmp, "notext_in.dem")
    with open(src, "wb") as f:
        f.write(make_demo([make_frame(1, 0, b"\x08\x01", compress=False)]))
    for tags in ([], [b""], [""]):
        try:
            strip_file(src, os.path.join(tmp, "notext_out.dem"), tags=tags)
        except ValueError:
            continue
        raise AssertionError(f"empty tag list must raise: {tags!r}")


TESTS = [
    test_u_bit_var_roundtrip,
    test_size_table_roundtrip,
    test_no_text_given,
    test_roundtrip_no_tag,
    test_alignments,
    test_full_packet,
    test_uncompressed_frame,
    test_multiple_tags,
    test_without_size_table,
    test_tag_outside_entity_data,
    test_broken_size_table,
    test_custom_tags,
]


def main():
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for test in TESTS:
            name = test.__name__
            try:
                test(tmp)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
            else:
                print(f"ok   {name}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
