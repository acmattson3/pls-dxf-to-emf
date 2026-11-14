#!/usr/bin/env python3
"""
Convert a DXF into a ULS-ready EMF by reusing the comments/pens from a template.

Flow:
- Load the template EMF and keep every record except the geometry (EMR_POLYLINE).
- Derive an affine transform (scale + offset per axis) from a calibration DXF
  that produced the template EMF, or accept the transform explicitly.
- Parse the input DXF into polylines (lines, lwpolylines, polylines, circles,
  arcs) and map every vertex through the transform.
- Emit new EMR_POLYLINE records, rebuild the EMF header with correct sizes, and
  write the result.
"""

import argparse
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import ezdxf
from ezdxf.math import bulge_to_arc


EMR_HEADER = 1
EMR_POLYLINE = 4
EMR_EOF = 14
EMR_COMMENT = 70
EMR_ULS_PATH = 123
ULS_GEOMETRY_COMMENTS = (b"CIRCLE(", b"ARC(", b"ELLIPSE(")

# DXF $INSUNITS values expressed as millimeters per drawing unit.
INSUNITS_TO_MM = {
    1: 25.4,  # inches
    2: 304.8,  # feet
    3: 1_609_344.0,  # miles
    4: 1.0,  # millimeters
    5: 10.0,  # centimeters
    6: 1_000.0,  # meters
    7: 1_000_000.0,  # kilometers
    8: 0.0000254,  # microinches
    9: 0.0254,  # mils
    10: 914.4,  # yards
    11: 0.0000001,  # angstroms
    12: 0.000001,  # nanometers
    13: 0.001,  # microns
    14: 100.0,  # decimeters
    15: 10_000.0,  # decameters
    16: 100_000.0,  # hectometers
    17: 1_000_000_000_000.0,  # gigameters
    18: 149_597_870_700_000.0,  # astronomical units
    19: 9_460_730_472_580_800_000.0,  # light years
    20: 30_856_775_814_913_700_000.0,  # parsecs
    21: 1200.0 * 1000.0 / 3937.0,  # US survey feet
    22: 100.0 * 1000.0 / 3937.0,  # US survey inches
    23: 3600.0 * 1000.0 / 3937.0,  # US survey yards
    24: 6_336_000.0 * 1000.0 / 3937.0,  # US survey miles
}


Point = Tuple[float, float]
IntPoint = Tuple[int, int]


@dataclass
class EmfRecord:
    rtype: int
    raw: bytes


@dataclass
class Transform:
    sx: float
    ox: float
    sy: float
    oy: float
    swap: bool
    flip_x: bool
    flip_y: bool

    def apply(self, pt: Point) -> IntPoint:
        x, y = pt
        if self.swap:
            src_x = -y if self.flip_x else y
            src_y = -x if self.flip_y else x
        else:
            src_x = -x if self.flip_x else x
            src_y = -y if self.flip_y else y
        return (int(round(src_x * self.sx + self.ox)), int(round(src_y * self.sy + self.oy)))


def parse_emf_records(path: Path) -> List[EmfRecord]:
    data = path.read_bytes()
    records: List[EmfRecord] = []
    pos = 0
    while pos + 8 <= len(data):
        rtype, size = struct.unpack_from("<II", data, pos)
        if size < 8:
            raise ValueError(f"Invalid EMF record size {size} at byte offset {pos}.")
        if size % 4:
            raise ValueError(f"EMF record at byte offset {pos} is not DWORD-aligned (size {size}).")
        if pos + size > len(data):
            raise ValueError(f"EMF record at byte offset {pos} extends past end of file.")
        raw = data[pos : pos + size]
        records.append(EmfRecord(rtype=rtype, raw=raw))
        pos += size
        if rtype == EMR_EOF:
            break
    if pos != len(data):
        raise ValueError("Template EMF contains trailing bytes or a truncated record header.")
    if not records or records[0].rtype != EMR_HEADER:
        raise ValueError("Template EMF is missing the EMR_HEADER record.")
    if records[-1].rtype != EMR_EOF:
        raise ValueError("Template EMF is missing the EMR_EOF record.")
    if len(records[0].raw) < 88:
        raise ValueError("Template EMF has a truncated EMR_HEADER record.")
    if len(records[-1].raw) < 20:
        raise ValueError("Template EMF has a truncated EMR_EOF record.")
    for record in records:
        if record.rtype == EMR_COMMENT:
            if len(record.raw) < 12:
                raise ValueError("Template EMF has a truncated EMR_COMMENT record.")
            data_size = struct.unpack_from("<I", record.raw, 8)[0]
            if data_size > len(record.raw) - 12:
                raise ValueError("Template EMF has an EMR_COMMENT payload larger than its record.")
    return records


