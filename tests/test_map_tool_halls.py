"""Synthetic scene, arrival binding, floor selection and marker projection checks."""
from array import array
import json
from pathlib import Path
import tempfile
import unittest

from enshrouded_data.map_tool.halls import bind_arrivals, collect_controls
from enshrouded_data.map_tool.halls_geometry import (
    box_contains_floor, map_markers, select_floors,
)


SCENE = '00000000-0000-0000-0000-000000000001'


def position(x, y, z):
    """Encode world coordinates as split 16.16 transform fields."""
    result = {}
    for axis, coordinate in zip('XYZ', (x, y, z)):
        encoded = round(coordinate * 65536) & 0xffffffff
        result['position' + axis + '0'] = encoded & 65535
        result['position' + axis + '1'] = encoded >> 16
    return result


def fixture(root):
    """Write a minimal synthetic scene extraction and return its hall profile."""
    profile = {'build_id': 'test', 'scene_guid': SCENE, 'expected_scene_chunk_count': 1,
        'halls': [{'region': 'Test', 'slug': 'test', 'level_offset': 9,
                  'surface_teleporter_id': 'surface', 'interior_teleporter_id': 'interior',
                  'expected_spawners': 1}]}
    box = {'flags': ['BigDungeon'], 'mapMarkerId': {'value': 123},
           'areaMin': {a: 0 for a in 'xyz'},
           'areaMax': {a: 20 * 2 ** 32 for a in 'xyz'}}
    scene = {'$guid': SCENE, 'resetPlayersOutOfBounds': {'playableAreas': [box]}}
    kinds = {
        'tele': ('Teleport', [{'${unused}': None, '$type': 'keen::ecs::TeleportZone', '$value': {'zoneTag': 'zone'}}]),
        'spawn': ('Enemy', [{'$type': 'keen::ecs::RandomSpawner', '$value': {}}]),
        'chest': ('LootChest_Random', [
            {'$type': 'keen::ecs::InventorySetup', '$value': {}},
            {'$type': 'keen::ecs::RandomLootContainer', '$value': {}}]),
        'save': ('SavePoint', [{'$type': 'keen::ecs::PlayerSpawnPoint', '$value': {'target': 'SavePoint'}}]),
    }
    entities = [
        {'templateIndex': 0, 'transform': position(30, 4, 30)},
        {'templateIndex': 0, 'transform': position(5.25, 2, 10.25)},
        {'templateIndex': 1, 'transform': position(6.25, 3, 10.25)},
        {'templateIndex': 2, 'transform': position(7.25, 2, 10.25)},
        {'templateIndex': 3, 'transform': position(8.25, 2, 10.25)},
        {'templateIndex': 0, 'transform': position(9.25, 2, 10.25)},
    ]
    masks = [['TeleporterId', 'TeleportTargetId'], ['Level', 'TeleporterId', 'TeleportTargetId'],
             ['Level'], ['Level'], [], ['TeleportTargetId']]
    packed = [[0, 0, 1], [0, 0, 1, 0], [0, 0], [0, 0], [0], [0, 0]]
    entries, indices = [], []
    for mask, data in zip(masks, packed):
        entries.append({'spawnIndexStart': len(indices), 'spawnMask': mask})
        indices.extend(data)
    chunk = {'$guid': SCENE, '$part': 0, 'templates': list(kinds), 'entities': entities,
        'spawnData': {'entries': entries, 'indices': indices, 'components': [],
                      'levels': [{'level': 0, 'levelOffset': 9}], 'locaTags': ['surface', 'interior']}}
    payloads = [('SceneResource', SCENE + '_0.json', scene),
                ('SceneEntityChunkResource', SCENE + '_0.json', chunk)]
    for guid, (name, components) in kinds.items():
        payloads.append(('TemplateResource', guid + '_0.json',
                         {'$guid': guid, 'name': name, 'components': components}))
    for category, name, data in payloads:
        directory = root / category
        directory.mkdir(exist_ok=True)
        (directory / name).write_text(json.dumps(data), encoding='utf-8')
    return profile


