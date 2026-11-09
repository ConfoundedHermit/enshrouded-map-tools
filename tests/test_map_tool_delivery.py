"""Independent pixels, orientation, corruption and bounded-memory delivery checks."""
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import tracemalloc
import unittest
import zlib
from unittest.mock import patch

from enshrouded_data.map_tool.delivery import (
    _up_bytes, convert_wiki_jpeg, inspect_png, png_rows, stitch_world_tiles,
    verify_png, verify_world_delivery, write_png, write_world_delivery,
)


def independent_decode(path):
    """Small test fixtures only; intentionally separate scalar unfilter code."""
    data = Path(path).read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    at = 8
    packed = bytearray()
    while at < len(data):
        size, kind = struct.unpack('>I4s', data[at:at + 8])
        body = data[at + 8:at + 8 + size]
        assert zlib.crc32(kind + body) == int.from_bytes(data[at + 8 + size:at + 12 + size], 'big')
        at += size + 12
        if kind == b'IHDR':
            width, height, *_ = struct.unpack('>2I5B', body)
        if kind == b'IDAT':
            packed.extend(body)
    decoded = zlib.decompress(packed)
    stride = width * 3
    previous = bytes(stride)
    rows = []
    for y in range(height):
        tag = decoded[y * (stride + 1)]
        values = decoded[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        row = bytes(values) if tag == 0 else bytes((a + b) % 256 for a, b in zip(values, previous))
        rows.append(row)
        previous = row
    return width, height, b''.join(rows)


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def tiles(self, edge=4, grid=2):
        tiles = []
        for r in range(grid):
            for c in range(grid):
                path = self.root / f'{c}_{r}.png'
                rows = [bytes(channel for x in range(edge) for channel in
                              (r * 70 + y, c * 70 + x, r * 31 + c * 19 + y * 7 + x))
                        for y in range(edge)]
                info = write_png(path, edge, edge, rows)
                tiles.append({'column': c, 'row_from_north': r, 'path': path,
                              'rgb_sha256': info['rgb_sha256'], 'png_sha256': info['sha256']})
        return tiles

    def test_integer_up_filter_is_scalar_modulo_256(self):
        rng = random.Random(642581)
        for size in (1, 3, 99, 2560 * 3):
            row, previous = rng.randbytes(size), rng.randbytes(size)
            filtered = _up_bytes(row, previous, decode=False)
            self.assertEqual(filtered, bytes((a - b) % 256 for a, b in zip(row, previous)))
            self.assertEqual(_up_bytes(filtered, previous, decode=True), row)
        # Explicit borrow/carry boundaries: no byte may affect its neighbor.
        row, previous = bytes([0, 255, 128, 0]), bytes([255, 255, 255, 128])
        self.assertEqual(_up_bytes(row, previous, decode=False), bytes([1, 0, 129, 128]))

    def test_streamed_stitch_seams_orientation_and_independent_pixel_hash(self):
        tiles = self.tiles()
        output = self.root / 'world.png'
        info = stitch_world_tiles(list(reversed(tiles)), output, grid=2, tile_world_units=2)
        width, height, decoded = independent_decode(output)
        self.assertEqual((width, height), (8, 8))
        expected = bytes(channel for y in range(8) for x in range(8) for channel in
                         ((y // 4) * 70 + y % 4, (x // 4) * 70 + x % 4,
                          (y // 4) * 31 + (x // 4) * 19 + (y % 4) * 7 + x % 4))
        self.assertEqual(decoded, expected)
        self.assertEqual(info['rgb_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(info['world_bounds_xy'], [[0, 0], [4, 4]])
        self.assertEqual(info['pixels_per_world_unit'], 2)
        self.assertFalse(info['full_image_allocated'])
        self.assertTrue(info['lossless_tile_assembly'])
        self.assertEqual(verify_png(output, info['rgb_sha256'])['status'], 'passed')

    def test_reject_incomplete_duplicate_flipped_and_mismatched_tiles(self):
        tiles = self.tiles()
        for wrong in (tiles[:-1], tiles[:-1] + [tiles[0]],
                      [{**tiles[0], 'position': [0, 0]}, *tiles[1:]],
                      [{**tiles[0], 'world_bounds_xy': [[0, 0], [4, 4]]}, *tiles[1:]],
                      [{**tiles[0], 'rgb_sha256': '0' * 64}, *tiles[1:]]):
            with self.subTest(wrong=wrong[0].get('position')):
                output = self.root / 'invalid.png'
                with self.assertRaises(ValueError):
                    stitch_world_tiles(wrong, output, grid=2)
                self.assertFalse(output.exists())
        odd = self.root / 'odd.png'
        write_png(odd, 3, 4, [bytes(9)] * 4)
        with self.assertRaises(ValueError):
            stitch_world_tiles([{**tiles[0], 'path': odd}, *tiles[1:]], self.root / 'invalid.png', grid=2)

    def test_bad_crc_and_truncated_final_tile_never_leave_master(self):
        tiles = self.tiles()
        original = tiles[-1]['path'].read_bytes()
        for damaged in (original[:-1], original[:-1] + bytes([original[-1] ^ 1]),
                        original[:-8] + b'\0' + original[-7:], original + b'trailing'):
            tiles[-1]['path'].write_bytes(damaged)
            rows = [{k: v for k, v in t.items() if k not in ('rgb_sha256', 'png_sha256')} for t in tiles]
            output = self.root / 'broken.png'
            with self.assertRaises(ValueError):
                stitch_world_tiles(rows, output, grid=2)
            self.assertFalse(output.exists())
        tiles[-1]['path'].write_bytes(original)

    def test_filters_and_compression_bomb_are_row_bounded(self):
        # One tiny IDAT expands to >6 MiB, but decoding must never inflate it all.
        width, height = 4096, 512
        path = self.root / 'compressible.png'
        header = chunk(b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
        zipper = zlib.compressobj()
        payload = bytearray()
        row = bytes([25, 129, 255]) * width
        for _ in range(height):
            payload.extend(zipper.compress(b'\0' + row))
        payload.extend(zipper.flush())
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + header + chunk(b'IDAT', payload) + chunk(b'IEND', b''))
        tracemalloc.start()
        digest = hashlib.sha256()
        for decoded in png_rows(path):
            self.assertEqual(decoded, row)
            digest.update(decoded)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        expected = hashlib.sha256()
        for _ in range(height):
            expected.update(row)
        self.assertEqual(digest.digest(), expected.digest())

    def test_writer_and_delivery_preserve_existing_files(self):
        path = self.root / 'existing.png'
        path.write_bytes(b'existing bytes')
        with self.assertRaises(ValueError):
            write_png(path, 1, 1, [b'abc'])
        self.assertEqual(path.read_bytes(), b'existing bytes')
        tiles = self.tiles()
        destination = self.root / 'delivery'
        destination.mkdir()
        (destination / 'untouched.txt').write_text('keep')
        with self.assertRaises(ValueError):
            write_world_delivery(tiles, destination, layout='tiles', build_id='123', grid=2)
        self.assertEqual((destination / 'untouched.txt').read_text(), 'keep')

    def test_layouts_size_hold_and_descriptions_have_no_license(self):
        tiles = self.tiles()
        for layout in ('single', 'tiles', 'both'):
            with self.subTest(layout=layout):
                root = self.root / layout
                result = write_world_delivery(tiles, root, layout=layout, build_id=26430625,
                                             grid=2, tile_world_units=2, max_upload_bytes=20)
                self.assertEqual(result['pixels_per_world_unit'], 2)
                self.assertEqual(result['network_requests'], 0)
                self.assertEqual('master' in result, layout in ('single', 'both'))
                self.assertEqual(len(result['tiles']), 4 if layout in ('tiles', 'both') else 0)
                self.assertTrue(all(not upload['upload_eligible'] for upload in result['uploads']))
                self.assertEqual(result['world_bounds_xy'], [[0, 0], [4, 4]])
                for upload in result['uploads']:
                    self.assertNotIn('license', (root / upload['description_file']).read_text().lower())
                self.assertTrue((root / 'index.html').is_file())
                self.assertEqual(json.loads((root / 'delivery.json').read_text()), result)
                self.assertNotIn(str(self.root), (root / 'delivery.json').read_text())
                if result['tiles']:
                    northwest = result['tiles'][0]
                    self.assertEqual(northwest['position'], [0, 1])
                    self.assertEqual(northwest['world_bounds_xy'], [[0, 2], [2, 4]])
                    self.assertEqual((root / northwest['local_file']).read_bytes(), tiles[0]['path'].read_bytes())

    def test_native_sampling_keeps_1280_world_unit_tile_extent(self):
        path = self.root / 'native.png'
        edge = 2560
        info = write_png(path, edge, edge, (bytes([30, 70, 90]) * edge for _ in range(edge)))
        tile = {'path': path, 'column': 0, 'row_from_north': 0,
                'rgb_sha256': info['rgb_sha256'], 'world_bounds_xy': [[0, 0], [1280, 1280]]}
        result = write_world_delivery([tile], self.root / 'native-delivery', layout='tiles',
                                      build_id=26430625, grid=1)
        self.assertEqual(result['tile_pixel_size'], [2560, 2560])
        self.assertEqual(result['pixels_per_world_unit'], 2)
        self.assertEqual(result['tiles'][0]['world_bounds_xy'], [[0, 0], [1280, 1280]])

    def test_resume_keeps_verified_tiles_and_replaces_only_unfinished_owned_file(self):
        tiles = self.tiles()
        root = self.root / 'interrupted'
        import shutil
        copy = shutil.copyfile
        count = 0

        def interrupted(source, target):
            nonlocal count
            count += 1
            if count == 2:
                Path(target).write_bytes(b'interrupted partial PNG')
                raise OSError('simulated interruption')
            return copy(source, target)

        with patch('enshrouded_data.map_tool.delivery.shutil.copyfile', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interruption'):
                write_world_delivery(tiles, root, layout='tiles', build_id=123, grid=2)
        first_hash = hashlib.sha256((root / 'tiles/0_0.png').read_bytes()).hexdigest()
        first_mtime = (root / 'tiles/0_0.png').stat().st_mtime_ns
        result = write_world_delivery(tiles, root, layout='tiles', build_id=123, grid=2, resume=True)
        self.assertEqual(len(result['tiles']), 4)
        self.assertEqual(hashlib.sha256((root / 'tiles/0_0.png').read_bytes()).hexdigest(), first_hash)
        self.assertEqual((root / 'tiles/0_0.png').stat().st_mtime_ns, first_mtime)
        self.assertEqual(verify_world_delivery(root), result)
        (root / 'tiles/0_0.png').write_bytes(b'changed completed image')
        with self.assertRaisesRegex(ValueError, 'changed'):
            write_world_delivery(tiles, root, layout='tiles', build_id=123, grid=2, resume=True)

    def test_resume_does_not_reseal_changed_options_or_descriptions(self):
        tiles = self.tiles()
        root = self.root / 'finished'
        write_world_delivery(tiles, root, layout='tiles', build_id=123, grid=2)
        with self.assertRaisesRegex(ValueError, 'options'):
            write_world_delivery(tiles, root, layout='both', build_id=123, grid=2, resume=True)
        (root / 'descriptions/0_0.wikitext').write_text('local edit')
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_world_delivery(root)

    @unittest.skipUnless(os.environ.get('ENSHROUDED_MAP_JPEG_HELPER'), 'Set helper path for actual native JPEG verification')
    def test_native_jpeg_full_pixel_metrics_and_actual_header(self):
        path = self.root / 'smooth.png'
        edge = 64
        rows = [bytes(channel for x in range(edge) for channel in (x * 2 + 20, y * 2 + 20, 100))
                for y in range(edge)]
        original = write_png(path, edge, edge, rows)
        result = convert_wiki_jpeg(path, self.root / 'smooth.jpg', os.environ['ENSHROUDED_MAP_JPEG_HELPER'],
                                   expected_rgb_sha256=original['rgb_sha256'])
        self.assertEqual(result['dimensions'], [64, 64])
        self.assertEqual(result['quality']['compared_pixels'], edge * edge)
        self.assertFalse(result['resampling'])
        self.assertTrue(result['upload_eligible'])
        self.assertEqual(result['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
