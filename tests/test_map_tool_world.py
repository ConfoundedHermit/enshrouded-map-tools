"""Independent format/profile controls for the contributor world renderer."""
import copy
import random
import struct
import unittest

from enshrouded_data.map_tool.world import (
    MAP_GUID, _config_at_zoom, _payload_size, _rva_reader, bc4_upper_bound,
    tile_entry, validate_parts,
)


class WorldProfileTests(unittest.TestCase):
    def test_bc4_bound_covers_every_explicit_selector_position(self):
        # Six-value BC4 mode includes an explicit 255 palette entry.
        for position in range(16):
            block = bytes([0, 1]) + (7 << (position * 3)).to_bytes(6, 'little')
            self.assertEqual(bc4_upper_bound(block), 255)
        self.assertEqual(bc4_upper_bound(bytes([0, 1]) + bytes(6)), 1)

    def test_bc4_bound_independent_palette_controls(self):
        rng = random.Random(87261)
        for _ in range(512):
            a, b = rng.randrange(256), rng.randrange(256)
            selectors = [rng.randrange(8) for _ in range(16)]
            if a > b:
                palette = [a, b] + [((7 - n) * a + n * b) / 7 for n in range(1, 7)]
            else:
                palette = [a, b] + [((5 - n) * a + n * b) / 5 for n in range(1, 5)] + [0, 255]
            block = bytes([a, b]) + sum(s << (3 * i) for i, s in enumerate(selectors)).to_bytes(6, 'little')
            self.assertGreaterEqual(bc4_upper_bound(block), max(palette[s] for s in selectors))
        with self.assertRaises(ValueError):
            bc4_upper_bound(bytes(7))

    def test_native_contour_packing_and_zoom_transition(self):
        near = [{'thickness': .3, 'alpha': a} for a in (.3, .5, .9)]
        far = [{'thickness': t, 'alpha': a} for t, a in ((.2, .2), (.5, .5), (.5, 1.))]
        configs = [{'zoomScale': z, 'isolineConfigs': rows}
                   for z, rows in ((16., near), (8., near), (4., far), (.7, far))]
        for zoom in (8., 12., 16.):
            selected = _config_at_zoom(configs, zoom)
            self.assertEqual(selected['widths'], [4, 4, 4])
            self.assertEqual(selected['alphas'], [76, 127, 229])
        self.assertEqual(_config_at_zoom(configs, 4.)['widths'], [3, 8, 8])
        self.assertEqual(_config_at_zoom(configs, 6.)['widths'], [4, 6, 6])
        # Output sampling density must not enter the UI contour calculation.
        self.assertNotEqual(_config_at_zoom(configs, 6.)['widths'], [8, 8, 8])
        for zoom in (0., -1., float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                _config_at_zoom(configs, zoom)
        with self.assertRaises(ValueError):
            _config_at_zoom(list(reversed(configs)), 8.)

    def test_world_bounds_are_independent_of_pixel_density(self):
        nw = tile_entry(0, 0)
        se = tile_entry(7, 7)
        self.assertEqual(nw['world_bounds_xy'], [[0, 8960], [1280, 10240]])
        self.assertEqual(nw['position'], [0, 7])
        self.assertEqual(se['world_bounds_xy'], [[8960, 0], [10240, 1280]])
        self.assertEqual(se['position'], [7, 0])
        low, high = tile_entry(3, 5, 1280), tile_entry(3, 5, 2560)
        self.assertEqual(low['world_bounds_xy'], high['world_bounds_xy'])
        self.assertEqual(low['raster_rectangle_world'], high['raster_rectangle_world'])
        self.assertNotEqual(low['pixel_size'], high['pixel_size'])
        for args in ((-1, 0), (0, 8), (True, 0), (0, 0, 5120)):
            with self.assertRaises(ValueError):
                tile_entry(*args)

    def test_complete_compressed_mip_sizes_include_terminal_blocks(self):
        self.assertEqual(_payload_size({'size': {'x': 256, 'y': 256},
                                        'levelCount': 9, 'format': 'BC7_unorm_block'}), 87408)
        self.assertEqual(_payload_size({'size': {'x': 512, 'y': 512},
                                        'levelCount': 10, 'format': 'BC4_unorm_block'}), 174776)
        self.assertEqual(_payload_size({'size': {'x': 1280, 'y': 1280},
                                        'levelCount': 1, 'format': 'R16_unorm'}), 3276800)

    def test_rva_reads_only_file_backed_sections(self):
        binary = bytearray(512)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 60, 64)
        binary[64:68] = b'PE\0\0'
        struct.pack_into('<H', binary, 70, 1)
        struct.pack_into('<H', binary, 84, 0)
        struct.pack_into('<4I', binary, 96, 128, 0x1000, 32, 256)
        binary[260:264] = b'test'
        at = _rva_reader(binary)
        self.assertEqual(at(0x1004, 4), b'test')
        with self.assertRaises(ValueError):
            at(0x1020, 4)
        binary[64] = 0
        with self.assertRaises(ValueError):
            _rva_reader(binary)


class WorldTextureContractTests(unittest.TestCase):
    @staticmethod
    def rows():
        rows = []
        names = ['baseGradient', 'isolineGradient', 'fogZoneBorderGradient',
                 'fogNoise', 'fogZoneNormal', 'fogZoneDangerous', 'baseHeight']
        for i, (size, fmt, levels) in enumerate([
            ((151, 1), 'R8G8B8A8_unorm', 1), ((128, 1), 'R8G8B8A8_unorm', 1),
            ((32, 1), 'R8G8B8A8_unorm', 1), ((512, 512), 'BC4_unorm_block', 10),
            ((256, 256), 'BC7_unorm_block', 9), ((256, 256), 'BC7_unorm_block', 9),
            ((1280, 1280), 'R16_unorm', 1),
        ]):
            rows.append({'$part': i, 'debugName': '_map_shader_parameters_' + names[i],
                         'format': fmt, 'size': dict(zip(('x', 'y'), size)), 'levelCount': levels})
        for y in range(10):
            for x in range(10):
                for name, fmt in [('DetailHeight', 'BC4_unorm_block'), ('FogZone', 'BC5_unorm_block')]:
                    rows.append({'$part': len(rows), 'debugName': f'{name}({x},{y})',
                                 'format': fmt, 'size': {'x': 1024, 'y': 1024}, 'levelCount': 1})
        for row in rows:
            row.update({'$guid': MAP_GUID, '$type': 'keen::UiTextureResource', 'isTiled': False})
        return rows

    def test_reject_holes_duplicates_and_coordinate_swaps(self):
        rows = self.rows()
        self.assertEqual(len(validate_parts(rows, (10, 10))), 207)
        for bad in (rows[:-1], rows + [rows[-1]]):
            with self.assertRaises(ValueError):
                validate_parts(bad, (10, 10))
        bad = copy.deepcopy(rows)
        bad[7]['debugName'], bad[9]['debugName'] = bad[9]['debugName'], bad[7]['debugName']
        with self.assertRaises(ValueError):
            validate_parts(bad, (10, 10))
        bad = copy.deepcopy(rows)
        bad[8]['format'] = 'BC4_unorm_block'
        with self.assertRaises(ValueError):
            validate_parts(bad, (10, 10))


if __name__ == '__main__':
    unittest.main()
