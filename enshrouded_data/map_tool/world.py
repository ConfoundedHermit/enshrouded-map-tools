"""Portable, read-only native world-background extraction and Vulkan rendering.

The source plane is world X/Z. Raster rows run north to south. A tile always
covers 1,280 world units, independently of its pixel density. The game-owned
shaders and textures are extracted from the selected installation, never bundled.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import uuid

from .delivery import png_from_rgb, verify_png


MAP_GUID = '01bcbd07-bdbf-41c0-9999-5d58fb1a3aa1'
SHADER_GUID = '7df551f7-ddd1-52b0-8ba6-d09be35dbccc'
UI_GUID = '46e14df6-fb8f-4f66-9463-e23b9e19e48e'
BALANCE_GUID = '82706b40-61b1-4b8f-8b23-dcec6971bda1'
MAP_TYPE = 3775288814
TEXTURE_TYPE = 4192758167
PROFILE_PATH = Path(__file__).with_name('profiles') / 'world-26430625.json'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def _retain(path, data):
    """A cache may be reused only if its bytes match freshly extracted input."""
    path = Path(path)
    if path.exists():
        if _sha(path) != hashlib.sha256(data).hexdigest():
            raise ValueError('Changed world input cache: ' + str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _descriptor(raw_root, category, guid, part=0):
    paths = list((Path(raw_root) / category).glob(f'{guid}_*_{part}.json'))
    if len(paths) != 1:
        raise ValueError(f'Expected one fresh {category} descriptor for {guid}, part {part}')
    path = paths[0]
    row = _read(path)
    if (row.get('$type'), row.get('$guid'), row.get('$part')) != ('keen::' + category, guid, part):
        raise ValueError('Wrong world descriptor identity: ' + str(path))
    return row, {'path': str(path.resolve()), 'sha256': _sha(path)}


def validate_parts(rows, tile_count):
    """Existing native map layout contract, applied to fresh descriptors."""
    nx, ny = tile_count
    by_part = {d['$part']: d for d in rows}
    if len(by_part) != len(rows) or set(by_part) != set(range(7 + nx * ny * 2)):
        raise ValueError('Incomplete or duplicate map texture parts')
    names = ['baseGradient', 'isolineGradient', 'fogZoneBorderGradient', 'fogNoise',
             'fogZoneNormal', 'fogZoneDangerous', 'baseHeight']
    formats = ['R8G8B8A8_unorm'] * 3 + ['BC4_unorm_block'] + ['BC7_unorm_block'] * 2 + ['R16_unorm']
    sizes = [(151, 1), (128, 1), (32, 1), (512, 512), (256, 256), (256, 256), (1280, 1280)]
    levels = [1, 1, 1, 10, 9, 9, 1]
    for part in range(7):
        d = by_part[part]
        if (d['debugName'], d['format'], d['size'], d['levelCount'], d['isTiled']) != (
                '_map_shader_parameters_' + names[part], formats[part],
                dict(zip(('x', 'y'), sizes[part])), levels[part], False):
            raise ValueError('Unexpected special map texture layout')
    for y in range(ny):
        for x in range(nx):
            for delta, name, fmt in [(0, 'DetailHeight', 'BC4_unorm_block'), (1, 'FogZone', 'BC5_unorm_block')]:
                d = by_part[7 + 2 * (y * nx + x) + delta]
                if (d['debugName'], d['format'], d['size'], d['levelCount'], d['isTiled']) != (
                        f'{name}({x},{y})', fmt, {'x': 1024, 'y': 1024}, 1, False):
                    raise ValueError('Unexpected map tile coordinates, dimensions or format')
    if any(d['$guid'] != MAP_GUID or d['$type'] != 'keen::UiTextureResource' for d in rows):
        raise ValueError('Wrong map texture identity')
    return by_part


def _payload_size(descriptor):
    w, h = descriptor['size']['x'], descriptor['size']['y']
    total = 0
    for level in range(descriptor['levelCount']):
        x, y = max(1, w >> level), max(1, h >> level)
        fmt = descriptor['format']
        if fmt == 'R8G8B8A8_unorm': total += x * y * 4
        elif fmt == 'R16_unorm': total += x * y * 2
        elif fmt == 'BC4_unorm_block': total += ((x + 3) // 4) * ((y + 3) // 4) * 8
        elif fmt in ('BC5_unorm_block', 'BC7_unorm_block'): total += ((x + 3) // 4) * ((y + 3) // 4) * 16
        else: raise ValueError('Unsupported map texture format')
    return total


def extract_world(context, output):
    """Extract all native inputs through the selected installation's content index."""
    output = Path(output)
    profile = _read(PROFILE_PATH)
    map_row, map_source = _descriptor(context.raw_root, 'UiMapResource', MAP_GUID)
    count = [map_row['tileCount']['x'], map_row['tileCount']['y']]
    if count != profile['map_tile_count']:
        raise ValueError('Unsupported world map extent; a new build profile is required')
    paths = sorted((Path(context.raw_root) / 'UiTextureResource').glob(MAP_GUID + '_*.json'))
    textures = validate_parts([_read(path) for path in paths], count)
    offset, length = context.index.location(96)
    if offset + length * 32 > len(context.index.data):
        raise ValueError('Map resource index exceeds KFC bounds')
    identities = set()
    for i in range(length):
        at = offset + i * 32
        if str(uuid.UUID(bytes_le=context.index.data[at:at + 16])) == MAP_GUID:
            key = struct.unpack_from('<II', context.index.data, at + 16)
            if key in identities:
                raise ValueError('Duplicate indexed world map resource')
            identities.add(key)
    if identities != {(TEXTURE_TYPE, part) for part in range(207)} | {(MAP_TYPE, 0)}:
        raise ValueError('Fresh map descriptor coverage differs from the resource index')
    receipts = []
    for part in range(207):
        row = textures[part]
        data, receipt = context.index.content(row['data']['hash'])
        if len(data) != _payload_size(row):
            raise ValueError(f'Map texture part {part} has an invalid complete mip-chain size')
        _retain(output / 'assets' / f'part-{part}.bin', data)
        receipts.append({'part': part, 'format': row['format'], 'size': row['size'],
                         'levels': row['levelCount'], **receipt})
    shader, shader_source = _descriptor(context.raw_root, 'GpcResource', SHADER_GUID)
    pipelines = [row for row in shader['rasterPipelines'] if row['name'] == 'MapRenderer_rgba']
    if len(pipelines) != 1:
        raise ValueError('Expected one native map shader pipeline')
    blob, shader_receipt = context.index.content(shader['strippedCode']['hash'])
    shader_outputs = []
    for name, entry in [('vsShaderCode', 'vs_main'), ('fsShaderCode', 'fs_main')]:
        stage = pipelines[0][name]
        if stage['entryPoint'] != entry or len(stage['variants']) != 1:
            raise ValueError('Native map shader entry point/variants changed')
        bounds = stage['variants'][0]['strippedCode']['range']
        start, size = bounds['start'], bounds['count']
        if start < 0 or size <= 0 or start % 4 or size % 4 or start + size > len(blob):
            raise ValueError('Native map shader range exceeds shipped content')
        code = blob[start:start + size]
        filename = name + '.spv'
        if code[:4] != b'\x03\x02\x23\x07' or hashlib.sha256(code).hexdigest() != profile['shaders'][filename]:
            raise ValueError('Unsupported or modified native map shader: ' + filename)
        _retain(output / 'shader' / filename, code)
        shader_outputs.append({'file': filename, 'sha256': hashlib.sha256(code).hexdigest(), 'range': bounds})
    report = {'status': 'passed', 'map_source': map_source, 'tile_count': count,
              'descriptor_sources': [{'path': str(p.resolve()), 'sha256': _sha(p)} for p in paths],
              'texture_receipts': receipts, 'shader_source': shader_source,
              'shader_content': shader_receipt, 'shaders': shader_outputs,
              'original_spirv': True, 'shader_rewriting': False, 'game_writes': 0}
    _json(output / 'extraction.json', report)
    return report