class ContributorHallTests(unittest.TestCase):
    def test_fresh_scene_drives_reciprocal_arrivals_spawners_chests_and_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = fixture(root)
            layout, chests = collect_controls(root, profile, lambda _: None)
        hall = layout['halls'][0]
        self.assertEqual(hall['teleport_to']['native_id'], SCENE + ':0:1')
        self.assertEqual(hall['teleport_to']['position'], {'x': 5.25, 'y': 2, 'z': 10.25})
        self.assertEqual(hall['map_marker_id'], 123)
        self.assertEqual([s['native_id'] for s in hall['spawner_controls']], [SCENE + ':0:2'])
        self.assertEqual([c['native_id'] for c in chests['chests']], [SCENE + ':0:3'])
        # Simulated native interval output, independent of scene normalization.
        values = array('H', [value for x in range(10, 20) for value in (x, 20, 4, 14, 0xff80, 0xff80)])
        labels = array('I', [3] * 10)
        supported, evidence = select_floors(values, labels, layout, chests)
        self.assertEqual(dict(supported), {3: {0}})
        self.assertEqual(len(evidence['seed_matches']), 6)
        markers = map_markers(hall, layout, values)
        self.assertEqual([m['kind'] for m in markers], ['entrance', 'checkpoint', 'exit'])
        self.assertEqual([m['gx'] for m in markers], [10.5, 16.5, 18.5])

    def test_changed_reciprocal_id_rejected_in_fresh_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = fixture(root)
            path = root / 'SceneEntityChunkResource' / (SCENE + '_0.json')
            chunk = json.loads(path.read_text())
            chunk['spawnData']['indices'][2] = 0  # Surface incorrectly targets itself.
            path.write_text(json.dumps(chunk))
            with self.assertRaisesRegex(ValueError, 'not reciprocal'):
                collect_controls(root, profile, lambda _: None)

    def test_overlapping_arrival_groups_are_rejected(self):
        profile = {'halls': [{'region': 'A', 'slug': 'a', 'level_offset': 9,
            'surface_teleporter_id': 'surface', 'interior_teleporter_id': 'interior'}]}
        areas = [{'map_marker_id': marker, 'min': dict.fromkeys('xyz', 0),
                  'max': dict.fromkeys('xyz', 10)} for marker in (1, 2)]
        teleports = [{'native_id': 'surface', 'position': dict.fromkeys('xyz', 100), 'levels': None,
            'teleporter': {'TeleporterId': 'surface', 'TeleportTargetId': 'interior'}},
            {'native_id': 'interior', 'position': dict.fromkeys('xyz', 5),
             'levels': {'level': 0, 'levelOffset': 9},
             'teleporter': {'TeleporterId': 'interior', 'TeleportTargetId': 'surface'}}]
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            bind_arrivals(areas, teleports, profile)

    def test_floating_control_selects_its_supporting_floor_without_selecting_roof(self):
        # The arrival lies in the lower interval's air, beyond direct floor-match range.
        values = array('H', [10, 20, 4, 30, 0xff80, 0xff80,
                             10, 20, 40, 50, 0xff80, 0xff80])
        labels = array('I', [3, 7])
        hall = {'name': 'A', 'teleport_to': {'position': {'x': 5.25, 'z': 10.25, 'y': 10}},
                'spawner_controls': []}
        supported, evidence = select_floors(values, labels,
            {'halls': [hall], 'scene_feature_controls': []}, {'chests': []})
        self.assertEqual(dict(supported), {3: {0}})
        self.assertGreater(evidence['seed_matches'][0]['match']['distance_m'], 3)
        self.assertEqual(evidence['seed_matches'][0]['air_match']['distance_m'], 0)

    def test_authored_union_clips_altitude_and_preserves_gap(self):
        boxes = [{'min': {'x': 5, 'z': 10, 'y': 2}, 'max': {'x': 6, 'z': 11, 'y': 4}},
                 {'min': {'x': 7, 'z': 10, 'y': 2}, 'max': {'x': 8, 'z': 11, 'y': 4}}]
        self.assertTrue(box_contains_floor([10, 20, 4, 20], boxes))
        self.assertFalse(box_contains_floor([12, 20, 4, 20], boxes))
        self.assertFalse(box_contains_floor([10, 20, 20, 30], boxes))


if __name__ == '__main__':
    unittest.main()