def split_template(records: Sequence[EmfRecord]) -> Tuple[EmfRecord, List[bytes | None], List[EmfRecord]]:
    geometry_indices = [i for i, r in enumerate(records) if is_template_geometry(r)]
    if not geometry_indices:
        raise ValueError("Template EMF has no recognized vector geometry to replace.")
    header = records[0]
    geometry = [r for r in records if is_template_geometry(r)]
    slots: List[bytes | None] = []
    inserted = False
    for record in records[1:]:
        if is_template_geometry(record):
            if not inserted:
                slots.append(None)
                inserted = True
        else:
            slots.append(record.raw)
    return header, slots, geometry


def comment_payload(record: EmfRecord) -> bytes:
    if record.rtype != EMR_COMMENT or len(record.raw) < 12:
        return b""
    data_size = struct.unpack_from("<I", record.raw, 8)[0]
    if data_size > len(record.raw) - 12:
        return b""
    return record.raw[12 : 12 + data_size]


def uls_path_points(record: EmfRecord) -> List[IntPoint] | None:
    """Decode ULS's private type-123 polyline record, if its layout matches."""
    if record.rtype != EMR_ULS_PATH or len(record.raw) < 36:
        return None
    count = struct.unpack_from("<I", record.raw, 24)[0]
    if count < 2 or len(record.raw) < 36 + 8 * count:
        return None
    coords = struct.unpack_from("<" + "ll" * count, record.raw, 28)
    return list(zip(coords[::2], coords[1::2]))


def is_template_geometry(record: EmfRecord) -> bool:
    if record.rtype == EMR_POLYLINE or uls_path_points(record) is not None:
        return True
    payload = comment_payload(record)
    return any(payload.startswith(prefix) for prefix in ULS_GEOMETRY_COMMENTS)


def bounds_from_template_geometry(records: Sequence[EmfRecord]) -> Tuple[int, int, int, int]:
    points: List[Point] = []
    for record in records:
        if record.rtype == EMR_POLYLINE:
            points.extend((float(x), float(y)) for x, y in polyline_points(record))
        elif record.rtype == EMR_ULS_PATH:
            path_points = uls_path_points(record) or []
            points.extend((float(x), float(y)) for x, y in path_points)
            count = len(path_points)
            bulge_offset = 28 + 8 * count
            if count == 2 and len(record.raw) >= bulge_offset + 4:
                bulge = struct.unpack_from("<f", record.raw, bulge_offset)[0]
                if math.isfinite(bulge) and abs(bulge) > 1e-12:
                    center, start_angle, end_angle, radius = bulge_to_arc(
                        path_points[0], path_points[1], bulge
                    )
                    points.extend(
                        _arc_extent_points(
                            center.x, center.y, radius, start_angle, end_angle - start_angle
                        )
                    )
        elif record.rtype == EMR_COMMENT:
            payload = comment_payload(record).rstrip(b"\x00")
            try:
                command, params = payload.split(b"(", 1)
                if not params.endswith(b")"):
                    continue
                values = [float(value) for value in params[:-1].split(b",")]
            except (ValueError, IndexError):
                continue
            if command == b"CIRCLE" and len(values) == 3:
                x, y, radius = values
                radius = abs(radius)
                points.extend([(x - radius, y - radius), (x + radius, y + radius)])
            elif command == b"ARC" and len(values) == 5:
                x, y, radius, start, end = values
                sweep = (end - start) % (2 * math.pi)
                points.extend(
                    _arc_extent_points(x, y, abs(radius), start, sweep)
                )
            elif command == b"ELLIPSE" and len(values) == 7:
                x, y, major, rotation, ratio, start, end = values
                points.extend(
                    _ellipse_extent_points(
                        x, y, abs(major), rotation, ratio, start, end - start
                    )
                )
    if not points:
        raise ValueError("Template vector geometry has no points for calibration.")
    xs, ys = zip(*points)
    return math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))


