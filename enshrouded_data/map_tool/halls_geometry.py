"""Match authored scene controls to enclosed floors and hall map landmarks."""
from array import array
from collections import Counter, defaultdict
import math


def contains(box, position):
    """Test a world position against inclusive authored bounds."""
    return all(box['min'][axis] <= position[axis] <= box['max'][axis]
               for axis in 'xyz')


def box_contains_floor(record, boxes):
    """Test the first air cell's centre against half-open world bounds."""
    x, z, lo, _ = record[:4]
    position = {'x': (x + .5) * .5, 'z': (z + .5) * .5,
                'y': (lo + .5) * .5}
    return any(all(box['min'][a] <= position[a] < box['max'][a]
                   for a in 'xyz') for box in boxes)


def match_seeds(values, labels, seeds, radius=4, *, floor_distance=False):
    """Find each control's nearest interval within an X/Z search square.

    ``radius`` is in half-metre cells. Vertical distance is measured to the
    air interval, or to its floor when ``floor_distance`` is true.
    """
    # Index nearby columns once so interval scanning only visits local controls.
    search = defaultdict(list)
    for index, seed in enumerate(seeds):
        p = seed['position']
        x, z = math.floor(p['x'] * 2), math.floor(p['z'] * 2)
        for dz in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                search[((z + dz) << 16) | (x + dx)].append(index)
    best = [None] * len(seeds)
    for i, component in enumerate(labels):
        x, z, lo, hi = values[i * 6:i * 6 + 4]
        candidates = search.get((z << 16) | x)
        if not candidates:
            continue
        for index in candidates:
            p = seeds[index]['position']
            y = p['y'] * 2
            dy = abs(lo - y) if floor_distance else max(lo - y, y - (hi - .001), 0)
            distance = ((x + .5) - p['x'] * 2) ** 2 + ((z + .5) - p['z'] * 2) ** 2 + dy ** 2
            if best[index] is None or distance < best[index][0]:
                best[index] = (distance, component, i)
    return [{**seed, 'match': None if result is None else {
        'distance_m': math.sqrt(result[0]) * .5,
        'component': result[1], 'record_index': result[2],
        'interval': list(values[result[2] * 6:result[2] * 6 + 6])}}
        for seed, result in zip(seeds, best)]


def floor_seeds(layout, chests):
    """Collect entrances, spawners and unambiguous scene/loot controls by hall."""
    seeds = []
    for index, hall in enumerate(layout['halls']):
        seeds.append({'hall': index, 'kind': 'entrance',
                      'position': hall['teleport_to']['position']})
        for row in hall['spawner_controls']:
            seeds.append({'hall': index, 'kind': 'spawner', 'position': row['position']})
    by_name = {hall['name']: index for index, hall in enumerate(layout['halls'])}
    useful = {'keen::ecs::PlayerSpawnPoint', 'keen::ecs::TeleportZone',
              'keen::ecs::SceneKnowledgeTrigger'}
    for feature in layout['scene_feature_controls']:
        owners = feature['hall_candidates']
        if len(owners) == 1 and useful.intersection(feature['components']):
            seeds.append({'hall': by_name[owners[0]], 'kind': 'scene_control',
                          'native_id': feature['native_id'], 'position': feature['position']})
    for chest in chests['chests']:
        seeds.append({'hall': by_name[chest['hall_name']], 'kind': 'chest',
                      'native_id': chest['native_id'], 'position': chest['position']})
    return seeds


def select_floors(values, labels, layout, chests):
    """Assign components using controls within 3 m of floors or 1 m of air.

    Return supported hall indices per component and both sets of seed matches.
    """
    if len(values) != 6 * len(labels):
        raise ValueError('Different interval/component lengths')
    seeds = floor_seeds(layout, chests)
    matches = match_seeds(values, labels, seeds, floor_distance=True)
    air_matches = match_seeds(values, labels, seeds, radius=8)
    supported = defaultdict(set)
    for row, air in zip(matches, air_matches):
        row['air_match'] = air['match']
        if row['match'] and row['match']['distance_m'] <= 3:
            supported[row['match']['component']].add(row['hall'])
        if row['air_match'] and row['air_match']['distance_m'] <= 1:
            supported[row['air_match']['component']].add(row['hall'])
    return supported, {'record_count': len(labels),
        'component_count': len(Counter(labels)), 'seed_matches': matches,
        'seed_component_halls': {str(key): sorted(value) for key, value in supported.items()}}


def map_markers(hall, layout, values):
    """Validate landmarks near selected floors, retaining exact scene positions."""
    markers = [{'position': hall['teleport_to']['position'], 'label': 'ENTRANCE',
                'kind': 'entrance', 'native_id': hall['teleport_to']['native_id']}]
    for feature in layout['scene_feature_controls']:
        if feature['hall_candidates'] != [hall['name']] or feature['native_id'] == hall['teleport_to']['native_id']:
            continue
        tele = feature['teleporter']
        spawn = feature['components'].get('keen::ecs::PlayerSpawnPoint')
        if tele.get('TeleportTargetId') == hall['teleport_from']['teleporter_id']:
            kind, label = 'exit', 'EXIT'
        elif tele.get('TeleporterId') or 'keen::ecs::TeleportZone' in feature['components']:
            kind, label = 'portal', 'PORTAL'
        elif spawn and spawn.get('target') == 'SavePoint':
            kind, label = 'checkpoint', ''
        else:
            continue
        markers.append({'position': feature['position'], 'label': label,
                        'kind': kind, 'native_id': feature['native_id']})
    results = []
    for row in match_seeds(values, array('I', [0]) * (len(values) // 6), markers,
                           floor_distance=True):
        if row['match'] is None or row['match']['distance_m'] > 4:
            raise ValueError('Map landmark has no nearby selected floor: ' + row['native_id'])
        p = row['position']
        results.append({**row, 'gx': p['x'] * 2, 'gz': p['z'] * 2,
                        'y_cells': p['y'] * 2, 'panels': [0]})
    return results
