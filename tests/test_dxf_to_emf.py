import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ezdxf

import dxf_to_emf


def emf_comment(text: bytes) -> bytes:
    payload = text + b"\x00"
    data_size = len(payload)
    size = (12 + data_size + 3) & ~3
    record = bytearray(size)
    struct.pack_into("<II", record, 0, dxf_to_emf.EMR_COMMENT, size)
    struct.pack_into("<I", record, 8, data_size)
    record[12 : 12 + data_size] = payload
    return bytes(record)


def emf_header() -> bytes:
    header = bytearray(108)
    struct.pack_into("<II", header, 0, dxf_to_emf.EMR_HEADER, len(header))
    struct.pack_into("<II", header, 40, 0x464D4520, 0x00010000)
    return bytes(header)


def emf_eof() -> bytes:
    return struct.pack("<IIIII", dxf_to_emf.EMR_EOF, 20, 0, 0, 20)


class DxfToEmfTests(unittest.TestCase):
    def test_auto_calibration_prefers_uls_y_axis_direction(self):
        transform = dxf_to_emf.solve_affine(
            [[(0, 0), (10, 0), (10, 10), (0, 10)]],
            (100, 200, 110, 210),
        )

        self.assertFalse(transform.swap)
        self.assertFalse(transform.flip_x)
        self.assertTrue(transform.flip_y)
        self.assertEqual(transform.apply((0, 0)), (100, 210))
        self.assertEqual(transform.apply((10, 10)), (110, 200))

    def test_private_bulge_bounds_include_every_segment_in_emf_coordinates(self):
        dxf_points = [(0, 0), (40, 0), (50, 30), (0, 30), (0, 0)]
        emf_points = [(256000 + 288 * x, 144000 - 288 * y) for x, y in dxf_points]
        bulges = [1.0, 0.0, -0.5, 0.0, 0.0]
        record_size = 28 + 12 * len(emf_points)
        record = bytearray(record_size)
        struct.pack_into("<II", record, 0, dxf_to_emf.EMR_ULS_PATH, record_size)
        struct.pack_into("<I", record, 24, len(emf_points))
        struct.pack_into(
            "<" + "ll" * len(emf_points),
            record,
            28,
            *(coordinate for point in emf_points for coordinate in point),
        )
        struct.pack_into("<" + "f" * len(bulges), record, 28 + 8 * len(emf_points), *bulges)

        bounds = dxf_to_emf.bounds_from_template_geometry(
            [dxf_to_emf.EmfRecord(dxf_to_emf.EMR_ULS_PATH, bytes(record))]
        )

        self.assertEqual(bounds, (256000, 135360, 270400, 149760))

    def test_filled_solid_renders_through_ezdxf_point_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solid.dxf"
            doc = ezdxf.new()
            doc.modelspace().add_solid([(0, 0), (20, 0), (0, 20), (20, 20)])
            doc.saveas(path)

            polylines = dxf_to_emf.extract_dxf_polylines(path, 64, 64, 128, 1.0)

        self.assertTrue(polylines)
        self.assertTrue(all(len(polyline) >= 2 for polyline in polylines))

    def test_conversion_preserves_machine_tag_and_updates_geometry_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.emf"
            calibration = root / "calibration.dxf"
            input_dxf = root / "input.dxf"
            output = root / "output.emf"

            template_geometry = dxf_to_emf.build_polyline_record(
                [(100, 100), (110, 100), (110, 110), (100, 110), (100, 100)]
            )
            records = [
                emf_header(),
                emf_comment(b"MACHINEID(34)"),
                emf_comment(b"PENS(8)"),
                template_geometry,
                emf_eof(),
            ]
            template.write_bytes(b"".join(records))

            calibration_doc = ezdxf.new()
            calibration_doc.modelspace().add_lwpolyline(
                [(0, 0), (10, 0), (10, 10), (0, 10)], close=True
            )
            calibration_doc.saveas(calibration)

            input_doc = ezdxf.new()
            input_doc.modelspace().add_line((0, 0), (10, 10))
            input_doc.saveas(input_dxf)

            with patch.object(
                sys,
                "argv",
                [
                    "dxf_to_emf.py",
                    "--template-emf",
                    str(template),
                    "--calibration-dxf",
                    str(calibration),
                    "--input-dxf",
                    str(input_dxf),
                    "--output-emf",
                    str(output),
                ],
            ):
                dxf_to_emf.main()

            result = dxf_to_emf.parse_emf_records(output)
            geometry = [record for record in result if record.rtype == dxf_to_emf.EMR_POLYLINE]
            comments = [
                dxf_to_emf.comment_payload(record)
                for record in result
                if record.rtype == dxf_to_emf.EMR_COMMENT
            ]

            self.assertEqual(len(geometry), 1)
            self.assertEqual(dxf_to_emf.polyline_points(geometry[0]), [(100, 110), (110, 100)])
            self.assertTrue(any(comment.startswith(b"MACHINEID(34)") for comment in comments))
            self.assertTrue(any(comment.startswith(b"PENS(8)") for comment in comments))
            self.assertEqual(struct.unpack_from("<I", output.read_bytes(), 48)[0], output.stat().st_size)
            self.assertEqual(struct.unpack_from("<I", output.read_bytes(), 52)[0], len(result))
            self.assertEqual(dxf_to_emf.bounds_from_template_geometry(geometry), (100, 100, 110, 110))


if __name__ == "__main__":
    unittest.main()