def _f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _config_at_zoom(configs, zoom):
    """Native descending interpolation with separate SSE float32 operations."""
    zoom = _f32(zoom)
    scales = [_f32(row['zoomScale']) for row in configs]
    if len(scales) < 2 or not math.isfinite(zoom) or zoom <= 0:
        raise ValueError('Expected a finite positive zoom and two native configurations')
    if any(not math.isfinite(s) or s <= 0 for s in scales) or any(a <= b for a, b in zip(scales, scales[1:])):
        raise ValueError('Native zoom knots must descend strictly')
    if any(len(row['isolineConfigs']) != 3 for row in configs):
        raise ValueError('Expected three native contour classes')
    for high, low in zip(configs, configs[1:]):
        hi, lo = _f32(high['zoomScale']), _f32(low['zoomScale'])
        weight = 0. if zoom < lo else 1. if zoom > hi else _f32(_f32(zoom - lo) / _f32(hi - lo))
        if zoom > lo:
            break
    selected = [{field: _f32(_f32(_f32(_f32(a[field]) - _f32(b[field])) * weight) + _f32(b[field]))
                 for field in ('thickness', 'alpha')}
                for a, b in zip(high['isolineConfigs'], low['isolineConfigs'])]
    if any(not math.isfinite(v) or v < 0 for row in selected for v in row.values()):
        raise ValueError('Invalid native contour values')
    widths = [int(max(0, min(255, _f32(_f32(row['thickness']) * 16)))) for row in selected]
    alphas = [int(max(0, min(255, _f32(_f32(row['alpha']) * 255)))) for row in selected]
    return {'zoom_scale': zoom, 'bracket_descending': [hi, lo], 'high_weight': weight,
            'isoline_configs': selected, 'widths': widths, 'alphas': alphas}


