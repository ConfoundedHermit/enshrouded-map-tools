"""Generate six flattened hall maps from a contributor's extracted game files.

All geometry, volumes and controls come from the supplied installation. The
small supported-build profile contains identities and validation expectations;
it supplies no geometry, floor masks, seed coordinates or historical exports.
"""
from array import array
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess

from .hall_flatten import render_flat_map
from .halls_scene import position_world, spawn_overrides
from .halls_geometry import contains, box_contains_floor, map_markers, select_floors


FEATURE_NAMES = ('PlayerSpawnPoint', 'TeleportZone', 'Ladder', 'HookShotAnchor',
    'GliderUpstreamZone', 'MagicalBarrier', 'MovingPlatform', 'InteractionLock',
    'SceneKnowledgeTrigger', 'LoreReading', 'WaterDrain', 'BlockWater',
    'TriggerBoxMultiSpawner', 'MapMarker')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path, value):
    path = Path(path)
    if path.exists():
        raise ValueError('Preserve existing output: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def _source(path, root):
    path = Path(path)
    return {'path': path.relative_to(root).as_posix(), 'sha256': _sha(path),
            'size_bytes': path.stat().st_size}


def load_profile(profile):
    """Accept a selected build profile or the halls identity object itself."""
    if 'halls' in profile and isinstance(profile['halls'], list) and 'scene_guid' in profile:
        return profile
    identity = profile.get('halls_profile', 'halls-' + str(profile['build_id']) + '.json')
    if isinstance(identity, dict):
        return identity
    path = Path(__file__).with_name('profiles') / identity
    return _read(path)


def _one(paths, description):
    paths = list(paths)
    if len(paths) != 1:
        raise ValueError('Expected one ' + description + ', found ' + str(len(paths)))
    return paths[0]


def bind_arrivals(areas, teleporters, profile):
    """Bind reciprocal native IDs to a unique authored playable-area group."""
    by_id = defaultdict(list)
    for row in teleporters:
        if row['teleporter'].get('TeleporterId'):
            by_id[row['teleporter']['TeleporterId']].append(row)
    halls = []
    used_groups = set()
    for binding in profile['halls']:
        surface_id = binding['surface_teleporter_id']
        interior_id = binding['interior_teleporter_id']
        if len(by_id[surface_id]) != 1 or len(by_id[interior_id]) != 1:
            raise ValueError('Missing or duplicate hall teleporter: ' + binding['region'])
        surface, arrival = by_id[surface_id][0], by_id[interior_id][0]
        if (surface['teleporter'].get('TeleportTargetId') != interior_id
                or arrival['teleporter'].get('TeleportTargetId') != surface_id):
            raise ValueError('Hall entrance references are not reciprocal: ' + binding['region'])
        if arrival['levels'] != {'level': 0, 'levelOffset': binding['level_offset']}:
            raise ValueError('Hall arrival level binding changed: ' + binding['region'])
        groups = {box['map_marker_id'] for box in areas if contains(box, arrival['position'])}
        if len(groups) != 1:
            raise ValueError('Hall arrival has ambiguous playable-area ownership: ' + binding['region'])
        marker_id = groups.pop()
        if marker_id in used_groups:
            raise ValueError('Multiple halls bind the same playable-area group')
        used_groups.add(marker_id)
        boxes = [box for box in areas if box['map_marker_id'] == marker_id]
        def endpoint(row):
            return {'native_id': row['native_id'], 'position': row['position'],
                    'teleporter_id': row['teleporter']['TeleporterId'],
                    'target_id': row['teleporter']['TeleportTargetId']}
        halls.append({'name': binding['region'] + ' Hollow Halls',
            'region': binding['region'], 'slug': binding['slug'],
            'level_offset': binding['level_offset'], 'map_marker_id': marker_id,
            'playable_areas': boxes, 'teleport_from': endpoint(surface),
            'teleport_to': endpoint(arrival), 'spawner_controls': []})
    return halls


def collect_controls(raw_root, profile, progress=print):
    """Discover fresh scene/template controls; return normalized layout/chests."""
    raw_root = Path(raw_root)
    profile = load_profile(profile)
    scene_id = profile['scene_guid']
    scene_path = _one((raw_root / 'SceneResource').glob(scene_id + '*.json'), 'main scene')
    scene = _read(scene_path)
    if scene.get('$guid') != scene_id:
        raise ValueError('Wrong main-scene identity')
    areas = []
    for index, row in enumerate(scene['resetPlayersOutOfBounds']['playableAreas']):
        if row['flags'] != ['BigDungeon']:
            raise ValueError('Unsupported main-scene playable-area flags')
        areas.append({'index': index, 'map_marker_id': row['mapMarkerId']['value'],
            'flags': row['flags'],
            'min': {a: row['areaMin'][a] / 2 ** 32 for a in 'xyz'},
            'max': {a: row['areaMax'][a] / 2 ** 32 for a in 'xyz'}})
    templates = {}
    sources = [_source(scene_path, raw_root)]
    for path in sorted((raw_root / 'TemplateResource').glob('*.json')):
        template = _read(path)
        guid = template['$guid']
        if guid in templates:
            raise ValueError('Duplicate template identity: ' + guid)
        templates[guid] = {'name': template['name'],
            'components': {c['$type']: c['$value'] for c in template['components']}}
        sources.append(_source(path, raw_root))
    if not templates:
        raise ValueError('No fresh templates extracted')
    chunk_paths = sorted((raw_root / 'SceneEntityChunkResource').glob(scene_id + '*.json'))
    if len(chunk_paths) != profile['expected_scene_chunk_count']:
        raise ValueError('Incomplete main-scene entity chunks')
    # Discover the entrance IDs first. Entity indices and positions are read
    # directly from this extraction rather than retained from a prior run.
    teleports = []
    chunks = []
    for path in chunk_paths:
        chunk = _read(path)
        if chunk['$guid'] != scene_id:
            raise ValueError('Unexpected scene chunk GUID')
        sources.append(_source(path, raw_root))
        chunks.append(chunk['$part'])
        for index in range(len(chunk['entities'])):
            entry = chunk['spawnData']['entries'][index] if index < len(chunk['spawnData']['entries']) else None
            if not entry or 'TeleporterId' not in entry['spawnMask']:
                continue
            override = spawn_overrides(chunk['spawnData'], index)
            teleports.append({'native_id': f'{scene_id}:{chunk["$part"]}:{index}',
                'position': position_world(chunk['entities'][index]['transform']),
                'levels': override.get('levels', {}).get('value'),
                'teleporter': {key: override[key]['value'] for key in ('TeleporterId', 'TeleportTargetId') if key in override}})
    if len(set(chunks)) != len(chunks):
        raise ValueError('Duplicate scene chunk part')
    halls = bind_arrivals(areas, teleports, profile)
    hall_by_offset = {hall['level_offset']: hall for hall in halls}
    features, chests, held = [], [], []
    spawner_counts = Counter()
    for count, path in enumerate(chunk_paths, 1):
        chunk = _read(path)
        for index, entity in enumerate(chunk['entities']):
            guid = chunk['templates'][entity['templateIndex']]
            if guid not in templates:
                raise ValueError('Missing scene template: ' + guid)
            template = templates[guid]
            defaults = template['components']
            position = position_world(entity['transform'])
            membership = [box for box in areas if contains(box, position)]
            if not membership:
                continue
            candidates = sorted({hall['name'] for hall in halls
                                 if hall['map_marker_id'] in {box['map_marker_id'] for box in membership}})
            if not candidates:
                continue  # Separate event interiors have no hall identity.
            override = spawn_overrides(chunk['spawnData'], index)
            levels = override.get('levels', {}).get('value')
            owner = hall_by_offset.get(levels['levelOffset']) if levels else None
            native_id = f'{scene_id}:{chunk["$part"]}:{index}'
            if 'keen::ecs::RandomSpawner' in defaults and owner:
                if owner['name'] not in candidates:
                    raise ValueError('Spawner level disagrees with authored hall volume: ' + native_id)
                owner['spawner_controls'].append({'native_id': native_id, 'position': position,
                    'template_guid': guid, 'debug_name': template['name']})
                spawner_counts[owner['slug']] += 1
            merged = {**defaults, **{c['$type']: c['$value'] for c in override['components']}}
            selected = {key: value for key, value in merged.items() if any(n in key for n in FEATURE_NAMES)}
            teleporter = {key: override[key]['value'] for key in ('TeleporterId', 'TeleportTargetId') if key in override}
            if selected or teleporter:
                features.append({'native_id': native_id, 'position': position,
                    'template_guid': guid, 'debug_name': template['name'],
                    'hall_candidates': candidates, 'levels': levels,
                    'teleporter': teleporter, 'components': selected})
            if 'keen::ecs::InventorySetup' not in defaults:
                continue
            short = {key.rsplit('::', 1)[-1]: value for key, value in merged.items()}
            overrides = {c['$type'].rsplit('::', 1)[-1]: c['$value'] for c in override['components']}
            inventory = short.get('DefaultInventory', {})
            default_id = inventory.get('defaultInventory')
            name = template['name'].lower()
            chest = ('lootchest' in name or 'treasurechest' in name) and ('RandomLootContainer' in short or bool(default_id))
            fixed_override = ('DefaultInventory' in overrides and default_id and not inventory.get('keepOnRandomLoot')
                and inventory != defaults.get('keen::ecs::DefaultInventory'))
            fixed_named = (default_id and not inventory.get('keepOnRandomLoot')
                and ('corpse' in name or ('coffin' in name and any(word in name for word in ('weapon', 'armor', 'key')))))
            if not (chest or fixed_override or fixed_named):
                continue
            if owner is None or owner['name'] not in candidates:
                held.append({'native_id': native_id, 'position': position, 'levels': levels})
                continue
            chests.append({'native_id': native_id, 'position': position,
                'hall_name': owner['name'], 'template_guid': guid,
                'kind': 'treasure_chest' if chest else 'explicit_fixed_loot_container'})
        if count % 400 == 0:
            progress(f'Hall controls: scanned {count}/{len(chunk_paths)} scene chunks')
    for binding, hall in zip(profile['halls'], halls):
        expected = binding.get('expected_spawners')
        if expected is not None and len(hall['spawner_controls']) != expected:
            raise ValueError('Hall spawner profile changed: ' + hall['name'])
        if not hall['spawner_controls']:
            raise ValueError('Hall has no positive spawner controls: ' + hall['name'])
    layout = {'build_id': profile['build_id'], 'scene_guid': scene_id, 'halls': halls,
        'scene_feature_controls': features, 'sources': sources,
        'coverage': {'templates': len(templates), 'scene_chunks': len(chunks),
            'scene_chunk_parts': sorted(chunks), 'spawners': dict(spawner_counts),
            'feature_controls': len(features)},
        'binding_method': 'Reciprocal native teleporter IDs and level offset, unique arrival playable-area group, all control positions decoded from fresh scene entities.'}
    chest_report = {'chests': chests, 'held_unbound_container_origins': held,
        'counts': dict(Counter(row['hall_name'] for row in chests))}
    return layout, chest_report


def _load_arrays(root):
    values, labels = array('H'), array('I')
    intervals, components = root / 'air-intervals.bin', root / 'surface-components.bin'
    if intervals.stat().st_size % 12 or components.stat().st_size * 3 != intervals.stat().st_size:
        raise ValueError('Truncated or mismatched interval/component data')
    with intervals.open('rb') as stream:
        values.fromfile(stream, intervals.stat().st_size // 2)
    with components.open('rb') as stream:
        labels.fromfile(stream, components.stat().st_size // 4)
    return values, labels


def _capture(context, output, layout, profile, progress):
    scene_id = profile['scene_guid']
    candidates = []
    for path in (Path(context.raw_root) / 'VoxelWorldResource').glob(scene_id + '*.json'):
        world = _read(path)
        if world.get('type') == 'Solid' and world.get('$part') == 0:
            candidates.append((path, world))
    if len(candidates) != 1:
        raise ValueError('Expected one main-scene Solid voxel world part zero')
    world_path, world = candidates[0]
    if (world['size']['x'] != 10240 or world['size']['z'] != 10240
            or any(world['origin'][axis] != 0 for axis in 'xyz')
            or len(world['voxelLevels']) != 7 or len(world['voxelLevels'][6]['tiles']) != 6400):
        raise ValueError('Unsupported solid voxel layout')
    boxes = [box for hall in layout['halls'] for box in hall['playable_areas']]
    xmin = max(0, math.floor(min(box['min']['x'] for box in boxes) * 2) - 32)
    xmax = min(20480, math.ceil(max(box['max']['x'] for box in boxes) * 2) + 32)
    zmin = max(0, math.floor(min(box['min']['z'] for box in boxes) * 2) - 32)
    zmax = min(20480, math.ceil(max(box['max']['z'] for box in boxes) * 2) + 32)
    bounds = [xmin, xmax, zmin, zmax, 0, math.ceil(world['size']['y'] * 2), 3]
    coords = [(tx, tz) for tz in range(zmin // 256, (zmax - 1) // 256 + 1)
              for tx in range(xmin // 256, (xmax - 1) // 256 + 1)]
    intervals = output / 'air-intervals.bin'
    if intervals.exists():
        raise ValueError('Incomplete prior hall capture; use a fresh halls directory')
    receipts = []
    arguments = [str(Path(context.hall_helper).resolve()), '--intervals', str(intervals.resolve())]
    with (output / 'interval-extraction.log').open('wb') as log:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=log, stderr=log)
        try:
            process.stdin.write(struct.pack('<10I', 0x31474848, 20480, 256, *bounds))
            for count, (tx, tz) in enumerate(coords, 1):
                reference = world['voxelLevels'][6]['tiles'][tz * 80 + tx]['hash']
                payload, receipt = context.index.content(reference)
                receipts.append({'tile_x': tx, 'tile_z': tz, 'tile_index': tz * 80 + tx, **receipt})
                process.stdin.write(struct.pack('<3I', tx, tz, len(payload)))
                process.stdin.write(payload)
                if count % 16 == 0:
                    progress(f'Hall geometry: {count}/{len(coords)} solid tiles')
            process.stdin.write(struct.pack('<I', 0xffffffff))
            process.stdin.close()
            status = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
    if status or not intervals.exists() or intervals.stat().st_size % 12:
        raise ValueError('Hall geometry capture failed; inspect interval-extraction.log')
    with (output / 'surface-components.log').open('wb') as log:
        status = subprocess.run([str(Path(context.hall_helper).resolve()), '--surface-components',
            str(intervals.resolve()), str((output / 'surface-components.bin').resolve())], stdout=log, stderr=log).returncode
    if status:
        raise ValueError('Hall floor connectivity failed; inspect surface-components.log')
    result = {'bounds_cells': bounds, 'tile_count': len(coords),
        'record_count': intervals.stat().st_size // 12,
        'voxel_descriptor': _source(world_path, Path(context.raw_root)),
        'tiles': receipts, 'minimum_enclosed_air_height_metres': 1.5,
        'maximum_adjacent_floor_rise_metres': 1,
        'helper_sha256': _sha(context.hall_helper),
        'intervals': _source(intervals, output),
        'components': _source(output / 'surface-components.bin', output)}
    _write(output / 'geometry.json', result)
    return result


def verify_halls(output):
    """Decode all delivered PNGs and verify their source geometry/projections."""
    from .delivery import verify_png
    output = Path(output)
    result = _read(output / 'halls.json')
    for delivered in result['maps']:
        receipt = _read(output / delivered['receipt'])
        image = output / delivered['image']
        if _sha(image) != delivered['sha256'] or len(receipt['panels']) != 1:
            raise ValueError('Hall image changed or has multiple panels')
        check = verify_png(image, receipt['rgb_sha256'])
        if check['dimensions'] != delivered['dimensions']:
            raise ValueError('Hall image dimensions differ')
        floors = output / delivered['floors']['path']
        if _sha(floors) != delivered['floors']['sha256']:
            raise ValueError('Hall floor source changed')
        values = array('H')
        with floors.open('rb') as stream:
            values.fromfile(stream, floors.stat().st_size // 2)
        if len(values) // 6 != delivered['source_floor_count']:
            raise ValueError('Hall floor source count differs')
        projection = {(values[i], values[i + 1]) for i in range(0, len(values), 6)}
        if len(projection) != receipt['flat_composition']['unique_xz_columns']:
            raise ValueError('Hall projection lost source columns')
        if receipt['flat_composition']['source_samples'] != len(values) // 6:
            raise ValueError('Hall projection lost lower source surfaces')
        if len(receipt['panels'][0]['markers']) != delivered['landmark_count']:
            raise ValueError('Hall landmark count differs')
    return result


def run_halls(context, output, progress=print):
    """Run fresh control extraction, native segmentation and flattened rendering."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'halls.json').exists():
        progress('Verifying completed Hollow Halls output')
        return verify_halls(output)
    profile = load_profile(context.profile)
    if (output / 'layout-controls.json').exists():
        layout, chests = _read(output / 'layout-controls.json'), _read(output / 'chest-controls.json')
        # Resume only against the exact fresh descriptors used for the controls.
        for receipt in layout['sources']:
            if _sha(Path(context.raw_root) / receipt['path']) != receipt['sha256']:
                raise ValueError('Extracted hall descriptor changed during resume')
    else:
        progress('Deriving Hollow Halls volumes, reciprocal entrances and scene controls')
        layout, chests = collect_controls(context.raw_root, profile, progress)
        _write(output / 'layout-controls.json', layout)
        _write(output / 'chest-controls.json', chests)
    if (output / 'geometry.json').exists():
        geometry = _read(output / 'geometry.json')
        for key in ('intervals', 'components'):
            if _sha(output / geometry[key]['path']) != geometry[key]['sha256']:
                raise ValueError('Hall geometry changed during resume')
    else:
        geometry = _capture(context, output, layout, profile, progress)
    values, labels = _load_arrays(output)
    progress(f'Selecting supported hall floors from {len(labels):,} enclosed intervals')
    supported, selection = select_floors(values, labels, layout, chests)
    delivered = []
    for index, (hall, binding) in enumerate(zip(layout['halls'], profile['halls'])):
        accepted = {component for component, owners in supported.items() if index in owners}
        rows = array('H')
        for at, component in enumerate(labels):
            if component in accepted:
                record = values[at * 6:at * 6 + 6]
                if box_contains_floor(record, hall['playable_areas']):
                    rows.extend(record)
        count = len(rows) // 6
        if not count:
            raise ValueError('No selected hall floors: ' + hall['name'])
        floor_path = output / (hall['slug'] + '-surfaces.bin')
        if floor_path.exists():
            if floor_path.read_bytes() != rows.tobytes():
                raise ValueError('Partial hall floor output differs')
        else:
            with floor_path.open('wb') as stream:
                rows.tofile(stream)
        if binding.get('expected_floor_count') is not None and count != binding['expected_floor_count']:
            raise ValueError('Selected floor count differs from supported build: ' + hall['name'])
        if binding.get('expected_floor_sha256') is not None and _sha(floor_path) != binding['expected_floor_sha256']:
            raise ValueError('Selected geometry differs from supported build: ' + hall['name'])
        samples = [tuple(rows[i:i + 4]) for i in range(0, len(rows), 6)]
        bounds = [min(s[0] for s in samples) - 16, min(s[1] for s in samples) - 16,
                  max(s[0] for s in samples) + 17, max(s[1] for s in samples) + 17]
        markers = map_markers(hall, layout, rows)
        if binding.get('expected_landmarks') is not None and len(markers) != binding['expected_landmarks']:
            raise ValueError('Hall landmark profile changed: ' + hall['name'])
        image_path = output / 'images' / (hall['slug'] + '-hollow-halls.png')
        receipt_path = output / (hall['slug'] + '-render.json')
        if receipt_path.exists():
            report = _read(receipt_path)
            if _sha(image_path) != report['png_sha256']:
                raise ValueError('Partial hall image differs')
        else:
            report = render_flat_map(image_path, hall['name'], bounds, samples, markers,
                build=profile['build_id'], pixels_per_metre=2,
                note='Static geometry | overlapping surfaces combined')
            report.update(hall=hall['name'], slug=hall['slug'],
                source_floor_count=count, source_floors=_source(floor_path, output),
                landmark_bindings=markers)
            _write(receipt_path, report)
        for profile_key, actual in (
                ('expected_dimensions', report['dimensions']),
                ('expected_rgb_sha256', report['rgb_sha256']),
                ('expected_projection_cells', report['flat_composition']['unique_xz_columns'])):
            if profile_key in binding and binding[profile_key] != actual:
                raise ValueError('Rendered hall differs from supported build (' + profile_key + '): ' + hall['name'])
        delivered.append({'name': hall['name'], 'slug': hall['slug'],
            'image': image_path.relative_to(output).as_posix(), 'sha256': _sha(image_path),
            'dimensions': report['dimensions'], 'panels': 1, 'source_floor_count': count,
            'landmark_count': len(markers), 'floors': _source(floor_path, output),
            'receipt': receipt_path.name})
        progress(f'Rendered {hall["name"]}: {count:,} surfaces, {len(markers)} landmarks')
    if not (output / 'selection.json').exists():
        _write(output / 'selection.json', selection)
    result = {'status': 'passed', 'build_id': profile['build_id'], 'maps': delivered,
        'map_count': len(delivered), 'source_floor_count': sum(row['source_floor_count'] for row in delivered),
        'landmark_count': sum(row['landmark_count'] for row in delivered),
        'interval_count': geometry['record_count'], 'tile_count': geometry['tile_count'],
        'projection': 'One flattened north-up X/Z map per hall, two pixels per metre.',
        'limits': 'Static voxel surfaces; mesh-only props and dynamic obstacles are omitted. Overlapping passages do not imply a traversable connection.',
        'game_writes': 0, 'network_calls': 0}
    _write(output / 'halls.json', result)
    return verify_halls(output)
