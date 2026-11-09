"""Assemble lossless maps, validate images and prepare manual-upload materials.

The streaming PNG codec accepts noninterlaced RGB8 with None or Up filters.
Assembly preserves source pixels and keeps world bounds independent of density.
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zlib

WIKI_MAX_BYTES = 15 * 1024 * 1024
_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_BLOCK = 65536


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.writing')
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError('Refusing a linked delivery output')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def _owned_path(root, name):
    """Require an output path under root with no linked path components."""
    root = Path(root).resolve()
    candidate = root / name
    if not candidate.resolve().is_relative_to(root):
        raise ValueError('Delivery output path escapes its directory')
    current = candidate
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('Refusing a linked delivery output')
        current = current.parent
    return candidate


class _DeliveryState:
    """Hash-bound checkpoints, with cleanup limited to named unfinished files."""
    def __init__(self, root, request, resume):
        self.root = Path(root).resolve()
        self.path = _owned_path(self.root, '.delivery-state.json')
        nonempty = self.root.exists() and any(self.root.iterdir())
        if nonempty:
            if not resume or not self.path.is_file():
                raise ValueError('World delivery requires a fresh output directory or its original --resume run')
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state['request'] != request:
                raise ValueError('World delivery resume options or native source tiles changed')
            for name, checksum in self.state['completed_files'].items():
                path = _owned_path(self.root, name)
                if not path.is_file() or _sha(path) != checksum:
                    raise ValueError('Completed world delivery file changed or is missing: ' + name)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.state = {'request': request, 'completed_files': {}}
            _write_json(self.path, self.state)

    def ready(self, name):
        path = _owned_path(self.root, name)
        if name in self.state['completed_files']:
            return True
        # The saved request binds this run; discard only this planned unfinished file.
        if path.exists():
            if not path.is_file():
                raise ValueError('Unfinished delivery path is not a regular file')
            path.unlink()
        return False

    def record(self, *names, **values):
        self.state.update(values)
        for name in names:
            self.state['completed_files'][name] = _sha(_owned_path(self.root, name))
        _write_json(self.path, self.state)


def verify_world_delivery(root):
    """Verify recorded files, PNG pixels and JPEG headers in a completed delivery."""
    root = Path(root).resolve()
    result = json.loads(_owned_path(root, 'delivery.json').read_text(encoding='utf-8'))
    state_path = _owned_path(root, '.delivery-state.json')
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        for name, checksum in state['completed_files'].items():
            path = _owned_path(root, name)
            if not path.is_file() or _sha(path) != checksum:
                raise ValueError('Completed world delivery file changed or is missing: ' + name)
    images = list(result['tiles'])
    if 'master' in result:
        images.append(result['master'])
    for image in images:
        path = _owned_path(root, image['local_file'])
        if _sha(path) != image['sha256']:
            raise ValueError('World delivery image changed: ' + image['local_file'])
        inspected = inspect_png(path, expected_rgb_sha256=image['rgb_sha256'])
        if inspected['dimensions'] != image['dimensions']:
            raise ValueError('World delivery image dimensions changed')
        if 'wiki_jpeg' in image:
            jpeg = image['wiki_jpeg']
            jpeg_path = _owned_path(root, jpeg['local_file'])
            if _sha(jpeg_path) != jpeg['sha256']:
                raise ValueError('Completed wiki JPEG changed')
            header = jpeg_header(jpeg_path.read_bytes())
            if [header['width'], header['height']] != image['dimensions']:
                raise ValueError('Completed wiki JPEG dimensions changed')
    for upload in result['uploads']:
        if _sha(_owned_path(root, upload['local_file'])) != upload['sha256']:
            raise ValueError('Completed manual-upload image changed')
        if not _owned_path(root, upload['description_file']).is_file():
            raise ValueError('Completed manual-upload description is missing')
    return result


def png_dimensions(path):
    """Check the header of a generated noninterlaced RGB8 PNG."""
    with Path(path).open('rb') as stream:
        header = stream.read(33)
    if (len(header) != 33 or header[:8] != _SIGNATURE
            or header[8:16] != b'\0\0\0\rIHDR'
            or header[24:29] != bytes([8, 2, 0, 0, 0])
            or zlib.crc32(header[12:29]) != int.from_bytes(header[29:33], 'big')):
        raise ValueError('Expected noninterlaced RGB8 PNG: ' + Path(path).name)
    width, height = struct.unpack('>II', header[16:24])
    if not (0 < width <= 65536 and 0 < height <= 65536):
        raise ValueError('Unsupported PNG dimensions')
    return width, height


@lru_cache(maxsize=4)
def _byte_masks(size):
    return int.from_bytes(b'\x7f' * size, 'little'), int.from_bytes(b'\x80' * size, 'little')


def _up_bytes(row, previous, *, decode):
    """Apply or reverse PNG's Up filter using carry-isolated integer byte lanes.

    Masked sums and biased differences prevent carries or borrows between bytes,
    avoiding a Python loop over each color channel.
    """
    low, high = _byte_masks(len(row))
    a, b = int.from_bytes(row, 'little'), int.from_bytes(previous, 'little')
    value = (((a & low) + (b & low)) ^ ((a ^ b) & high)) if decode else (
        ((a | high) - (b & low)) ^ ((a ^ b ^ high) & high))
    return value.to_bytes(len(row), 'little')


def _idat_blocks(stream):
    """Yield IDAT blocks up to 64 KiB, checking CRCs, order and the file end."""
    seen = False
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError('Truncated PNG chunk')
        size, kind = struct.unpack('>I4s', header)
        if kind not in (b'IDAT', b'IEND') or size > 0x7fffffff:
            raise ValueError('Unexpected PNG chunk/order')
        if kind == b'IEND' and (size or not seen):
            raise ValueError('Unexpected PNG end')
        crc = zlib.crc32(kind)
        remaining = size
        while remaining:
            block = stream.read(min(remaining, _BLOCK))
            if not block:
                raise ValueError('Truncated PNG payload')
            remaining -= len(block)
            crc = zlib.crc32(block, crc)
            yield block
        checksum = stream.read(4)
        if len(checksum) != 4 or crc != int.from_bytes(checksum, 'big'):
            raise ValueError('PNG chunk CRC mismatch')
        if kind == b'IEND':
            if stream.read(1):
                raise ValueError('Trailing PNG data')
            return
        seen = True


def png_rows(path, *, expected_rgb_sha256=None):
    """Yield decoded RGB rows with bounded decompression.

    Exhaust the iterator to validate all chunk CRCs, IEND and any expected pixel
    hash. Decompression buffers at most one filtered row at a time.
    """
    width, height = png_dimensions(path)
    stride = width * 3 + 1
    previous = bytes(stride - 1)
    pending = bytearray()
    decoder = zlib.decompressobj()
    count = 0
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.seek(33)
        for block in _idat_blocks(stream):
            while block:
                pending.extend(decoder.decompress(block, stride - len(pending)))
                block = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ValueError('Trailing PNG compressed data')
                if len(pending) == stride:
                    tag, filtered = pending[0], bytes(pending[1:])
                    pending.clear()
                    if tag == 0:
                        row = filtered
                    elif tag == 2:
                        row = _up_bytes(filtered, previous, decode=True)
                    else:
                        raise ValueError('Unsupported PNG filter in map export')
                    count += 1
                    if count > height:
                        raise ValueError('Too many PNG rows')
                    previous = row
                    digest.update(row)
                    yield row
        if pending or count != height or not decoder.eof or decoder.unused_data:
            raise ValueError('Incomplete PNG image')
    if expected_rgb_sha256 is not None and digest.hexdigest() != expected_rgb_sha256:
        raise ValueError('PNG decoded pixel hash mismatch')


def inspect_png(path, *, expected_rgb_sha256=None):
    """Fully decode and validate a PNG, returning dimensions and file/pixel hashes."""
    dimensions = png_dimensions(path)
    digest = hashlib.sha256()
    for row in png_rows(path, expected_rgb_sha256=expected_rgb_sha256):
        digest.update(row)
    return {'dimensions': list(dimensions), 'rgb_sha256': digest.hexdigest(),
            'sha256': _sha(path), 'size_bytes': Path(path).stat().st_size,
            'all_crcs_checked': True, 'all_pixels_checked': True}


def verify_png(path, expected_rgb_sha256=None):
    """Return a passed validation report after checking all PNG rows and chunks."""
    info = inspect_png(path, expected_rgb_sha256=expected_rgb_sha256)
    return {'status': 'passed', 'decoded_rgb_sha256': info['rgb_sha256'], **info}


def write_png(path, width, height, rows):
    """Write RGB rows to a fresh PNG using Up filtering and bounded row buffers."""
    path = Path(path)
    if path.exists():
        raise ValueError('Output already exists: ' + path.name)
    if not (type(width) is int and type(height) is int and 0 < width <= 65536 and 0 < height <= 65536):
        raise ValueError('Unsupported PNG dimensions')
    digest = hashlib.sha256()
    count = 0
    previous = bytes(width * 3)
    zipper = zlib.compressobj(6)

    def chunk(stream, kind, data):
        stream.write(struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data)))

    # A failed row iterator must never leave a file looking like a completed map.
    created = False
    try:
        with path.open('xb') as stream:
            created = True
            stream.write(_SIGNATURE)
            chunk(stream, b'IHDR', struct.pack('>2I5B', width, height, 8, 2, 0, 0, 0))
            for row in rows:
                if len(row) != width * 3 or count >= height:
                    raise ValueError('Wrong RGB row dimensions')
                digest.update(row)
                compressed = zipper.compress(b'\x02' + _up_bytes(row, previous, decode=False))
                if compressed:
                    chunk(stream, b'IDAT', compressed)
                previous = row
                count += 1
            if count != height:
                raise ValueError('Wrong RGB image height')
            chunk(stream, b'IDAT', zipper.flush())
            chunk(stream, b'IEND', b'')
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    return {'dimensions': [width, height], 'rgb_sha256': digest.hexdigest(),
            'sha256': _sha(path), 'size_bytes': path.stat().st_size}


png_from_rgb = write_png


def _tiles(tile_rows, grid, tile_world_units):
    """Validate tile geometry and return the complete grid in north-to-south order."""
    if type(grid) is not int or not 1 <= grid <= 16:
        raise ValueError('Invalid world tile grid')
    if type(tile_world_units) is not int or tile_world_units <= 0:
        raise ValueError('Invalid tile world extent')
    rows = list(tile_rows)
    if len(rows) != grid * grid:
        raise ValueError('Incomplete world tile set')
    indexed = {}
    dimensions = None
    for source in rows:
        column, row = source['column'], source['row_from_north']
        if (type(column) is not int or type(row) is not int or not 0 <= column < grid
                or not 0 <= row < grid or (column, row) in indexed):
            raise ValueError('Invalid or duplicate world tile coordinate')
        path = Path(source['path'])
        size = png_dimensions(path)
        if size[0] != size[1] or (dimensions is not None and dimensions != size):
            raise ValueError('World tiles must share square pixel dimensions')
        dimensions = size
        bounds = [[column * tile_world_units, (grid - 1 - row) * tile_world_units],
                  [(column + 1) * tile_world_units, (grid - row) * tile_world_units]]
        if 'world_bounds_xy' in source and source['world_bounds_xy'] != bounds:
            raise ValueError('World bounds disagree with north-origin tile coordinates')
        if 'position' in source and source['position'] != [column, grid - 1 - row]:
            raise ValueError('World tile position is flipped')
        if 'png_sha256' in source and _sha(path) != source['png_sha256']:
            raise ValueError('World source PNG hash mismatch')
        indexed[column, row] = {**source, 'path': path, 'world_bounds_xy': bounds,
                                'position': [column, grid - 1 - row]}
    return [indexed[c, r] for r in range(grid) for c in range(grid)], dimensions[0]


def stitch_world_tiles(tile_rows, output_path, grid=8, *, tile_world_units=1280):
    """Join tiles north to south without resampling or allocating a full image.

    Rows supply path, column and row_from_north, with optional expected file/pixel
    hashes and world bounds. Pixel dimensions do not set the tiles' world extent.
    """
    tiles, edge = _tiles(tile_rows, grid, tile_world_units)

    def rows():
        for north in range(grid):
            with ExitStack() as stack:
                decoders = [png_rows(t['path'], expected_rgb_sha256=t.get('rgb_sha256'))
                            for t in tiles[north * grid:(north + 1) * grid]]
                for decoder in decoders:
                    stack.callback(decoder.close)
                for _ in range(edge):
                    yield b''.join(next(decoder) for decoder in decoders)
                # Exhaust each iterator to validate the final CRC/IEND and hash.
                if any(next(decoder, None) is not None for decoder in decoders):
                    raise ValueError('Extra rows in a world tile')

    result = write_png(output_path, edge * grid, edge * grid, rows())
    verified = inspect_png(output_path, expected_rgb_sha256=result['rgb_sha256'])
    return {**result, 'all_crcs_checked': verified['all_crcs_checked'],
            'all_pixels_checked': True, 'lossless_tile_assembly': True,
            'world_bounds_xy': [[0, 0], [grid * tile_world_units, grid * tile_world_units]],
            'pixels_per_world_unit': edge / tile_world_units, 'north_up': True,
            'working_memory': 'One tile row per decoder plus one output row and bounded 64-KiB input blocks',
            'full_image_allocated': False, 'tile_count': grid * grid}


def jpeg_header(data):
    """Check encoded JPEG dimensions, all-one quantization and 4:4:4 sampling."""
    if data[:2] != b'\xff\xd8' or data[-2:] != b'\xff\xd9':
        raise ValueError('Expected complete JPEG')
    at, frame, tables = 2, None, {}
    while at < len(data):
        if data[at] != 255:
            raise ValueError('Malformed JPEG marker')
        while at < len(data) and data[at] == 255:
            at += 1
        if at >= len(data):
            raise ValueError('Truncated JPEG marker')
        kind = data[at]
        at += 1
        if kind in (0xda, 0xd9):
            break
        if at + 2 > len(data):
            raise ValueError('Truncated JPEG length')
        size = int.from_bytes(data[at:at + 2], 'big')
        body = data[at + 2:at + size]
        at += size
        if size < 2 or len(body) != size - 2:
            raise ValueError('Truncated JPEG segment')
        if kind == 0xdb:
            p = 0
            while p < len(body):
                tag = body[p]
                p += 1
                if tag >> 4 or tag > 3 or len(body) - p < 64 or tag in tables:
                    raise ValueError('Invalid JPEG quantization table')
                tables[tag] = list(body[p:p + 64])
                p += 64
        if kind in (0xc0, 0xc1, 0xc2):
            if len(body) < 6 or frame:
                raise ValueError('Invalid JPEG frame')
            precision, height, width, count = struct.unpack('>BHHB', body[:6])
            if len(body) != 6 + 3 * count or not width or not height:
                raise ValueError('Invalid JPEG frame dimensions')
            frame = {'precision': precision, 'width': width, 'height': height,
                     'components': [{'id': body[6 + 3 * i], 'sampling': body[7 + 3 * i],
                                     'quantization_table': body[8 + 3 * i]} for i in range(count)]}
    if (not frame or frame['precision'] != 8 or len(frame['components']) != 3
            or any(c['sampling'] != 0x11 or c['quantization_table'] not in tables
                   for c in frame['components'])):
        raise ValueError('JPEG did not preserve RGB8-equivalent 4:4:4 sampling')
    if not tables or any(value != 1 for table in tables.values() for value in table):
        raise ValueError('JPEG Q100 quantization was not honored')
    return {**frame, 'quantization_tables': tables}


def convert_wiki_jpeg(source_png, output_path, helper, *, expected_rgb_sha256=None,
                      max_upload_bytes=WIKI_MAX_BYTES):
    """Convert through WIC and validate encoded settings and pixel-error metrics."""
    source = inspect_png(source_png, expected_rgb_sha256=expected_rgb_sha256)
    output_path = Path(output_path)
    if output_path.exists():
        raise ValueError('JPEG output already exists')
    result = subprocess.run([str(Path(helper).resolve()), str(Path(source_png).resolve()),
                             str(output_path.resolve())], capture_output=True, text=True)
    if result.returncode or not output_path.is_file():
        raise ValueError('Native JPEG conversion failed: ' + result.stderr.strip())
    quality = json.loads(result.stdout)
    header = jpeg_header(output_path.read_bytes())
    dimensions = [quality['width'], quality['height']]
    if (dimensions != source['dimensions'] or [header['width'], header['height']] != dimensions
            or quality['source_rgb_sha256'] != source['rgb_sha256']
            or quality['resampling'] is not False or quality['quality'] != 100
            or quality['requested_chroma'] != '4:4:4'
            or quality['compared_pixels'] != dimensions[0] * dimensions[1]):
        raise ValueError('Native JPEG conversion changed image geometry or source pixels')
    if (quality['psnr_db'] < 50 or quality['maximum_channel_error'] > 12
            or quality['pixel_error_p999'] > 3):
        raise ValueError('JPEG quality thresholds failed; retained output requires review')
    return {'dimensions': dimensions, 'sha256': _sha(output_path),
            'size_bytes': output_path.stat().st_size, 'quality': quality,
            'jpeg_header': header, 'status': 'passed',
            'upload_eligible': output_path.stat().st_size <= max_upload_bytes,
            'max_upload_bytes': max_upload_bytes, 'resampling': False}


def _description(build_id, *, column=None, north=None, bounds=None, dimensions=None):
    target = ('Full-resolution world map background' if column is None else
              f'World map background tile, column {column}, row {north} counted from north')
    text = ('== Summary ==\n' + target + f', generated from Enshrouded client build {build_id}.\n\n'
            'Rendered from the game map textures and shaders. North is up and east is right. '
            f'Original dimensions: {dimensions[0]} × {dimensions[1]} pixels. No resampling.\n')
    if bounds is not None:
        text += f'\nWorld bounds (X/Y): {bounds[0]} to {bounds[1]}.\n'
    return text


def write_world_delivery(tile_rows, output_root, *, layout='both', build_id,
                         jpeg_helper=None, grid=8, tile_world_units=1280,
                         max_upload_bytes=WIKI_MAX_BYTES, resume=False):
    """Write the selected world layout, optional JPEG tiles and upload records.

    Records flag files above the configured upload limit without resizing them.
    JPEG tiles require a native helper; otherwise tile upload records use PNGs.
    """
    if layout not in ('single', 'tiles', 'both'):
        raise ValueError('World layout must be single, tiles or both')
    if not re.fullmatch(r'[0-9]+', str(build_id)):
        raise ValueError('Build ID must contain digits only')
    if type(max_upload_bytes) is not int or max_upload_bytes <= 0:
        raise ValueError('Invalid upload size limit')
    tiles, edge = _tiles(tile_rows, grid, tile_world_units)
    root = Path(output_root).resolve()
    request = {'layout': layout, 'build_id': str(build_id), 'grid': grid,
               'tile_world_units': tile_world_units, 'max_upload_bytes': max_upload_bytes,
               'jpeg_helper_sha256': _sha(jpeg_helper) if jpeg_helper is not None else None,
               'source_tiles': [{'column': t['column'], 'row_from_north': t['row_from_north'],
                                 'png_sha256': _sha(t['path']), 'rgb_sha256': t.get('rgb_sha256')}
                                for t in tiles]}
    checkpoint = _DeliveryState(root, request, resume)
    if (root / 'delivery.json').is_file():
        return verify_world_delivery(root)
    _owned_path(root, 'descriptions').mkdir(exist_ok=True)
    result = {'schema_version': 1, 'status': 'complete', 'build_id': str(build_id),
              'layout': layout, 'grid': [grid, grid], 'north_up': True,
              'tile_pixel_size': [edge, edge], 'tile_world_units': tile_world_units,
              'pixels_per_world_unit': edge / tile_world_units,
              'world_bounds_xy': [[0, 0], [grid * tile_world_units, grid * tile_world_units]],
              'max_upload_bytes': max_upload_bytes, 'tiles': [], 'uploads': [], 'network_requests': 0}

    def upload_entry(local, wiki_name, info, description):
        desc = 'descriptions/' + Path(local).stem + '.wikitext'
        _owned_path(root, desc).write_text(description, encoding='utf-8')
        checkpoint.record(desc)
        eligible = info['size_bytes'] <= max_upload_bytes
        entry = {'local_file': local, 'wiki_filename': wiki_name, 'description_file': desc,
                 'sha256': info['sha256'], 'size_bytes': info['size_bytes'],
                 'dimensions': info['dimensions'], 'upload_eligible': eligible,
                 'reason': 'Within configured file-size limit; review before upload' if eligible else
                           'Exceeds configured wiki file-size limit; keep as local master and use tiles'}
        result['uploads'].append(entry)
        return entry

    # The master is assembled losslessly and checkpointed independently of tiles.
    if layout in ('single', 'both'):
        _owned_path(root, 'master').mkdir(exist_ok=True)
        local = 'master/Embervale.png'
        if checkpoint.ready(local):
            info = checkpoint.state['master']
            inspect_png(root / local, expected_rgb_sha256=info['rgb_sha256'])
        else:
            info = stitch_world_tiles(tiles, _owned_path(root, local), grid, tile_world_units=tile_world_units)
            checkpoint.record(local, master=info)
        result['master'] = {'local_file': local, **info}
        upload_entry(local, f'Embervale Map {build_id}.png', info,
                     _description(build_id, bounds=result['world_bounds_xy'], dimensions=info['dimensions']))
    if layout in ('tiles', 'both'):
        _owned_path(root, 'tiles').mkdir(exist_ok=True)
        if jpeg_helper is not None:
            _owned_path(root, 'wiki').mkdir(exist_ok=True)
            _owned_path(root, 'quality').mkdir(exist_ok=True)
        for tile in tiles:
            c, r = tile['column'], tile['row_from_north']
            name = f'{c}_{r}'
            local = f'tiles/{name}.png'
            if not checkpoint.ready(local):
                shutil.copyfile(tile['path'], _owned_path(root, local))
            info = inspect_png(root / local, expected_rgb_sha256=tile.get('rgb_sha256'))
            if info['sha256'] != request['source_tiles'][r * grid + c]['png_sha256']:
                raise ValueError('World tile changed during delivery copy')
            checkpoint.record(local)
            item = {'column': c, 'row_from_north': r, 'position': tile['position'],
                    'world_bounds_xy': tile['world_bounds_xy'], 'local_file': local, **info}
            description = _description(build_id, column=c, north=r,
                                       bounds=tile['world_bounds_xy'], dimensions=info['dimensions'])
            if jpeg_helper is not None:
                wiki_local = f'wiki/{name}.jpg'
                quality_file = f'quality/{name}.json'
                jpeg_ready = checkpoint.ready(wiki_local)
                quality_ready = checkpoint.ready(quality_file)
                # A reusable derivative requires both the image and its quality report.
                if jpeg_ready != quality_ready:
                    raise ValueError('Incomplete JPEG comparison checkpoint')
                if jpeg_ready:
                    jpeg = json.loads((root / quality_file).read_text(encoding='utf-8'))
                    if jpeg['quality']['source_rgb_sha256'] != info['rgb_sha256']:
                        raise ValueError('Retained JPEG comparison uses different source pixels')
                else:
                    jpeg = convert_wiki_jpeg(root / local, _owned_path(root, wiki_local), jpeg_helper,
                                             expected_rgb_sha256=info['rgb_sha256'], max_upload_bytes=max_upload_bytes)
                    _write_json(_owned_path(root, quality_file), jpeg)
                    checkpoint.record(wiki_local, quality_file)
                item['wiki_jpeg'] = {'local_file': wiki_local, 'quality_file': quality_file,
                                     **{k: jpeg[k] for k in ('sha256', 'size_bytes', 'upload_eligible')}}
                upload_entry(wiki_local, name + '.jpg', jpeg, description)
            else:
                entry = upload_entry(local, f'Embervale Map {build_id} {name}.png', info, description)
                entry['reason'] += '; PNG names require a reviewed background-definition update'
            result['tiles'].append(item)
    # Record delivery completion only after all images and review materials exist.
    _write_json(_owned_path(root, 'manual-upload.json'), {'network_requests': 0, 'uploads': result['uploads']})
    _write_gallery(root, result)
    _write_json(_owned_path(root, 'delivery.json'), result)
    checkpoint.record('manual-upload.json', 'index.html', 'delivery.json')
    return result


def _write_gallery(root, delivery):
    esc = html.escape
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><title>Generated world map</title>',
             '<style>body{font:16px system-ui;margin:2rem;background:#17202b;color:#eee}'
             'a{color:#9ee0ff}.tiles{display:grid;max-width:1200px;gap:0}'
             '.tiles img{display:block;width:100%}td,th{padding:.4rem;text-align:left}'
             'table{border-collapse:collapse}tr{border-bottom:1px solid #526}</style>',
             '<h1>Generated world map</h1><p>Build ' + esc(delivery['build_id']) +
             '. North is up. Review the images and validation report before manual upload.</p>']
    if 'master' in delivery:
        m = delivery['master']
        parts.append('<p><a href="' + esc(m['local_file'], quote=True) + '">Download full-resolution PNG master</a> (' +
                     ' × '.join(map(str, m['dimensions'])) + ' pixels).</p>')
    if delivery['tiles']:
        parts.append('<div class="tiles" style="grid-template-columns:repeat(' +
                     str(delivery['grid'][0]) + ',1fr)">')
        for tile in delivery['tiles']:
            path = tile['local_file']
            parts.append('<a href="' + esc(path, quote=True) + '"><img loading="lazy" src="' +
                         esc(path, quote=True) + '" alt="Tile ' + str(tile['column']) + ',' +
                         str(tile['row_from_north']) + '"></a>')
        parts.append('</div>')
    parts += ['<h2>Manual upload</h2><ol><li>Review every map and delivery.json. '
              'Use only files marked within the configured upload limit.</li>'
              '<li>Sign into your own wiki account and open the upload page. '
              'For an existing image, use its “Upload a new version” link and preserve its current description.</li>'
              '<li>Match each filename and tile position exactly. Compare the current wiki image before replacing it. '
              'For new files, use the supplied description after review.</li>'
              '<li>Check the uploaded images and rendered map. Changing PNG filenames in a protected background '
              'definition may require an administrator.</li></ol>',
              '<p>The size check uses a configured limit of ' + str(delivery['max_upload_bytes']) +
              ' bytes. Check current wiki limits before uploading. This program makes no network requests.</p>',
              '<table><tr><th>Wiki filename</th><th>File</th><th>Description</th><th>Size check</th></tr>']
    for upload in delivery['uploads']:
        parts.append('<tr><td>' + esc(upload['wiki_filename']) + '</td><td><a href="' +
                     esc(upload['local_file'], quote=True) + '">Download</a></td><td><a href="' +
                     esc(upload['description_file'], quote=True) + '">Wikitext</a></td><td>' +
                     str(upload['size_bytes']) + ' bytes; ' + esc(upload['reason']) + '</td></tr>')
    parts.append('</table></html>')
    _owned_path(root, 'index.html').write_text('\n'.join(parts), encoding='utf-8')
