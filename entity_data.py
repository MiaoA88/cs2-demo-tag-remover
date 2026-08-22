# -*- coding: utf-8 -*-
"""``CSVCMsg_PacketEntities`` entity bit-stream surgery.

The message describes its entity updates twice over:

* field 7 ``entity_data`` -- the packed entity bit stream. Per update it holds a
  *delta header* (``u_bit_var`` index delta + 2 command bits, plus 33 bits of
  class id / serial number when the entity enters the PVS) followed by the
  entity's field data.
* field 13 ``serialized_entities`` -- one varint per update that carries field
  data, holding the **bit length** of that entity's field data.

CS2 uses the field-13 table to find each entity's chunk, so shortening
``entity_data`` without shrinking the matching table entry makes the client read
the next delta header at the wrong bit offset. Playback then dies with
``FATAL ERROR: Failed to parse delta header for Packet Entities``.

Demos rewritten by skin-changer tools drop field 13 (it is optional); such
messages are decoded strictly sequentially, so there is no table to fix.
"""
from __future__ import annotations

from proto_utils import (BitReader, ProtoError, delete_bits_from_bitstream,
                         encode_fields, parse_fields, pattern_hits, read_varint,
                         write_varint)

#: ``svc_PacketEntities`` message type inside a ``CDemoPacket.data`` container.
PACKET_ENTITIES = 55

F_UPDATED_ENTRIES = 2
F_ENTITY_DATA = 7
F_SERIALIZED_ENTITIES = 13

#: Entity indices are 14-bit (MAX_EDICTS).
MAX_EDICT_BITS = 14

#: Bits that follow the 2 command bits when an entity enters the PVS
#: (class id + serial number). Verified against the field-13 size table on
#: every ``svc_PacketEntities`` message of the reference demos.
CREATE_HEADER_BITS = 33


def read_size_table(blob):
    """Decode ``serialized_entities`` into a list of per-entity bit lengths."""
    sizes = []
    pos = 0
    while pos < len(blob):
        value, pos = read_varint(blob, pos)
        sizes.append(value)
    return sizes


def write_size_table(sizes):
    """Inverse of :func:`read_size_table`."""
    return b"".join(write_varint(size) for size in sizes)


def entity_spans(entity_data, sizes, updated_entries):
    """Walk `entity_data` and locate the field data of every updated entity.

    Returns a list of (slot, start_bit, bit_len), one entry per size-table slot.
    Raises ProtoError unless the walk matches the size table exactly -- callers
    must then leave the message alone rather than corrupt it.
    """
    reader = BitReader(entity_data)
    total = len(entity_data) * 8
    index = -1
    slot = 0
    spans = []
    try:
        for _ in range(updated_entries):
            index += 1 + reader.read_u_bit_var()
            if index >= 1 << MAX_EDICT_BITS:
                raise ProtoError(f"entity index {index} out of range")
            leaving = reader.read_nbits(1)
            entering = reader.read_nbits(1)
            if leaving:  # left the PVS or was deleted: no field data follows
                continue
            if entering:
                reader.bit += CREATE_HEADER_BITS
            if slot >= len(sizes):
                raise ProtoError("size table exhausted")
            start = reader.bit
            reader.bit += sizes[slot]
            if reader.bit > total:
                raise ProtoError("entity field data overruns the stream")
            spans.append((slot, start, sizes[slot]))
            slot += 1
    except IndexError:
        raise ProtoError("entity stream truncated") from None
    if slot != len(sizes):
        raise ProtoError(f"{len(sizes) - slot} size-table entries unused")
    left = total - reader.bit
    if not 0 <= left < 8:
        raise ProtoError(f"{left} bits left after the last entity")
    return spans


def _owning_slot(spans, start_bit, bit_len):
    """Size-table slot whose field data fully contains [start_bit, +bit_len)."""
    for slot, start, size in spans:
        if start <= start_bit and start_bit + bit_len <= start + size:
            return slot
    return None


def strip_entity_data(message, tags):
    """Delete every occurrence of any of `tags` from ``entity_data``, keeping
    ``serialized_entities`` in sync. Returns (new_message, removed).

    Occurrences are searched *inside ``entity_data``* -- the offsets a search
    over the enclosing message would produce are shifted by the field header and
    would cut the stream at the wrong place.
    """
    fields = parse_fields(message)
    values = {field_no: value for field_no, _wt, value in fields}
    data = values.get(F_ENTITY_DATA)
    if not isinstance(data, bytes):
        return message, 0
    hits = pattern_hits(data, tags)
    if not hits:
        return message, 0
    table = values.get(F_SERIALIZED_ENTITIES)
    removed = 0
    if table is None:
        for start_bit, bit_len in hits:
            data = delete_bits_from_bitstream(data, start_bit, bit_len)
            removed += 1
    else:
        sizes = read_size_table(table)
        spans = entity_spans(data, sizes, values.get(F_UPDATED_ENTRIES, 0))
        for start_bit, bit_len in hits:
            slot = _owning_slot(spans, start_bit, bit_len)
            if slot is None:  # not inside one entity's field data: too risky
                continue
            data = delete_bits_from_bitstream(data, start_bit, bit_len)
            sizes[slot] -= bit_len
            removed += 1
        table = write_size_table(sizes)
    if not removed:
        return message, 0
    out = []
    for field_no, wt, value in fields:
        if field_no == F_ENTITY_DATA:
            value = data
        elif field_no == F_SERIALIZED_ENTITIES:
            value = table
        out.append((field_no, wt, value))
    return encode_fields(out), removed
