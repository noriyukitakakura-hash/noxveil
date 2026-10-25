#!/usr/bin/env python3
"""Builds the bundled basemap from source shapefiles.

The app never reaches the network, so the only map it can draw is the one it
carries. This turns coastlines and arterial roads into a compact binary of
polylines the Metal renderer can upload directly.

    python3 Tools/build-basemap.py --coastline <dir>/ne_10m_coastline.shp \\
        --output App/darkworld/Basemap.noxbase

Coordinates are stored in the same Web Mercator space the mask grid uses, but
at 2^32 per axis rather than the mask's 2^24. One mask cell is about 1.9 m in
Tokyo and spans four screen pixels at zoom 18, so quantising the basemap to
cells would put a visible stair-step on every coastline. At 2^32 the quantum is
about a centimetre; shifting right by 8 gives cell coordinates when the
renderer wants them.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shapefile import read_polylines  # noqa: E402

MAGIC = b"NOXBASE\x00"
VERSION = 2
COORDINATE_BITS = 32
COORDINATE_SCALE = 1 << COORDINATE_BITS

# Layer kinds, in the order the renderer draws them: coastline underneath,
# then roads from least to most prominent.
KIND_COASTLINE = 0
KIND_MOTORWAY = 1
KIND_TRUNK = 2
KIND_PRIMARY = 3
KIND_SECONDARY = 4
KIND_TERTIARY = 5
KIND_STREET = 6
KIND_PATH = 7

KIND_NAMES = {
    KIND_COASTLINE: "coastline",
    KIND_MOTORWAY: "motorway",
    KIND_TRUNK: "trunk",
    KIND_PRIMARY: "primary",
    KIND_SECONDARY: "secondary",
    KIND_TERTIARY: "tertiary",
    KIND_STREET: "street",
    KIND_PATH: "path",
}

# Below these zooms a road class is not drawn.
#
# Staggered rather than uniform. At zoom 8 the whole of Japan is on screen and
# a life's walking is a small smudge; a million points of road over it would be
# a grey wash that buries the thing the map is for, as well as the most
# expensive frame in the app. Coastline alone is the right wide view.
ROAD_MIN_ZOOM = {
    KIND_MOTORWAY: 8,
    KIND_TRUNK: 10,
    KIND_PRIMARY: 11,
    KIND_SECONDARY: 12,
    KIND_TERTIARY: 13,
    KIND_STREET: 14,
    KIND_PATH: 15,
}

# How finely each layer is cut up for loading.
#
# A bucket is the unit the app loads and keeps: one tile at this zoom. Chosen
# about three zooms below where the layer starts being drawn, which puts one to
# four buckets on screen whenever the layer is visible. Coarser and a single
# bucket would be tens of megabytes; finer and a wide view would need hundreds
# of them at once.
BUCKET_ZOOM = {
    KIND_COASTLINE: 5,
    KIND_MOTORWAY: 6,
    KIND_TRUNK: 7,
    KIND_PRIMARY: 8,
    KIND_SECONDARY: 9,
    KIND_TERTIARY: 10,
    KIND_STREET: 11,
    KIND_PATH: 12,
}

# How much detail each layer keeps, in 2^32 units (one unit is about a
# centimetre; 512 is about 5 m).
#
# Matched to where the layer first appears rather than uniform. A street is
# only ever seen from zoom 14 in, where half a pixel is about 2 m, so bends
# finer than that are bytes nobody can see. Detail nobody can see is the main
# way a map of a whole country becomes too big to carry.
ROAD_TOLERANCE = {
    KIND_MOTORWAY: 512.0,
    KIND_TRUNK: 512.0,
    KIND_PRIMARY: 512.0,
    KIND_SECONDARY: 384.0,
    KIND_TERTIARY: 320.0,
    KIND_STREET: 256.0,
    KIND_PATH: 256.0,
}

# The Mercator projection is cut off where it stops being useful.
LATITUDE_LIMIT = 85.05112878


def project(longitude: float, latitude: float) -> tuple[int, int]:
    """Web Mercator, as integers filling the 2^32 square."""
    longitude = min(180.0, max(-180.0, longitude))
    latitude = min(LATITUDE_LIMIT, max(-LATITUDE_LIMIT, latitude))
    x = (longitude + 180.0) / 360.0
    y = (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0
    return (
        min(COORDINATE_SCALE - 1, max(0, int(x * COORDINATE_SCALE))),
        min(COORDINATE_SCALE - 1, max(0, int(y * COORDINATE_SCALE))),
    )


def simplify(points: list[tuple[int, int]], tolerance: float) -> list[tuple[int, int]]:
    """Douglas-Peucker, iteratively.

    Iteratively because a single coastline part can carry tens of thousands of
    points and the recursive form hits Python's stack limit on real data.
    """
    if len(points) < 3 or tolerance <= 0:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    squared_tolerance = tolerance * tolerance

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[last]
        dx, dy = bx - ax, by - ay
        length_squared = dx * dx + dy * dy

        furthest_index = -1
        furthest_distance = 0.0
        for i in range(first + 1, last):
            px, py = points[i]
            if length_squared == 0:
                distance = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / length_squared
                t = min(1.0, max(0.0, t))
                ex, ey = ax + t * dx, ay + t * dy
                distance = (px - ex) ** 2 + (py - ey) ** 2
            if distance > furthest_distance:
                furthest_distance = distance
                furthest_index = i

        if furthest_index >= 0 and furthest_distance > squared_tolerance:
            keep[furthest_index] = True
            stack.append((first, furthest_index))
            stack.append((furthest_index, last))

    return [point for point, kept in zip(points, keep) if kept]


def write_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def write_zigzag(out: bytearray, value: int) -> None:
    write_varint(out, (value << 1) ^ (value >> 63))


def encode_line_into(body: bytearray, line: list[tuple[int, int]]) -> None:
    """One line appended to a bucket: a count, an origin, then deltas."""
    write_varint(body, len(line))
    x, y = line[0]
    body += struct.pack("<II", x, y)
    previous_x, previous_y = x, y
    for x, y in line[1:]:
        write_zigzag(body, x - previous_x)
        write_zigzag(body, y - previous_y)
        previous_x, previous_y = x, y


def split_into_buckets(
    line: list[tuple[int, int]], bucket_zoom: int
) -> list[tuple[tuple[int, int], list[tuple[int, int]]]]:
    """Cuts a line wherever it leaves one bucket for the next.

    Without this a single expressway would sit in whichever bucket it started
    in, and looking at one end would load the whole length of it. The cut keeps
    the crossing point in both pieces, so the line still joins up on screen.
    """
    shift = COORDINATE_BITS - bucket_zoom
    pieces: list[tuple[tuple[int, int], list[tuple[int, int]]]] = []
    current_key = (line[0][0] >> shift, line[0][1] >> shift)
    current: list[tuple[int, int]] = [line[0]]

    for point in line[1:]:
        key = (point[0] >> shift, point[1] >> shift)
        current.append(point)
        if key != current_key:
            if len(current) >= 2:
                pieces.append((current_key, current))
            current_key = key
            current = [point]
    if len(current) >= 2:
        pieces.append((current_key, current))
    return pieces


@dataclass
class LayerPlan:
    """What one class of line is, before any of it has been read."""
    kind: int
    min_zoom: int
    bucket_zoom: int
    tolerance: float


class Accumulator:
    """Encodes lines into buckets as they are read.

    Holding the geometry first and encoding afterwards is what the old builder
    did, and it was fine for two million points. The walkable network of Japan
    is twenty times that, and as Python tuples it would not fit in memory. Each
    line is simplified, cut at bucket boundaries and encoded on the spot, so
    what is held is the finished bytes -- a few bytes a point rather than a
    hundred.
    """

    def __init__(self) -> None:
        self.plans: dict[int, LayerPlan] = {}
        self.bodies: dict[tuple[int, int, int], bytearray] = {}
        self.line_counts: dict[tuple[int, int, int], int] = {}
        self.points: dict[int, int] = {}
        self.lines: dict[int, int] = {}

    def plan(self, kind: int, min_zoom: int, bucket_zoom: int, tolerance: float) -> None:
        self.plans[kind] = LayerPlan(kind, min_zoom, bucket_zoom, tolerance)

    def add(self, kind: int, points: list[tuple[int, int]]) -> bool:
        plan = self.plans[kind]
        deduped = [points[0]]
        for point in points[1:]:
            if point != deduped[-1]:
                deduped.append(point)
        simplified = simplify(deduped, plan.tolerance)
        if len(simplified) < 2:
            return False

        for key, piece in split_into_buckets(simplified, plan.bucket_zoom):
            bucket = (kind, key[0], key[1])
            body = self.bodies.get(bucket)
            if body is None:
                body = bytearray()
                self.bodies[bucket] = body
                self.line_counts[bucket] = 0
            encode_line_into(body, piece)
            self.line_counts[bucket] += 1
            self.points[kind] = self.points.get(kind, 0) + len(piece)
        self.lines[kind] = self.lines.get(kind, 0) + 1
        return True

    def kinds(self) -> list[int]:
        return sorted(k for k in self.plans if self.lines.get(k, 0) > 0)


def load_layer(
    accumulator: Accumulator,
    shp: Path,
    kind: int,
    bounds: tuple[float, float, float, float] | None = None,
    fclass_field: str | None = None,
    fclass_values: set[str] | None = None,
) -> None:
    dropped_short = 0
    for feature in read_polylines(shp, with_attributes=fclass_field is not None):
        if fclass_field is not None:
            value = feature.attributes.get(fclass_field, "")
            if fclass_values is not None and value not in fclass_values:
                continue

        for part in feature.parts:
            if bounds is not None:
                west, south, east, north = bounds
                if not any(
                    west <= lon <= east and south <= lat <= north for lon, lat in part
                ):
                    continue
            if not accumulator.add(kind, [project(lon, lat) for lon, lat in part]):
                dropped_short += 1

    if dropped_short:
        print(f"    dropped {dropped_short} part(s) with fewer than two points")


HIGHWAY_TO_KIND = {
    "motorway": KIND_MOTORWAY, "motorway_link": KIND_MOTORWAY,
    "trunk": KIND_TRUNK, "trunk_link": KIND_TRUNK,
    "primary": KIND_PRIMARY, "primary_link": KIND_PRIMARY,
    "secondary": KIND_SECONDARY, "secondary_link": KIND_SECONDARY,
    "tertiary": KIND_TERTIARY, "tertiary_link": KIND_TERTIARY,
    "residential": KIND_STREET, "unclassified": KIND_STREET,
    "living_street": KIND_STREET, "pedestrian": KIND_STREET,
    "footway": KIND_PATH, "path": KIND_PATH, "steps": KIND_PATH,
    "cycleway": KIND_PATH, "track": KIND_PATH,
}


def load_geojsonseq_roads(
    accumulator: Accumulator,
    path: Path,
    bounds: tuple[float, float, float, float] | None = None,
) -> None:
    """Reads what `osmium export -f geojsonseq` writes.

    One JSON object per line, so a national road network streams through
    without ever being held whole. Features are sorted into layers by their
    highway tag, with the `_link` variants folded into the class they connect.
    """
    import json

    read = kept = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")  # geojsonseq may use a record separator
            if not line:
                continue
            read += 1
            if read % 1_000_000 == 0:
                print(f"    {read // 1000}k features read, {kept // 1000}k kept")
            feature = json.loads(line)
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "LineString":
                continue
            kind = HIGHWAY_TO_KIND.get(feature.get("properties", {}).get("highway", ""))
            if kind is None:
                continue

            coordinates = geometry.get("coordinates") or []
            if len(coordinates) < 2:
                continue
            if bounds is not None:
                west, south, east, north = bounds
                if not any(
                    west <= lon <= east and south <= lat <= north
                    for lon, lat in coordinates
                ):
                    continue
            if accumulator.add(kind, [project(lon, lat) for lon, lat in coordinates]):
                kept += 1

    print(f"    read {read} features, kept {kept}")


def build(accumulator: Accumulator) -> tuple[bytes, dict[int, tuple[int, int]]]:
    """The file: a directory of buckets first, then the geometry.

    Split in two so the app can read the directory at launch -- a few hundred
    kilobytes -- and leave the rest on disk until something on screen needs it.
    The alternative, one buffer per layer uploaded whole, is what the old
    format did, and it only worked while the map was nothing but arterials.
    """
    kinds = accumulator.kinds()
    by_kind: dict[int, list[tuple[int, int]]] = {kind: [] for kind in kinds}
    for (kind, x, y) in accumulator.bodies:
        by_kind[kind].append((x, y))
    for keys in by_kind.values():
        keys.sort()

    header_size = len(MAGIC) + 8
    directory_size = sum(8 + 20 * len(by_kind[kind]) for kind in kinds)
    body_start = header_size + directory_size

    directory = bytearray()
    bodies = bytearray()
    sizes: dict[int, tuple[int, int]] = {}

    for kind in kinds:
        plan = accumulator.plans[kind]
        keys = by_kind[kind]
        directory += struct.pack(
            "<BBBBI", kind, plan.min_zoom, plan.bucket_zoom, 0, len(keys)
        )
        layer_bytes = 0
        for x, y in keys:
            body = accumulator.bodies[(kind, x, y)]
            directory += struct.pack(
                "<IIIII",
                x, y, accumulator.line_counts[(kind, x, y)],
                body_start + len(bodies), len(body),
            )
            bodies += body
            layer_bytes += len(body)
        sizes[kind] = (len(keys), layer_bytes)

    out = bytearray(MAGIC)
    out += struct.pack("<HHI", VERSION, len(kinds), 0)
    out += directory
    out += bodies
    return bytes(out), sizes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coastline", type=Path, help="ne_10m_coastline.shp")
    parser.add_argument("--roads", type=Path, help="a roads .shp with an fclass field")
    parser.add_argument("--roads-field", default="fclass")
    parser.add_argument(
        "--roads-geojsonseq", type=Path,
        help="what `osmium export -f geojsonseq` writes; sorted by highway tag",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--coastline-tolerance", type=float, default=2048.0,
        help="in 2^32 units; 2048 is about 19 m, half a pixel at zoom 14",
    )
    parser.add_argument(
        "--japan-only", action="store_true",
        help="clip roads to Japan's bounding box",
    )
    args = parser.parse_args()

    japan = (122.0, 24.0, 154.0, 46.0)
    accumulator = Accumulator()

    if args.coastline:
        print(f"coastline from {args.coastline.name}")
        # Worldwide: outside Japan this is the only thing on the map, and it is
        # the right thing -- most of the world is an outline you have never
        # walked.
        accumulator.plan(
            KIND_COASTLINE, min_zoom=0,
            bucket_zoom=BUCKET_ZOOM[KIND_COASTLINE],
            tolerance=args.coastline_tolerance,
        )
        load_layer(accumulator, args.coastline, KIND_COASTLINE)
        print(f"    {accumulator.lines.get(KIND_COASTLINE, 0)} lines, "
              f"{accumulator.points.get(KIND_COASTLINE, 0)} points")

    road_kinds = (KIND_MOTORWAY, KIND_TRUNK, KIND_PRIMARY, KIND_SECONDARY,
                  KIND_TERTIARY, KIND_STREET, KIND_PATH)
    for kind in road_kinds:
        accumulator.plan(
            kind, min_zoom=ROAD_MIN_ZOOM[kind],
            bucket_zoom=BUCKET_ZOOM[kind], tolerance=ROAD_TOLERANCE[kind],
        )

    if args.roads_geojsonseq:
        print(f"roads from {args.roads_geojsonseq.name}")
        load_geojsonseq_roads(
            accumulator, args.roads_geojsonseq,
            bounds=japan if args.japan_only else None,
        )

    if args.roads:
        for kind, values in (
            (KIND_MOTORWAY, {"motorway", "motorway_link"}),
            (KIND_TRUNK, {"trunk", "trunk_link"}),
            (KIND_PRIMARY, {"primary", "primary_link"}),
        ):
            print(f"{KIND_NAMES[kind]} from {args.roads.name}")
            load_layer(
                accumulator, args.roads, kind,
                bounds=japan if args.japan_only else None,
                fclass_field=args.roads_field, fclass_values=values,
            )

    if not accumulator.kinds():
        parser.error("nothing to build: pass --coastline and/or a roads source")

    data, sizes = build(accumulator)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)

    print()
    print(f"wrote {args.output} — {len(data) / 1048576:.2f} MB")
    for kind in accumulator.kinds():
        buckets, encoded = sizes[kind]
        points = accumulator.points.get(kind, 0)
        print(
            f"    {KIND_NAMES[kind]:<10} {accumulator.lines.get(kind, 0):>8} lines "
            f"{points:>10} points {encoded / 1048576:>7.2f} MB "
            f"({encoded / max(1, points):.2f} B/point) "
            f"in {buckets} buckets of z{accumulator.plans[kind].bucket_zoom}, "
            f"{encoded / max(1, buckets) / 1024:.0f} KiB each"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
