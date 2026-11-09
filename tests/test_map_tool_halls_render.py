import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from enshrouded_data.map_tool.hall_flatten import flatten_surfaces, render_flat_map
from enshrouded_data.map_tool.hall_render import PALETTE


def _decode(path):
    """Independent decoder for the shared encoder's RGB/Up-filter output."""
    data = Path(path).read_bytes()
    offset, compressed = 8, bytearray()
    while offset < len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        payload = data[offset + 8:offset + 8 + size]
        if kind == b'IHDR':
            width, height = struct.unpack_from('>II', payload)
        elif kind == b'IDAT':
            compressed.extend(payload)
        offset += size + 12
    packed = zlib.decompress(compressed)
    previous = bytes(width * 3)
    rows = []
    for y in range(height):
        start = y * (width * 3 + 1)
        if packed[start] != 2:
            raise AssertionError('Unexpected PNG filter')
        row = bytes((a + b) & 255 for a, b in zip(
            packed[start + 1:start + 1 + width * 3], previous))
        rows.append(row)
        previous = row
    return width, height, lambda x, y: tuple(rows[y][x * 3:x * 3 + 3]), b''.join(rows)


class HollowFlatMapTests(unittest.TestCase):
    def test_all_floors_share_the_original_union_and_order_is_deterministic(self):
        samples = [(-2, -3, 10, 20), (-2, -3, 30, 40), (-2, -3, 50, 60),
                   (-1, -2, 15, 25), (5, 5, 10, 20)]
        layers, report = flatten_surfaces(samples, [-2, -3, 0, 0])
        reverse_layers, reverse_report = flatten_surfaces(reversed(samples), [-2, -3, 0, 0])
        self.assertEqual((layers, report), (reverse_layers, reverse_report))
        self.assertEqual(report['source_samples'], 4)
        self.assertEqual(report['contributing_samples'], 4)
        self.assertEqual(report['unique_xz_columns'], 2)
        self.assertEqual(report['source_samples_per_depth'], [2, 1, 1])
        self.assertEqual(report['lower_surface_samples'], 2)
        self.assertEqual(report['stacked_columns'], 1)
        self.assertEqual(report['max_surfaces_per_column'], 3)
        self.assertEqual(report['excluded_samples'], 1)
        self.assertEqual(layers[0], {(0, 2): (50, 10), (1, 1): (15, 10)})
        self.assertEqual(layers[1], {(0, 2): (30, 10)})
        self.assertEqual(layers[2], {(0, 2): (10, 10)})
        union = hashlib.sha256(struct.pack('<qqqq', -2, -3, -1, -2)).hexdigest()
        self.assertEqual(report['projection_union_sha256'], union)

    def test_actual_png_composites_lower_surfaces_without_changing_the_footprint(self):
        top = [(x, z, 90, 100) for x in range(40) for z in range(40)]
        # The common minimum keeps the base elevation palette fixed in all
        # three images, independently of the lower patches under comparison.
        common = top + [(0, 0, 0, 8)]
        lower = [(x, z, 10, 20) for x in range(8, 20) for z in range(8, 20)]
        middle = [(12, 12, 50, 60)]
        with tempfile.TemporaryDirectory() as directory:
            reports = [render_flat_map(Path(directory) / f'{i}.png', 'Hollow Halls',
                                       [0, 0, 40, 40], samples)
                       for i, samples in enumerate((common, common + lower, common + lower + middle))]
            decoded = [_decode(report['path']) for report in reports]
            ox, oy = reports[0]['panels'][0]['map_origin_pixels']
            # A lower sample actually changes the RGB at that exact shared X/Z.
            location = (ox + 12, oy + 40 - 12 - 1)
            self.assertNotEqual(decoded[0][2](*location), decoded[1][2](*location))
            self.assertNotEqual(decoded[1][2](*location), decoded[2][2](*location))
            unstacked = (ox + 30, oy + 40 - 30 - 1)
            self.assertEqual(decoded[0][2](*unstacked), decoded[2][2](*unstacked))
            for report, (width, height, _, rgb) in zip(reports, decoded):
                self.assertEqual(report['dimensions'], [width, height])
                self.assertEqual(report['rgb_sha256'], hashlib.sha256(rgb).hexdigest())
                self.assertTrue(report['png_verification']['all_pixels_checked'])
                self.assertEqual(len(report['panels']), 1)
                self.assertEqual(report['columns'], 1)
                self.assertEqual(report['panels'][0]['map_dimensions_pixels'], [40, 40])
                self.assertEqual(report['flat_composition']['unique_xz_columns'], 1600)
            last = reports[-1]['flat_composition']
            self.assertEqual(last['source_samples'], 1746)
            self.assertEqual(last['lower_surface_samples'], 146)
            self.assertGreater(last['lower_composite_changed_pixels'], 100)

    def test_markers_project_once_at_continuous_world_coordinates_and_keep_coincident_roles(self):
        markers = [
            {'gx': -75.25, 'gz': -150.25, 'y_cells': 25, 'kind': 'portal', 'panels': [5]},
            {'gx': -75.25, 'gz': -150.25, 'y_cells': 45, 'kind': 'portal', 'panels': [2]},
            {'gx': -50, 'gz': -125, 'y_cells': 10, 'kind': 'checkpoint', 'panels': [3]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = render_flat_map(Path(directory) / 'markers.png', 'Halls',
                                      [-100, -200, 0, -100], [(-75, -151, 20, 30)], markers)
            panel = report['panels'][0]
            ox, oy = panel['map_origin_pixels']
            self.assertEqual(len(panel['markers']), 3)
            self.assertEqual(panel['markers'][0]['pixel'], [ox + 24, oy + 50])
            self.assertEqual(panel['markers'][1]['pixel'], [ox + 24, oy + 50])
            self.assertEqual(panel['markers'][2]['pixel'], [ox + 50, oy + 25])
            self.assertEqual(report['flat_composition']['coincident_marker_positions'], [
                {'world_cells': [-75.25, -150.25], 'marker_indices': [0, 1],
                 'count': 2, 'kinds': ['portal', 'portal']}])
            _, _, pixel, _ = _decode(report['path'])
            self.assertEqual(pixel(ox + 28, oy + 50), PALETTE['portal'])

    def test_sparse_union_leaves_unsampled_gaps_and_allows_no_output_overwrite(self):
        samples = [(x, z, 10, 20) for x in range(80) for z in range(80)
                   if x < 20 or x >= 60 or z < 20 or z >= 60]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sparse.png'
            report = render_flat_map(path, 'Halls', [0, 0, 80, 80], samples)
            _, _, pixel, _ = _decode(path)
            ox, oy = report['panels'][0]['map_origin_pixels']
            self.assertEqual(pixel(ox + 35, oy + 35), PALETTE['panel'])
            self.assertNotEqual(pixel(ox + 10, oy + 35), PALETTE['panel'])
            with self.assertRaisesRegex(ValueError, 'fresh'):
                render_flat_map(path, 'Halls', [0, 0, 80, 80], samples)

    def test_invalid_samples_or_non_native_scale_are_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.png'
            for samples, bounds, options in [
                ([], [0, 0, 2, 2], {}),
                ([(0, 0, 10, 10)], [0, 0, 2, 2], {}),
                ([(0, 0, 10, 20)], [0, 0, 0, 2], {}),
                ([(0, 0, 10, 20)], [0, 0, 2, 2], {'pixels_per_metre': 1}),
            ]:
                with self.assertRaises(ValueError):
                    render_flat_map(path, 'Halls', bounds, samples, **options)
                self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