def _arc_extent_points(x: float, y: float, radius: float, start: float, sweep: float) -> List[Point]:
    angles = [start, start + sweep]
    cardinals = (0, math.pi / 2, math.pi, 3 * math.pi / 2)
    if sweep >= 0:
        angles.extend(q for q in cardinals if (q - start) % (2 * math.pi) <= sweep)
    else:
        angles.extend(q for q in cardinals if (start - q) % (2 * math.pi) <= -sweep)
    return [(x + radius * math.cos(a), y + radius * math.sin(a)) for a in angles]


def _ellipse_extent_points(
    x: float,
    y: float,
    major: float,
    rotation: float,
    ratio: float,
    start: float,
    sweep: float,
) -> List[Point]:
    minor = major * abs(ratio)
    cos_rotation = math.cos(rotation)
    sin_rotation = math.sin(rotation)

    def at(parameter: float) -> Point:
        major_component = major * math.cos(parameter)
        minor_component = minor * math.sin(parameter)
        return (
            x + major_component * cos_rotation - minor_component * sin_rotation,
            y + major_component * sin_rotation + minor_component * cos_rotation,
        )

    angles = [start, start + sweep]
    full_ellipse = abs(sweep) >= 2 * math.pi - 1e-12
    if major > 0 and minor > 0:
        x_extreme = math.atan2(-minor * sin_rotation, major * cos_rotation)
        y_extreme = math.atan2(minor * cos_rotation, major * sin_rotation)
        for candidate in (x_extreme, x_extreme + math.pi, y_extreme, y_extreme + math.pi):
            if full_ellipse or _angle_in_sweep(candidate, start, sweep):
                angles.append(candidate)
    return [at(angle) for angle in angles]


def _angle_in_sweep(angle: float, start: float, sweep: float) -> bool:
    if sweep >= 0:
        return (angle - start) % (2 * math.pi) <= sweep
    return (start - angle) % (2 * math.pi) <= -sweep


def rebuild_comment_jobname(record: bytes, job_name: str) -> bytes:
    # Payload format observed: ASCII prefix "JOBNAMEW()" followed by UTF-16LE name and a terminating 0x0000.
    prefix = b"JOBNAMEW()"
    rtype, size = struct.unpack_from("<II", record, 0)
    if rtype != EMR_COMMENT:
        return record
    datasize = struct.unpack_from("<I", record, 8)[0]
    payload = record[12 : 12 + datasize]
    if not payload.startswith(prefix):
        return record
    name_bytes = job_name.encode("utf-16-le") + b"\x00\x00"
    new_payload = prefix + name_bytes
    new_datasize = len(new_payload)
    pad = (-new_datasize) % 4  # DWORD alignment for EMR records
    new_size = 12 + new_datasize + pad
    buf = bytearray(new_size)
    struct.pack_into("<II", buf, 0, EMR_COMMENT, new_size)
    struct.pack_into("<I", buf, 8, new_datasize)
    buf[12 : 12 + new_datasize] = new_payload
    return bytes(buf)