def _rva_reader(binary):
    if len(binary) < 64 or binary[:2] != b'MZ':
        raise ValueError('Expected a supported Windows executable')
    pe = struct.unpack_from('<I', binary, 60)[0]
    if pe + 24 > len(binary) or binary[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid executable PE header')
    count = struct.unpack_from('<H', binary, pe + 6)[0]
    optional = struct.unpack_from('<H', binary, pe + 20)[0]
    sections = []
    for i in range(count):
        offset = pe + 24 + optional + i * 40 + 8
        if offset + 16 > len(binary):
            raise ValueError('Invalid executable section table')
        _, rva, size, raw = struct.unpack_from('<4I', binary, offset)
        if raw + size > len(binary):
            raise ValueError('Executable section exceeds file')
        sections.append((rva, size, raw))
    def read(rva, size):
        for start, length, raw in sections:
            if start <= rva and rva + size <= start + length:
                return binary[raw + rva - start:raw + rva - start + size]
        raise ValueError('Native map witness exceeds a file-backed executable section')
    return read


def bc4_upper_bound(block):
    """Bound all selectors, including explicit 255 in six-value mode."""
    if len(block) != 8:
        raise ValueError('Expected exactly one BC4 block')
    a, b = block[:2]
    bound = max(a, b)
    if a <= b:
        bits = int.from_bytes(block[2:], 'little')
        if bits & (bits >> 1) & (bits >> 2) & 0x249249249249:
            bound = 255
    return bound


def prepare_profile(context, assets, output):
    """Derive closer-contour uniforms from fresh UI/balancing data and native witnesses."""
    assets, output = Path(assets), Path(output)
    profile = _read(PROFILE_PATH)
    executable = Path(context.game) / 'enshrouded.exe'
    binary = executable.read_bytes()
    executable_sha = hashlib.sha256(binary).hexdigest()
    if executable_sha != profile['executable_sha256']:
        raise ValueError('Native world profile requires supported client build ' + profile['build_id'])
    at = _rva_reader(binary)
    for rva, expected in profile['native_instructions'].items():
        if at(int(rva, 16), len(bytes.fromhex(expected))) != bytes.fromhex(expected):
            raise ValueError('Changed native world instruction at ' + rva)
    scalars = {}
    for name, rva in profile['native_scalar_rvas'].items():
        scalars[name] = struct.unpack('<f', at(int(rva, 16), 4))[0]
        if scalars[name] != profile['native_scalar_values'][name]:
            raise ValueError('Changed native map scalar: ' + name)
    ui, ui_source = _descriptor(context.raw_root, 'FbUiBundle', UI_GUID)
    balance, balance_source = _descriptor(context.raw_root, 'BalancingTable', BALANCE_GUID)
    selected = _config_at_zoom(ui['menu']['map']['zoomConfigs'], profile['zoom_scale'])
    if selected['widths'] != profile['packed_widths'] or selected['alphas'] != profile['packed_alphas']:
        raise ValueError('Fresh UI map contour profile changed')
    threshold = balance['flameLevelMax']
    if threshold != profile['flame_threshold']:
        raise ValueError('Fresh maximum Flame profile changed')
    alpha_range = ui['menu']['map']['fogZoneAlphaRange']
    alpha = _f32(_f32(alpha_range['x']) + _f32(_f32(alpha_range['y'] - alpha_range['x']) * _f32(.25)))
    if not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise ValueError('Invalid fresh map Shroud opacity')
    bounds = Counter()
    for part in range(8, 207, 2):
        data = (assets / 'assets' / f'part-{part}.bin').read_bytes()
        if len(data) != 1024 * 1024:
            raise ValueError('Invalid native Shroud payload size')
        for offset in range(8, len(data), 16):
            bounds[bc4_upper_bound(data[offset:offset + 8])] += 1
    if max(bounds) > threshold:
        raise ValueError('Fresh Shroud payload exceeds normal-blue Flame threshold')
    words = struct.pack('<II5fI',
                        sum(v << (8 * i) for i, v in enumerate(selected['widths'])),
                        sum(v << (8 * i) for i, v in enumerate(selected['alphas'])),
                        scalars['isoline_spacing'], float(threshold), 0.,
                        scalars['shroud_displacement'], alpha, 10 | (10 << 16))
    _retain(output / 'profile.bin', words)
    report = {'status': 'passed', 'profile_source': {'path': str(PROFILE_PATH.resolve()), 'sha256': _sha(PROFILE_PATH)},
              'executable_sha256': executable_sha, 'selected': selected, 'native_scalars': scalars,
              'native_instruction_witnesses': profile['native_instructions'], 'ui_source': ui_source,
              'balance_source': balance_source, 'flame_threshold': threshold,
              'profile_sha256': hashlib.sha256(words).hexdigest(), 'shader_data_hex': words.hex(),
              'shroud_blocks_checked': sum(bounds.values()),
              'shroud_upper_bound_histogram': dict(sorted(bounds.items())),
              'maximum_reconstructed_shroud_level': max(bounds), 'all_shroud_uses_normal_blue_variant': True,
              'animation_time_seconds': 0, 'fog_noise_offset': 0, 'fog_opacity': alpha,
              'presentation': 'Native SDR values; no extra gamma conversion',
              'current_game_frame_compared': False, 'game_execution': False, 'game_writes': 0}
    _json(output / 'profile.json', report)
    return report


def tile_entry(column, row, pixels=2560, build_id='26430625'):
    if type(column) is not int or type(row) is not int or not 0 <= column < 8 or not 0 <= row < 8:
        raise ValueError('World tile must belong to the eight-by-eight layout')
    if type(pixels) is not int or pixels not in (1280, 2560):
        raise ValueError('World tile sampling must be one or two pixels per world unit')
    return {'column': column, 'row_from_north': row,
            'image': f'Embervale_Map_{build_id}_{column}_{row}.png',
            'pixel_size': [pixels, pixels], 'raster_rectangle_world': [column * 1280, row * 1280, 1280, 1280],
            'position': [column, 7 - row],
            'world_bounds_xy': [[column * 1280, (7 - row) * 1280], [(column + 1) * 1280, (8 - row) * 1280]]}


def _run_helper(helper, arguments, log):
    env = os.environ.copy()
    env['VK_LOADER_LAYERS_DISABLE'] = '~all~'
    result = subprocess.run([str(Path(helper).resolve()), *map(str, arguments)],
                            env=env, capture_output=True, text=True, errors='replace')
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    Path(log).write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise ValueError('Native world renderer failed; see ' + str(log) + '\n' + result.stdout + result.stderr)
    return result.stdout + result.stderr


def run_world(context, output, progress=print):
    """Render 64 verified native tiles, resuming only completed, unchanged tiles.

    ``context`` owns verified installation/descriptor extraction and packaged
    helper integrity. Its world_pixels may be 1280 or 2560. The returned rows
    are accepted directly by the portable delivery assembler.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    pixels = getattr(context, 'world_pixels', 2560)
    tile_entry(0, 0, pixels)
    profile_config = _read(PROFILE_PATH)
    helper_sha = _sha(context.world_helper)
    progress('Checking Vulkan world-renderer requirements')
    _run_helper(context.world_helper, ['--probe', pixels], output / 'device-probe.txt')
    progress('Extracting 207 map textures and the original world shaders')
    assets = output / 'inputs'
    extraction = extract_world(context, assets)
    progress('Validating fresh UI contour settings and all native Shroud blocks')
    profile = prepare_profile(context, assets, output / 'profile')
    binding = {'build_id': profile_config['build_id'], 'pixels_per_tile': pixels,
               'helper_sha256': helper_sha, 'profile_sha256': profile['profile_sha256'],
               'device_probe_sha256': _sha(output / 'device-probe.txt'),
               'executable_sha256': profile['executable_sha256'],
               'textures': [row['sha256'] for row in extraction['texture_receipts']],
               'shaders': {row['file']: row['sha256'] for row in extraction['shaders']}}
    binding_path = output / 'run-inputs.json'
    if binding_path.exists():
        if not getattr(context, 'resume', False):
            raise ValueError('World work already exists; use resume or select a fresh output folder')
        if _read(binding_path) != binding:
            raise ValueError('World resume inputs changed; select a fresh output folder')
    else:
        _json(binding_path, binding)
    tiles = []
    (output / 'tiles').mkdir(exist_ok=True)
    expected_words = 'Map uniform words:' + ''.join(' ' + format(word, 'x') for word in
                                                  struct.unpack('<8I', (output / 'profile/profile.bin').read_bytes()))
    for row in range(8):
        for column in range(8):
            tile = tile_entry(column, row, pixels, binding['build_id'])
            png = output / 'tiles' / tile['image']
            rgb = png.with_suffix('.rgb')
            receipt = png.with_suffix('.json')
            log = png.with_suffix('.txt')
            if receipt.exists():
                if not getattr(context, 'resume', False):
                    raise ValueError('World tile already exists without resume')
                old = _read(receipt)
                if old['tile'] != tile or old['run_input_binding_sha256'] != _sha(binding_path):
                    raise ValueError('World resume tile geometry or source binding changed')
                verified = verify_png(png, old['rgb_sha256'])
                if verified['sha256'] != old['png_sha256'] or verified['dimensions'] != [pixels, pixels]:
                    raise ValueError('World resume tile pixels changed')
                if not log.exists() or _sha(log) != old['log_sha256']:
                    raise ValueError('World resume renderer receipt changed')
                progress(f'Verified existing world tile {len(tiles) + 1}/64')
            else:
                if png.exists() or rgb.exists():
                    # Uncommitted outputs can be left by interruption. Restrict
                    # cleanup to this tool's exact two calculated tile paths.
                    if not getattr(context, 'resume', False):
                        raise ValueError('Unfinished world tile exists; use resume')
                    for partial in (png, rgb):
                        if partial.exists():
                            if partial.is_symlink() or partial.resolve().parent != (output / 'tiles').resolve():
                                raise ValueError('Unsafe partial world tile path')
                            partial.unlink()
                progress(f'Rendering world tile {len(tiles) + 1}/64 ({column}, {row})')
                arguments = [str(assets.resolve()), str(rgb.resolve()), pixels,
                             *tile['raster_rectangle_world'], str((output / 'profile/profile.bin').resolve())]
                rendered = _run_helper(context.world_helper, arguments, log)
                if expected_words not in rendered.splitlines():
                    raise ValueError('Native renderer did not upload the verified uniform words')
                if not rgb.is_file() or rgb.stat().st_size != pixels * pixels * 3:
                    raise ValueError('Native renderer returned a truncated RGB tile')
                rgb_sha = _sha(rgb)
                def rows():
                    with rgb.open('rb') as source:
                        for _ in range(pixels):
                            yield source.read(pixels * 3)
                png_from_rgb(png, pixels, pixels, rows())
                verified = verify_png(png, rgb_sha)
                if verified['dimensions'] != [pixels, pixels]:
                    raise ValueError('Native PNG tile has wrong dimensions')
                old = {'status': 'passed', 'tile': tile, 'rgb_sha256': rgb_sha,
                       'png_sha256': verified['sha256'], 'run_input_binding_sha256': _sha(binding_path),
                       'log_sha256': _sha(log), 'original_spirv': True, 'shader_rewriting': False,
                       'all_png_crcs_checked': True, 'all_png_pixels_checked': True}
                _json(receipt, old)
                rgb.unlink()
            tiles.append({**tile, 'path': str(png.resolve()), 'rgb_sha256': old['rgb_sha256'],
                          'png_sha256': old['png_sha256'], 'receipt': str(receipt.resolve())})
            _json(output / 'progress.json', {'completed': len(tiles), 'total': 64, 'tiles': tiles})
    _json(output / 'world-render.json', {'status': 'passed', 'single_native_sampling_grid': True,
                                        'tiles': tiles, 'input_binding': binding, 'game_writes': 0})
    return tiles
