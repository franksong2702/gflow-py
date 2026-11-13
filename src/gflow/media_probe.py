"""Pure-Python MP4 / ISO-BMFF frame-size probe (no ffprobe dependency).

Used to *measure* the real width/height of generated videos instead of trusting dimensions
inferred from Flow's aspect-ratio enum codes.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

__all__ = ["measure_mp4_dimensions"]

_VISUAL_SAMPLE_ENTRIES = frozenset(
    {b"avc1", b"avc3", b"hvc1", b"hev1", b"av01", b"vp08", b"vp09", b"mp4v", b"dvh1", b"dvhe", b"encv"}
)


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(box_type, payload_start, box_end)`` for each box in ``data[start:end]``.

    Handles ``size == 1`` (64-bit ``largesize`` follows the type) and ``size == 0``
    (box extends to the end of the enclosing range). Stops silently on truncated/malformed headers.
    """
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        box_type = bytes(data[pos + 4 : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        if box_type == b"uuid":
            header += 16  # 16-byte extended type precedes the payload
            if header > size:
                return
        yield box_type, pos + header, pos + size
        pos += size


def _find_child(data: bytes, start: int, end: int, box_type: bytes) -> tuple[int, int] | None:
    for b_type, b_start, b_end in _iter_boxes(data, start, end):
        if b_type == box_type:
            return b_start, b_end
    return None


def _parse_tkhd(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Return the track presentation size (16.16 fixed point) honouring 90/270 degree rotation matrices."""
    if end - start < 4:
        return None
    version = data[start]
    base = start + 4 + (32 if version == 1 else 20)
    matrix_off = base + 8 + 2 + 2 + 2 + 2  # reserved[8], layer, alternate_group, volume, reserved
    wh_off = matrix_off + 36
    if wh_off + 8 > end:
        return None
    w_raw, h_raw = struct.unpack_from(">II", data, wh_off)
    width = round(w_raw / 65536)
    height = round(h_raw / 65536)
    if width <= 0 or height <= 0:
        return None
    a, b, _u, c, d = struct.unpack_from(">iiiii", data, matrix_off)
    if a == 0 and d == 0 and b != 0 and c != 0:
        width, height = height, width
    return width, height


def _parse_stsd(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Return width/height from the first visual sample entry (avc1/hvc1/...) in ``stsd``."""
    if end - start < 8:
        return None
    for e_type, e_start, e_end in _iter_boxes(data, start + 8, end):
        if e_type in _VISUAL_SAMPLE_ENTRIES and e_end - e_start >= 28:
            width, height = struct.unpack_from(">HH", data, e_start + 24)
            if width > 0 and height > 0:
                return width, height
    return None


def _parse_mdia(data: bytes, start: int, end: int) -> tuple[bytes | None, tuple[int, int] | None]:
    handler: bytes | None = None
    stsd_dims: tuple[int, int] | None = None
    for m_type, m_start, m_end in _iter_boxes(data, start, end):
        if m_type == b"hdlr" and m_end - m_start >= 12:
            handler = bytes(data[m_start + 8 : m_start + 12])
        elif m_type == b"minf":
            stbl = _find_child(data, m_start, m_end, b"stbl")
            if stbl is not None:
                stsd = _find_child(data, stbl[0], stbl[1], b"stsd")
                if stsd is not None:
                    stsd_dims = _parse_stsd(data, stsd[0], stsd[1])
    return handler, stsd_dims


def _measure(buf: bytes) -> tuple[int, int] | None:
    moov = _find_child(buf, 0, len(buf), b"moov")
    if moov is None:
        return None
    fallback: tuple[int, int] | None = None
    for box_type, t_start, t_end in _iter_boxes(buf, moov[0], moov[1]):
        if box_type != b"trak":
            continue
        tkhd_dims: tuple[int, int] | None = None
        stsd_dims: tuple[int, int] | None = None
        handler: bytes | None = None
        for c_type, c_start, c_end in _iter_boxes(buf, t_start, t_end):
            if c_type == b"tkhd":
                tkhd_dims = _parse_tkhd(buf, c_start, c_end)
            elif c_type == b"mdia":
                handler, stsd_dims = _parse_mdia(buf, c_start, c_end)
        dims = tkhd_dims or stsd_dims
        if dims is None:
            continue
        if handler == b"vide":
            return dims
        if handler is None and fallback is None:
            fallback = dims
    return fallback


def measure_mp4_dimensions(data: bytes) -> tuple[int, int] | None:
    """Measure the video track's ``(width, height)`` from MP4 bytes (``moov/trak/tkhd``, ``stsd`` fallback).

    Returns ``None`` when the bytes are not a parseable MP4 or contain no visual track.
    """
    try:
        buf = bytes(data)
    except (TypeError, ValueError):
        return None
    try:
        return _measure(buf)
    except (struct.error, IndexError, ValueError, OverflowError):
        return None