def polyline_points(record: EmfRecord) -> List[IntPoint]:
    # record.raw already includes type and size.
    rtype, size = struct.unpack_from("<II", record.raw, 0)
    if rtype != EMR_POLYLINE:
        raise ValueError("Attempted to parse non-polyline record.")
    cpt = struct.unpack_from("<I", record.raw, 24)[0]
    if size < 28 + 8 * cpt:
        raise ValueError("EMR_POLYLINE record is too short for its declared point count.")
    if cpt < 2:
        raise ValueError("EMR_POLYLINE record must contain at least two points.")
    coords = struct.unpack_from("<" + "ll" * cpt, record.raw, 28)
    pts = list(zip(coords[::2], coords[1::2]))
    return pts


def bbox_from_points(points: Iterable[IntPoint]) -> Tuple[int, int, int, int]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def union_bounds(polylines: Sequence[Sequence[IntPoint]]) -> Tuple[int, int, int, int]:
    xs: List[int] = []
    ys: List[int] = []
    for pl in polylines:
        xs.extend(p[0] for p in pl)
        ys.extend(p[1] for p in pl)
    if not xs or not ys:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


def build_polyline_record(points: Sequence[IntPoint], zero_bounds: bool = False) -> bytes:
    if len(points) < 2:
        raise ValueError("Polyline must contain at least two points.")
    min_x, min_y, max_x, max_y = (0, 0, 0, 0) if zero_bounds else bbox_from_points(points)
    cpt = len(points)
    record_size = 8 + 16 + 4 + 8 * cpt  # header, rclBounds, cpt, points
    buf = bytearray(record_size)
    struct.pack_into("<II", buf, 0, EMR_POLYLINE, record_size)
    struct.pack_into("<llll", buf, 8, min_x, min_y, max_x, max_y)
    struct.pack_into("<I", buf, 24, cpt)
    flat: List[int] = [coord for pt in points for coord in pt]
    struct.pack_into("<" + "ll" * cpt, buf, 28, *flat)
    return bytes(buf)


def sample_circle(center: Point, radius: float, segments: int) -> List[Point]:
    cx, cy = center
    pts: List[Point] = []
    for i in range(segments):
        angle = 2 * math.pi * i / segments
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    pts.append(pts[0])  # close
    return pts


def sample_arc(center: Point, radius: float, start_deg: float, end_deg: float, segments: int) -> List[Point]:
    cx, cy = center
    start_rad = math.radians(start_deg)
    end_rad = math.radians(end_deg)
    sweep = end_rad - start_rad
    if sweep <= 0:
        sweep += 2 * math.pi
    pts: List[Point] = []
    for i in range(segments + 1):
        angle = start_rad + sweep * i / segments
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return pts


def tessellate_bulge(start: Point, end: Point, bulge: float, segments_hint: int) -> List[Point]:
    # Return points between start and end (excluding start, including end).
    center, start_ang, end_ang, radius = bulge_to_arc(start, end, bulge)
    sweep = end_ang - start_ang
    segs = max(4, int(abs(sweep) / (math.pi / segments_hint))) if segments_hint > 0 else 8
    pts: List[Point] = []
    for i in range(1, segs):
        ang = start_ang + sweep * i / segs
        pts.append((center.x + radius * math.cos(ang), center.y + radius * math.sin(ang)))
    pts.append(end)
    return pts


