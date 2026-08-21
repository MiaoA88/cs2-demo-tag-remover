# -*- coding: utf-8 -*-
"""Remove name tags from a CS2 (Source 2) demo file.

Principle
---------
Some skin tools write a fixed custom name (by default "CS2 INSIGHT AGENT",
see DEFAULT_TAG) into the item entity data of a demo. The name tag lives in
entity *bit streams* (ClassInfo / instance baselines), so it is usually NOT
byte-aligned.

Search strategy
---------------
For a tag starting at bit offset `B` (B % 8 = s) inside the stream, the bytes
*after* the first partial byte are fully determined by the tag itself (no
dependency on preceding fields). So for each s in 0..7 we build the byte
pattern of the tag bit stream starting at bit (8 - s) and use C-level
`bytes.find`. Every hit is then verified bit-by-bit (the first partial byte
is checked here).

Deletion strategy
-----------------
The tag bytes are *deleted* from the bit stream (the NUL terminator stays), so
the custom name decodes to the empty string.  This matters because CS2 draws
quotes around *non-empty* custom names -- replacing the tag with spaces makes
the game show `"   "`.  An empty name renders as nothing at all.

Deleting bits shrinks the payload, so every enclosing structure is rewritten:
  1. the innermost entity bit stream (bits shifted left),
  2. protobuf length varints of enclosing messages,
  3. the CDemoPacket-style container around entity data
     (msg type u_bit_var + byte-size varint per message) -- the size of the
     affected message is decremented.
"""
from __future__ import annotations

from demo_frames import DemoFile
from proto_utils import ProtoError, proto_delete_bits

#: Tag text removed by default (bytes; the NUL terminator that follows it in
#: the bit stream is kept, so the custom name decodes to an empty string).
DEFAULT_TAG = b"CS2 INSIGHT AGENT"

#: Frame types whose payloads carry entity data. The tag has only ever been
#: observed there, so the fast default mode scans just these frame types.
ENTITY_FRAME_TYPES = frozenset({7, 8, 13})


def _pattern_bits(pattern: bytes):
    """LSB-first bit list of the pattern."""
    bits = []
    for b in pattern:
        for i in range(8):
            bits.append((b >> i) & 1)
    return bits


def _suffix_patterns(pattern: bytes):
    """Return 8 (shift, suffix_bytes) pairs.

    If the tag starts at bit offset B with B % 8 == s, the payload bytes at
    positions (B//8 + 1) .. (B//8 + len(suffix)) equal `suffix_bytes` -- and
    crucially those bytes depend only on the tag, not on preceding fields.
    """
    pb = _pattern_bits(pattern)
    out = []
    for s in range(8):
        bits = pb[8 - s:]  # skip the first (8 - s) bits of the tag
        nbytes = (len(bits) + 7) // 8
        buf = bytearray(nbytes)
        for k, b in enumerate(bits):
            if b:
                buf[k >> 3] |= 1 << (k & 7)
        out.append((s, bytes(buf)))
    return out


def _verify_full(data, start_bit, pat_bits):
    n = len(data)
    if start_bit < 0 or start_bit + len(pat_bits) > n * 8:
        return False
    for k, pb in enumerate(pat_bits):
        bit = start_bit + k
        if ((data[bit >> 3] >> (bit & 7)) & 1) != pb:
            return False
    return True


def find_tag_positions(data: bytes, pattern: bytes) -> list[int]:
    """Return a list of bit offsets where `pattern` occurs in `data`
    (arbitrary bit alignment, fast C-level suffix search + bit verify)."""
    pat_bits = _pattern_bits(pattern)
    hits = []
    for s, suffix in _suffix_patterns(pattern):
        pos = 0
        while True:
            idx = data.find(suffix, pos)
            if idx < 0:
                break
            start_bit = idx * 8 - (8 - s)
            if _verify_full(data, start_bit, pat_bits):
                hits.append(start_bit)
            pos = idx + 1
    return hits


def strip_name_tags(demo: DemoFile, tags=(DEFAULT_TAG,), deep=False, progress=None) -> tuple[int, int]:
    """Delete every occurrence of any of `tags` from the demo frames.

    The tag bytes are *deleted* from the bit stream (the NUL terminator stays),
    so the custom name decodes to the empty string and CS2 does not draw the
    name (nor the quotes it renders around non-empty names).

    With ``deep=False`` only compressed entity-data frames are scanned (fast;
    the tags have only ever been observed there). With ``deep=True`` every
    frame is scanned, compressed or not.

    Returns (frames_modified, tags_removed). Frames are modified in place;
    call ``demo.write()`` afterwards.
    """
    tags = [t if isinstance(t, bytes) else str(t).encode("utf-8") for t in tags]
    tags = [t for t in tags if t]
    modified = 0
    removed = 0
    frames = demo.frames
    total = len(frames)
    for i, fr in enumerate(frames):
        if progress and i % 2000 == 0:
            progress(i, total)
        if not deep and (not fr.compressed or fr.msg_type not in ENTITY_FRAME_TYPES):
            continue
        try:
            payload = bytearray(demo.payload(fr))
        except Exception:
            continue
        # collect (start_bit, bit_len) hits of all tags in this frame
        hits = []
        for tag in tags:
            hits.extend((start_bit, len(tag) * 8)
                        for start_bit in find_tag_positions(bytes(payload), tag))
        if not hits:
            continue
        # delete later occurrences first so earlier bit offsets stay valid;
        # drop hits overlapping an already-scheduled (later) range
        hits.sort(reverse=True)
        scheduled = []
        prev_start = None
        for start_bit, bit_len in hits:
            if prev_start is not None and start_bit + bit_len > prev_start:
                continue
            scheduled.append((start_bit, bit_len))
            prev_start = start_bit
        frame_removed = 0
        for start_bit, bit_len in scheduled:
            try:
                payload = bytearray(proto_delete_bits(bytes(payload), start_bit, bit_len))
            except ProtoError:
                if not deep:
                    raise
                break  # structure not understood: leave this frame untouched
            frame_removed += 1
        if frame_removed < len(scheduled):
            continue
        removed += frame_removed
        demo.set_payload(fr, bytes(payload))
        modified += 1
    return modified, removed


def strip_file(src: str, dst: str, tags=(DEFAULT_TAG,), deep=False, progress=None) -> tuple[int, int, int]:
    """Convenience: load src, strip tags, write dst.

    Returns (frames_modified, tags_removed, size_delta).
    """
    demo = DemoFile(src)
    n, removed = strip_name_tags(demo, tags=tags, deep=deep, progress=progress)
    delta = demo.write(dst)
    return n, removed, delta
