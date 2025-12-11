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
        raw = data[pos : pos + size]
        records.append(EmfRecord(rtype=rtype, raw=raw))
        pos += size
        if rtype == EMR_EOF:
            break
    if not records or records[0].rtype != EMR_HEADER:
        raise ValueError("Template EMF is missing the EMR_HEADER record.")
    if records[-1].rtype != EMR_EOF:
        raise ValueError("Template EMF is missing the EMR_EOF record.")
    return records


def split_template(records: Sequence[EmfRecord]) -> Tuple[EmfRecord, List[bytes], List[EmfRecord], List[bytes]]:
    poly_indices = [i for i, r in enumerate(records) if r.rtype == EMR_POLYLINE]
    if not poly_indices:
        raise ValueError("Template EMF has no EMR_POLYLINE records to replace.")
    first_poly, last_poly = poly_indices[0], poly_indices[-1]
    header = records[0]
    before = [r.raw for r in records[1:first_poly]]
    polys = list(records[first_poly : last_poly + 1])
    after = [r.raw for r in records[last_poly + 1 :]]
    return header, before, polys, after


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
            pts = [
                (float(v.dxf.location.x) * unit_scale, float(v.dxf.location.y) * unit_scale)
                for v in e.vertices()
                if not v.is_seqend
            ]
            if e.is_closed and pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            result.append(pts)
        elif dxftype == "CIRCLE":
            center = (float(e.dxf.center.x) * unit_scale, float(e.dxf.center.y) * unit_scale)
            radius = float(e.dxf.radius) * unit_scale
            result.append(sample_circle(center, radius, circle_segments))
        elif dxftype == "ARC":
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
        choices=["auto", "mm", "in", "inch", "inches"],
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

    records = parse_emf_records(args.template_emf)
    job_name = args.job_name or args.output_emf.stem
    header, before, template_polys, after = split_template(records)
    before = [rebuild_comment_jobname(r, job_name) for r in before]
    after = [rebuild_comment_jobname(r, job_name) for r in after]
    template_bounds = union_bounds([polyline_points(r) for r in template_polys])

    # Determine DXF unit scale (to mm) before calibration/transform.
    def unit_scale_from_header(path: Path) -> float:
        try:
            doc = ezdxf.readfile(path)
            insunits = doc.header.get("$INSUNITS")
        except Exception:
            return 1.0
        # INSUNITS codes: 1=inches, 4=mm. Default to mm if unknown.
        if insunits == 1:
            return 25.4
        if insunits == 4:
            return 1.0
        return 1.0

    if args.dxf_units in ("in", "inch", "inches"):
        unit_scale = 25.4
    elif args.dxf_units == "mm":
        unit_scale = 1.0
    else:
        unit_scale = unit_scale_from_header(args.input_dxf)

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
    total_records = 1 + len(before) + len(new_poly_records) + len(after)
    header_placeholder = rebuild_header(
        header,
        total_records,
        0,
        geom_bounds,
        keep_header_counts=args.preserve_header_counts,
        zero_bounds=args.zero_bounds,
    )
    assembled = [header_placeholder] + before + new_poly_records + after
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
    print(
        "Transform used: scale_x={:.6f}, offset_x={:.3f}, scale_y={:.6f}, offset_y={:.3f}, swap_xy={}, flip_x={}, flip_y={}".format(
            transform.sx, transform.ox, transform.sy, transform.oy, transform.swap, transform.flip_x, transform.flip_y
        )
    )
    print("New geometry bounds (EMF units):", geom_bounds)


if __name__ == "__main__":
    main()