def extract_dxf_polylines(
    path: Path,
    circle_segments: int,
    arc_segments: int,
    spline_segments: int,
    unit_scale: float,
) -> List[List[Point]]:
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    result: List[List[Point]] = []
    render_entities = []

    def has_world_xy_normal(entity) -> bool:
        normal = entity.dxf.get("extrusion")
        if normal is None:
            return True
        return (
            abs(float(normal.x)) < 1e-9
            and abs(float(normal.y)) < 1e-9
            and abs(float(normal.z) - 1.0) < 1e-9
        )

    for e in msp:
        dxftype = e.dxftype()
        if dxftype == "LINE":
            start = e.dxf.start
            end = e.dxf.end
            result.append(
                [
                    (float(start.x) * unit_scale, float(start.y) * unit_scale),
                    (float(end.x) * unit_scale, float(end.y) * unit_scale),
                ]
            )
        elif dxftype == "LWPOLYLINE":
            pts: List[Point] = []
            for sub in e.virtual_entities():
                if sub.dxftype() == "LINE":
                    st = sub.dxf.start
                    en = sub.dxf.end
                    if not pts:
                        pts.append((float(st.x) * unit_scale, float(st.y) * unit_scale))
                    pts.append((float(en.x) * unit_scale, float(en.y) * unit_scale))
                elif sub.dxftype() == "ARC":
                    center = (float(sub.dxf.center.x) * unit_scale, float(sub.dxf.center.y) * unit_scale)
                    radius = float(sub.dxf.radius) * unit_scale
                    arc_pts = sample_arc(center, radius, float(sub.dxf.start_angle), float(sub.dxf.end_angle), arc_segments)
                    if not pts:
                        pts.append(arc_pts[0])
                        pts.extend(arc_pts[1:])
                    else:
                        pts.extend(arc_pts[1:])  # skip duplicate start
            if pts and pts[0] != pts[-1] and e.closed:
                pts.append(pts[0])
            result.append(pts)
        elif dxftype == "POLYLINE":
            # Use ezdxf's path renderer here so bulges, OCS/elevation, and
            # polygon meshes are expanded instead of treating every vertex
            # as a straight XY segment.
            render_entities.append(e)
        elif dxftype == "CIRCLE":
            if not has_world_xy_normal(e):
                render_entities.append(e)
                continue
            center = (float(e.dxf.center.x) * unit_scale, float(e.dxf.center.y) * unit_scale)
            radius = float(e.dxf.radius) * unit_scale
            result.append(sample_circle(center, radius, circle_segments))
        elif dxftype == "ARC":
            if not has_world_xy_normal(e):
                render_entities.append(e)
                continue
            center = (float(e.dxf.center.x) * unit_scale, float(e.dxf.center.y) * unit_scale)
            radius = float(e.dxf.radius) * unit_scale
            start_angle = float(e.dxf.start_angle)
            end_angle = float(e.dxf.end_angle)
            result.append(sample_arc(center, radius, start_angle, end_angle, arc_segments))
        elif dxftype == "SPLINE":
            # Approximate spline into straight segments.
            bspline = e.construction_tool()
            pts = [(float(x) * unit_scale, float(y) * unit_scale) for x, y, *_ in bspline.approximate(spline_segments)]
            if not pts:
                continue
            result.append(pts)

        else:
            render_entities.append(e)

    if render_entities:
        try:
            from ezdxf.addons.drawing import Frontend, RenderContext
            from ezdxf.addons.drawing.backend import Backend
            from ezdxf.addons.drawing.config import Configuration, HatchPolicy, TextPolicy
        except ImportError as exc:
            raise RuntimeError(
                "Rendering additional DXF entities requires Pillow. Install it with: pip install Pillow"
            ) from exc

        class PolylineCollector(Backend):
            def __init__(self) -> None:
                super().__init__()
                self.polylines: List[List[Point]] = []
                self.raster_entities: List[str] = []

            def set_background(self, color) -> None:
                pass

            def draw_point(self, pos, properties) -> None:
                self.polylines.append([(float(pos.x), float(pos.y)), (float(pos.x), float(pos.y))])

            def draw_line(self, start, end, properties) -> None:
                self.polylines.append(
                    [(float(start.x), float(start.y)), (float(end.x), float(end.y))]
                )

            def draw_path(self, path, properties) -> None:
                for sub_path in path.sub_paths():
                    pts = [
                        (float(vertex.x), float(vertex.y))
                        for vertex in sub_path.flattening(distance=0.01)
                    ]
                    if sub_path.is_closed and pts and pts[0] != pts[-1]:
                        pts.append(pts[0])
                    if len(pts) >= 2:
                        self.polylines.append(pts)

            def draw_filled_paths(self, paths, properties) -> None:
                # Laser paths are vector outlines; keep hatch boundaries and
                # text glyph outlines without turning fills into scanlines.
                for filled_path in paths:
                    self.draw_path(filled_path, properties)

            def draw_filled_polygon(self, points, properties) -> None:
                pts = [(float(point.x), float(point.y)) for point in points]
                if pts and pts[0] != pts[-1]:
                    pts.append(pts[0])
                if len(pts) >= 2:
                    self.polylines.append(pts)

            def draw_image(self, image_data, properties) -> None:
                entity = self.current_entity
                self.raster_entities.append(entity.dxftype() if entity is not None else "IMAGE")

            def clear(self) -> None:
                self.polylines.clear()

        class ReportingFrontend(Frontend):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.skipped_entities: List[Tuple[str, str]] = []

            def skip_entity(self, entity, msg: str) -> None:
                self.skipped_entities.append((entity.dxftype(), msg))
                super().skip_entity(entity, msg)

        collector = PolylineCollector()
        config = Configuration(
            circle_approximation_count=max(8, circle_segments),
            hatch_policy=HatchPolicy.SHOW_OUTLINE,
            text_policy=TextPolicy.OUTLINE,
            pdmode=0,
            pdsize=1,
        )
        frontend = ReportingFrontend(RenderContext(doc), collector, config)
        frontend.draw_entities(render_entities)
        result.extend(
            [
                [(x * unit_scale, y * unit_scale) for x, y in polyline]
                for polyline in collector.polylines
            ]
        )
        skipped_types = sorted({entity_type for entity_type, _ in frontend.skipped_entities})
        if skipped_types:
            print("Warning: unsupported or omitted DXF entities:", ", ".join(skipped_types))
        if collector.raster_entities:
            raster_types = ", ".join(sorted(set(collector.raster_entities)))
            print("Warning: raster image content is not converted to vector paths:", raster_types)

    if not result:
        raise ValueError(f"No supported geometry found in DXF {path}")
    return result


