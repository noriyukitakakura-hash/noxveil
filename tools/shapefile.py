"""A minimal ESRI shapefile reader, for polylines only.

No GDAL, no pyshp, nothing to install. The parts of the format this needs are
small and fixed, and a basemap build that depends on a system package is a
basemap build that stops working on the next machine.

Reads the geometry from a .shp and the attributes from its .dbf, which is all
the basemap needs: lines, and enough fields to tell a motorway from a lane.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Only the shape types this project encounters.
SHAPE_NULL = 0
SHAPE_POLYLINE = 3
SHAPE_POLYLINE_Z = 13
SHAPE_POLYLINE_M = 23
POLYLINE_TYPES = {SHAPE_POLYLINE, SHAPE_POLYLINE_Z, SHAPE_POLYLINE_M}


@dataclass
class Feature:
    """One record: its parts, each a list of (longitude, latitude) pairs."""

    index: int
    parts: list[list[tuple[float, float]]]
    attributes: dict[str, str]

    @property
    def point_count(self) -> int:
        return sum(len(part) for part in self.parts)


def read_dbf(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Reads a dBASE III table: the attribute half of a shapefile."""
    data = path.read_bytes()
    header_length, record_length = struct.unpack_from("<HH", data, 8)
    record_count = struct.unpack_from("<I", data, 4)[0]

    fields: list[tuple[str, int]] = []
    offset = 32
    while data[offset] != 0x0D:
        raw_name, _type, _reserved, length = struct.unpack_from("<11scIB", data, offset)
        fields.append((raw_name.split(b"\0")[0].decode("latin-1"), length))
        offset += 32

    names = [name for name, _ in fields]
    records: list[dict[str, str]] = []
    base = header_length
    for i in range(record_count):
        start = base + i * record_length
        if start + record_length > len(data):
            break
        # The first byte of a record is a deletion flag.
        cursor = start + 1
        values: dict[str, str] = {}
        for name, length in fields:
            raw = data[cursor:cursor + length]
            values[name] = raw.decode("latin-1").strip()
            cursor += length
        records.append(values)
    return names, records


def read_polylines(shp_path: Path, with_attributes: bool = True) -> Iterator[Feature]:
    """Yields every polyline record in a .shp, with its .dbf attributes."""
    data = shp_path.read_bytes()
    attributes: list[dict[str, str]] = []
    if with_attributes:
        dbf_path = shp_path.with_suffix(".dbf")
        if dbf_path.exists():
            _, attributes = read_dbf(dbf_path)

    # 100-byte file header, then variable-length records.
    offset = 100
    index = 0
    while offset < len(data):
        _record_number, content_length_words = struct.unpack_from(">II", data, offset)
        content_start = offset + 8
        content_end = content_start + content_length_words * 2

        shape_type = struct.unpack_from("<I", data, content_start)[0]
        if shape_type in POLYLINE_TYPES:
            # bbox (4 doubles), then part and point counts, then the parts index.
            num_parts, num_points = struct.unpack_from("<II", data, content_start + 36)
            parts_start = content_start + 44
            part_offsets = list(
                struct.unpack_from(f"<{num_parts}I", data, parts_start)
            )
            points_start = parts_start + num_parts * 4
            coords = struct.unpack_from(f"<{num_points * 2}d", data, points_start)

            parts: list[list[tuple[float, float]]] = []
            bounds = part_offsets + [num_points]
            for part in range(num_parts):
                first, last = bounds[part], bounds[part + 1]
                parts.append([
                    (coords[i * 2], coords[i * 2 + 1]) for i in range(first, last)
                ])

            yield Feature(
                index=index,
                parts=parts,
                attributes=attributes[index] if index < len(attributes) else {},
            )

        index += 1
        offset = content_end
