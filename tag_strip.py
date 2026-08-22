# -*- coding: utf-8 -*-
"""Remove name tags from a CS2 (Source 2) demo file.

Where the tag lives
-------------------
A custom item name (``m_szCustomName``) is a NUL-terminated string inside
``CSVCMsg_PacketEntities.entity_data``, an unaligned entity bit stream nested
like this::

    DEM_Packet / DEM_SignonPacket -> CDemoPacket.data     -> container
    DEM_FullPacket -> CDemoFullPacket.packet -> .data     -> container
    container -> svc_PacketEntities (55) -> entity_data (field 7)

Search strategy
---------------
For a tag starting at bit offset ``B`` (``B % 8 == s``) the whole bytes *inside*
the tag are fully determined by the tag itself, so for each of the 8 alignments
a byte pattern is built and located with C-level ``bytes.find``; every candidate
is then verified bit by bit (:func:`proto_utils.pattern_hits`). The search runs
on ``entity_data`` itself, so the offsets it yields address the entity stream
directly.

Deletion strategy
-----------------
The tag bytes are *deleted* and the NUL terminator kept, so the name decodes to
the empty string -- CS2 draws quotes around non-empty names, so padding with
spaces would show ``"   "`` instead of nothing.

Deleting bits shrinks every enclosing structure, all of which are rewritten:
the entity's ``serialized_entities`` bit length, the protobuf length varints,
and the container's per-message byte-size varint (its trailing padding bits are
preserved).
"""
from __future__ import annotations

from demo_frames import DemoFile
from entity_data import PACKET_ENTITIES, strip_entity_data
from proto_utils import (ProtoError, encode_container, encode_fields,
                         parse_container, parse_fields, pattern_hits)

#: DEM_Packet, DEM_SignonPacket, DEM_FullPacket -- the only frames that carry a
#: ``CDemoPacket`` and therefore entity data.
ENTITY_FRAME_TYPES = frozenset({7, 8, 13})
FULL_PACKET = 13

#: ``CDemoPacket.data`` and ``CDemoFullPacket.packet``.
F_PACKET_DATA = 3
F_FULLPACKET_PACKET = 2


def _strip_container(blob, tags):
    """Strip tags from every ``svc_PacketEntities`` in a ``CDemoPacket.data`` blob."""
    messages, tail, tail_bits = parse_container(blob)
    removed = 0
    out = []
    for msg_type, payload in messages:
        if msg_type == PACKET_ENTITIES:
            payload, n = strip_entity_data(payload, tags)
            removed += n
        out.append((msg_type, payload))
    if not removed:
        return blob, 0
    return encode_container(out, tail, tail_bits), removed


def _strip_nested(message, field_no, strip, tags):
    """Apply `strip` to the first occurrence of length-delimited field `field_no`."""
    fields = parse_fields(message)
    removed = 0
    out = []
    for fno, wt, value in fields:
        if fno == field_no and wt == 2 and not removed:
            value, removed = strip(value, tags)
        out.append((fno, wt, value))
    if not removed:
        return message, 0
    return encode_fields(out), removed


def strip_frame_payload(payload: bytes, msg_type: int, tags) -> tuple[bytes, int]:
    """Strip tags from one decompressed entity frame. Returns (payload, removed).

    Raises ProtoError when the frame's structure does not parse; the caller then
    leaves the frame untouched.
    """
    if msg_type == FULL_PACKET:
        return _strip_nested(
            payload, F_FULLPACKET_PACKET,
            lambda packet, t: _strip_nested(packet, F_PACKET_DATA, _strip_container, t),
            tags)
    return _strip_nested(payload, F_PACKET_DATA, _strip_container, tags)


def strip_name_tags(demo: DemoFile, tags, progress=None) -> tuple[int, int, int]:
    """Delete every occurrence of any of `tags` from the demo's entity frames.

    `tags` is the text to remove, as ``bytes`` or ``str``; at least one non-empty
    entry is required. The NUL terminator that follows a name in the bit stream
    is kept, so the custom name decodes to an empty string.

    Returns (frames_modified, tags_removed, tags_left). ``tags_left`` counts
    occurrences that were found but deliberately not touched -- a frame whose
    structure did not parse, or a tag sitting outside an entity's field data.
    Frames are modified in place; call ``demo.write()`` afterwards.
    """
    tags = [t if isinstance(t, bytes) else str(t).encode("utf-8") for t in tags]
    tags = [t for t in tags if t]
    if not tags:
        raise ValueError("no text to remove was given")
    modified = removed = left = 0
    frames = demo.frames
    total = len(frames)
    for i, frame in enumerate(frames):
        if progress and i % 2000 == 0:
            progress(i, total)
        if frame.msg_type not in ENTITY_FRAME_TYPES:
            continue
        payload = demo.payload(frame)
        found = len(pattern_hits(payload, tags))
        if not found:
            continue
        try:
            new_payload, n = strip_frame_payload(payload, frame.msg_type, tags)
        except ProtoError:
            left += found
            continue
        left += found - n
        if not n:
            continue
        demo.set_payload(frame, new_payload)
        modified += 1
        removed += n
    return modified, removed, left


def strip_file(src: str, dst: str, tags, progress=None) -> tuple[int, int, int, int]:
    """Convenience: load src, strip tags, write dst.

    Returns (frames_modified, tags_removed, size_delta, tags_left).
    """
    demo = DemoFile(src)
    modified, removed, left = strip_name_tags(demo, tags=tags, progress=progress)
    delta = demo.write(dst)
    return modified, removed, delta, left