def bounds_from_points(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def bounds_from_polylines(polys: Sequence[Sequence[Point]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for pl in polys:
        xs.extend(p[0] for p in pl)
        ys.extend(p[1] for p in pl)
    if not xs or not ys:
        raise ValueError("Cannot compute bounds: no points supplied.")
    return min(xs), min(ys), max(xs), max(ys)


def _axis_min_max(min_a: float, max_a: float, flip: bool) -> Tuple[float, float]:
    if flip:
        return -max_a, -min_a
    return min_a, max_a


def solve_affine(
    calib_polys: Sequence[Sequence[Point]],
    template_bounds: Tuple[int, int, int, int],
    force_swap: bool = None,
    force_flip_x: bool = None,
    force_flip_y: bool = None,
) -> Transform:
    dxf_min_x, dxf_min_y, dxf_max_x, dxf_max_y = bounds_from_polylines(calib_polys)
    emf_min_x, emf_min_y, emf_max_x, emf_max_y = template_bounds
    if dxf_max_x == dxf_min_x or dxf_max_y == dxf_min_y:
        raise ValueError("Calibration DXF has zero span in at least one axis.")

    candidates = []
    for swap in ([True, False] if force_swap is None else [force_swap]):
        for flip_x in ([False, True] if force_flip_x is None else [force_flip_x]):
            for flip_y in ([False, True] if force_flip_y is None else [force_flip_y]):
                if swap:
                    src_x_min, src_x_max = _axis_min_max(dxf_min_y, dxf_max_y, flip_x)
                    src_y_min, src_y_max = _axis_min_max(dxf_min_x, dxf_max_x, flip_y)
                else:
                    src_x_min, src_x_max = _axis_min_max(dxf_min_x, dxf_max_x, flip_x)
                    src_y_min, src_y_max = _axis_min_max(dxf_min_y, dxf_max_y, flip_y)
                sx = (emf_max_x - emf_min_x) / (src_x_max - src_x_min)
                sy = (emf_max_y - emf_min_y) / (src_y_max - src_y_min)
                ox = emf_min_x - sx * src_x_min
                oy = emf_min_y - sy * src_y_min
                anisotropy = abs(sx - sy) / max(abs(sx), abs(sy))
                candidates.append((anisotropy, swap, flip_x, flip_y, sx, ox, sy, oy))
    best = min(candidates, key=lambda c: c[0])
    _, swap, flip_x, flip_y, sx, ox, sy, oy = best
    print(
        "Calibration orientation",
        {"swap_xy": swap, "flip_x": flip_x, "flip_y": flip_y, "scale_x": sx, "scale_y": sy},
    )
    return Transform(sx=sx, ox=ox, sy=sy, oy=oy, swap=swap, flip_x=flip_x, flip_y=flip_y)


def apply_affine(points: Sequence[Point], sx: float, ox: float, sy: float, oy: float) -> List[IntPoint]:
    return [(int(round(x * sx + ox)), int(round(y * sy + oy))) for x, y in points]


def unit_scale_from_header(path: Path) -> float:
    try:
        doc = ezdxf.readfile(path)
        insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        return 1.0
    # Unitless and unknown drawings retain the documented millimeter default.
    return INSUNITS_TO_MM.get(insunits, 1.0)


def rebuild_header(
    header: EmfRecord,
    total_records: int,
    total_bytes: int,
    rcl_bounds: Tuple[int, int, int, int],
    keep_header_counts: bool,
    zero_bounds: bool,
) -> bytes:
    buf = bytearray(header.raw)
    bounds = (0, 0, 0, 0) if zero_bounds else rcl_bounds
    struct.pack_into("<llll", buf, 8, *bounds)
    if not keep_header_counts:
        struct.pack_into("<I", buf, 48, total_bytes)
        struct.pack_into("<I", buf, 52, total_records)
    return bytes(buf)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swap geometry in a template EMF with polylines from a DXF.")
    parser.add_argument("--template-emf", type=Path, default=Path("PebbleFredBase.emf"), help="Template EMF with good comments/pens.")
    parser.add_argument("--calibration-dxf", type=Path, default=Path("PebbleFredBase.dxf"), help="DXF that produced the template EMF.")
    parser.add_argument("--input-dxf", type=Path, required=True, help="DXF to convert.")
    parser.add_argument("--output-emf", type=Path, required=True, help="Where to write the rebuilt EMF.")
    parser.add_argument("--job-name", type=str, help="Job name to embed (defaults to output filename stem).")
    parser.add_argument(
        "--dxf-units",
        choices=["auto", "mm", "in", "inch", "inches", "cm", "m", "ft", "yd", "mi", "km", "mil"],
        default="auto",
        help="Units of the input DXF geometry; auto uses $INSUNITS if present (default mm).",
    )
    parser.add_argument("--swap-xy", action="store_true", help="Force swap of X/Y axes (rotate 90 degrees).")
    parser.add_argument("--no-swap-xy", action="store_true", help="Force no axis swap.")
    parser.add_argument("--flip-x", action="store_true", help="Force flip X axis.")
    parser.add_argument("--flip-y", action="store_true", help="Force flip Y axis.")
    parser.add_argument("--circle-segments", type=int, default=64, help="Segment count when approximating circles.")
    parser.add_argument("--arc-segments", type=int, default=64, help="Segment count when approximating arcs.")
    parser.add_argument("--spline-segments", type=int, default=128, help="Segments when approximating splines.")
    parser.add_argument("--preserve-header-counts", action="store_true", help="Keep template nBytes/nRecords instead of recomputing.")
    parser.add_argument("--zero-bounds", action="store_true", help="Force rclBounds in header/polylines to zero to mimic template.")
    parser.add_argument("--scale-x", type=float, help="Override: DXF->EMF scale in X (skip calibration).")
    parser.add_argument("--scale-y", type=float, help="Override: DXF->EMF scale in Y (skip calibration).")
    parser.add_argument("--offset-x", type=float, help="Override: DXF->EMF offset in X (skip calibration).")
    parser.add_argument("--offset-y", type=float, help="Override: DXF->EMF offset in Y (skip calibration).")
    args = parser.parse_args()

    for name, count in (
        ("--circle-segments", args.circle_segments),
        ("--arc-segments", args.arc_segments),
        ("--spline-segments", args.spline_segments),
    ):
        if count < 1:
            parser.error(f"{name} must be a positive integer")

    records = parse_emf_records(args.template_emf)
    job_name = args.job_name or args.output_emf.stem
    header, slots, template_geometry = split_template(records)
    slots = [
        rebuild_comment_jobname(record, job_name) if record is not None else None
        for record in slots
    ]
    template_bounds = bounds_from_template_geometry(template_geometry)

    # Determine DXF unit scale (to mm) before calibration/transform.
    explicit_unit_scales = {
        "mm": 1.0,
        "in": 25.4,
        "inch": 25.4,
        "inches": 25.4,
        "cm": 10.0,
        "m": 1_000.0,
        "ft": 304.8,
        "yd": 914.4,
        "mi": 1_609_344.0,
        "km": 1_000_000.0,
        "mil": 0.0254,
    }
    if args.dxf_units == "auto":
        unit_scale = unit_scale_from_header(args.input_dxf)
    else:
        unit_scale = explicit_unit_scales[args.dxf_units]

    if any(v is not None for v in (args.scale_x, args.scale_y, args.offset_x, args.offset_y)):
        if None in (args.scale_x, args.scale_y, args.offset_x, args.offset_y):
            raise SystemExit("If you override the transform, provide all of --scale-x/--scale-y/--offset-x/--offset-y.")
        transform = Transform(
            sx=args.scale_x,
            ox=args.offset_x,
            sy=args.scale_y,
            oy=args.offset_y,
            swap=args.swap_xy if args.swap_xy else False,
            flip_x=args.flip_x,
            flip_y=args.flip_y,
        )
    else:
        calib_polys = extract_dxf_polylines(
            args.calibration_dxf,
            args.circle_segments,
            args.arc_segments,
            args.spline_segments,
            unit_scale_from_header(args.calibration_dxf),
        )
        transform = solve_affine(
            calib_polys,
            template_bounds,
            force_swap=args.swap_xy if args.swap_xy else (False if args.no_swap_xy else None),
            force_flip_x=args.flip_x if args.flip_x else None,
            force_flip_y=args.flip_y if args.flip_y else None,
        )

    new_geom = extract_dxf_polylines(
        args.input_dxf,
        args.circle_segments,
        args.arc_segments,
        args.spline_segments,
        unit_scale,
    )
    new_polys_int: List[List[IntPoint]] = [[transform.apply(pt) for pt in pl] for pl in new_geom]
    new_poly_records = [build_polyline_record(pl, args.zero_bounds) for pl in new_polys_int if len(pl) >= 2]

    geom_bounds = union_bounds(new_polys_int)
    total_records = 1 + sum(record is not None for record in slots) + len(new_poly_records)
    header_placeholder = rebuild_header(
        header,
        total_records,
        0,
        geom_bounds,
        keep_header_counts=args.preserve_header_counts,
        zero_bounds=args.zero_bounds,
    )
    assembled = [header_placeholder]
    for record in slots:
        if record is None:
            assembled.extend(new_poly_records)
        else:
            assembled.append(record)
    total_bytes = sum(len(b) for b in assembled)
    assembled[0] = rebuild_header(
        header,
        total_records,
        total_bytes,
        geom_bounds,
        keep_header_counts=args.preserve_header_counts,
        zero_bounds=args.zero_bounds,
    )

    args.output_emf.write_bytes(b"".join(assembled))
    print("Wrote", args.output_emf)
    print("Note: DXF pen colors and lineweights are not mapped; geometry uses the template's active pen settings.")
    print(
        "Transform used: scale_x={:.6f}, offset_x={:.3f}, scale_y={:.6f}, offset_y={:.3f}, swap_xy={}, flip_x={}, flip_y={}".format(
            transform.sx, transform.ox, transform.sy, transform.oy, transform.swap, transform.flip_x, transform.flip_y
        )
    )
    print("New geometry bounds (EMF units):", geom_bounds)


if __name__ == "__main__":
    main()
