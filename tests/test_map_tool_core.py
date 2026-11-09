"""Portable map input safety and fresh-extraction contract checks."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from enshrouded_data.map_tool import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game = self.root / 'game'
        self.game.mkdir()
        (self.game / 'enshrouded.exe').write_bytes(b'supported executable')
        self.exe_hash = hashlib.sha256(b'supported executable').hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def test_output_must_be_independent_of_game_and_its_ancestors(self):
        for output in (self.game, self.game / 'generated', self.root):
            with self.assertRaises(core.MapToolError):
                core.separate_output(self.game, output)
        self.assertEqual(core.separate_output(self.game, self.root / 'output')[1], (self.root / 'output').resolve())

    def test_package_paths_cannot_escape_using_absolute_or_parent_path(self):
        for path in ('../escape', self.root.parent / 'escape'):
            with self.assertRaises(core.MapToolError):
                core.within(self.root, path)
        self.assertEqual(core.within(self.root, 'helpers/parser.exe'), (self.root / 'helpers/parser.exe').resolve())

    def test_supported_executable_identification_rejects_modification_and_ambiguity(self):
        profile = {'executable_sha256': self.exe_hash, 'build_id': '123'}
        self.assertEqual(core.identify_game(self.game, [profile]), profile)
        with self.assertRaises(core.MapToolError):
            core.identify_game(self.game, [profile, profile])
        (self.game / 'enshrouded.exe').write_bytes(b'modified executable')
        with self.assertRaises(core.MapToolError):
            core.identify_game(self.game, [profile])

    def test_all_container_hashes_checked_and_ongoing_input_change_detected(self):
        container = self.game / 'enshrouded_000.dat'
        container.write_bytes(b'original archive')
        profile = {'build_id': '123', 'kfc_version': 'test', 'files': [
            {'name': 'enshrouded.exe', 'size_bytes': (self.game / 'enshrouded.exe').stat().st_size,
             'sha256': self.exe_hash},
            {'name': container.name, 'size_bytes': container.stat().st_size, 'sha256': core.digest(container)}]}
        with patch.object(core, 'KFCIndex', return_value=SimpleNamespace(version='test')):
            verified = core.verify_game(self.game, profile, progress=lambda *_: None)
        self.assertEqual(len(verified['files']), 2)
        self.assertEqual(verified['game_writes'], 0)
        original_stat = container.stat()
        container.write_bytes(b'modified archive')
        # A same-length rewrite can land in the same filesystem clock tick.
        # Exercise metadata stability without depending on runner timing.
        os.utime(container, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 2_000_000_000))
        with self.assertRaises(core.MapToolError):
            core.assert_inputs_stable(self.game, verified)
        with self.assertRaises(core.MapToolError):
            core.verify_game(self.game, profile, progress=lambda *_: None)

    def test_modification_during_hashing_is_rejected(self):
        path = self.game / 'enshrouded.exe'
        profile = {'files': [{'name': path.name, 'size_bytes': path.stat().st_size, 'sha256': self.exe_hash}]}

        def changed_digest(p):
            path.write_bytes(b'changed during hashing')
            return self.exe_hash

        with patch.object(core, 'digest', side_effect=changed_digest):
            with self.assertRaises(core.MapToolError):
                core.verify_game(self.game, profile, progress=lambda *_: None)

    def test_bundle_hashes_bind_helpers_and_detect_changed_code(self):
        helper = self.root / 'helper.exe'
        helper.write_bytes(b'pinned helper')
        core.write_json(self.root / 'bundle.json', {'files': {'helper.exe': core.digest(helper)}})
        self.assertIn('helper.exe', core.load_bundle(self.root)['files'])
        helper.write_bytes(b'changed helper')
        with self.assertRaises(core.MapToolError):
            core.load_bundle(self.root)

    def test_resource_identity_table_rejects_duplicate_and_out_of_bounds(self):
        resource = uuid.UUID('509feadb-4c60-425f-9c7c-deeefd9b6920').bytes_le + struct.pack('<IIII', 12, 0, 0, 0)
        index = SimpleNamespace(data=resource, location=lambda _: (0, 1))
        self.assertEqual(core.indexed_resources(index)[0], ('509feadb-4c60-425f-9c7c-deeefd9b6920', 12, 0))
        index.location = lambda _: (0, 2)
        with self.assertRaises(core.MapToolError):
            core.indexed_resources(index)
        index.data = resource * 2
        with self.assertRaises(core.MapToolError):
            core.indexed_resources(index)

    def extraction_fixture(self):
        names = ['keen::ecs::TemplateResource', 'keen::SceneResource', 'keen::SceneEntityChunkResource',
                 'keen::VoxelWorldResource', 'keen::UiMapResource', 'keen::UiTextureResource',
                 'keen::GpcResource', 'keen::FbUiBundle', 'keen::BalancingTable']
        types = {name: number for number, name in enumerate(names, 1)}
        parser = self.root / 'parser.exe'
        parser.write_bytes(b'pinned extractor')
        profile = {'build_id': '123', 'kfc_version': 'test', 'parser_sha256': core.digest(parser),
                   'main_scene_guid': 'scene', 'map_guid': 'map', 'shader_guid': 'shader', 'ui_guid': 'ui',
                   'resource_types': types}
        record = ('map', types['keen::UiTextureResource'], 0)
        index = SimpleNamespace()
        return parser, profile, record, index

    def test_extraction_selection_binds_main_scene_and_known_world_ids(self):
        _, profile, record, index = self.extraction_fixture()
        types = profile['resource_types']
        records = [record, ('unexpected-map', record[1], 0),
                   ('scene', types['keen::SceneEntityChunkResource'], 0),
                   ('other-scene', types['keen::SceneEntityChunkResource'], 0),
                   ('template', types['keen::ecs::TemplateResource'], 0)]
        with patch.object(core, 'indexed_resources', return_value=records):
            self.assertEqual(core.extraction_selection(index, profile, 'world'), {record})
            self.assertEqual(core.extraction_selection(index, profile, 'halls'), {records[2], records[4]})
            self.assertEqual(core.extraction_selection(index, profile, 'all'), {record, records[2], records[4]})

    def test_parser_success_without_required_descriptors_is_not_accepted(self):
        parser, profile, record, index = self.extraction_fixture()
        output = self.root / 'missing'

        def parser_run(arguments, log, progress):
            if 'extract-types' in arguments:
                core.write_json(output / 'cache/parser/reflection_data.json', {
                    'version': 'test', 'types': [{'qualifiedName': n, 'qualifiedHash': k}
                                                for n, k in profile['resource_types'].items()]})
            else:
                (output / 'cache/descriptors').mkdir()
            return 'Unpacked a total of 0/1 descriptors (0 raw, 0 failed)'

        with patch.object(core, 'KFCIndex', return_value=index), \
                patch.object(core, 'indexed_resources', return_value=[record]), \
                patch.object(core, '_run_parser', side_effect=parser_run):
            with self.assertRaisesRegex(core.MapToolError, 'cover the requested'):
                core.extract_descriptors(self.game, output, profile, 'world', parser, progress=lambda *_: None)
        self.assertFalse((output / 'cache/extraction.json').exists())

    def test_resume_validates_every_descriptor_and_reflection(self):
        parser, profile, record, index = self.extraction_fixture()
        output = self.root / 'run'
        descriptor = output / 'cache/descriptors/UiTextureResource/map.json'

        def parser_run(arguments, log, progress):
            if 'extract-types' in arguments:
                core.write_json(output / 'cache/parser/reflection_data.json', {
                    'version': 'test', 'types': [{'qualifiedName': n, 'qualifiedHash': k}
                                                for n, k in profile['resource_types'].items()]})
            else:
                core.write_json(descriptor, {'$guid': 'map', '$type': 'keen::UiTextureResource', '$part': 0})
            return 'Unpacked a total of 1/1 descriptors (0 raw, 0 failed)'

        with patch.object(core, 'KFCIndex', return_value=index), \
                patch.object(core, 'indexed_resources', return_value=[record]), \
                patch.object(core, '_run_parser', side_effect=parser_run) as called:
            core.extract_descriptors(self.game, output, profile, 'world', parser, progress=lambda *_: None)
            self.assertEqual(called.call_count, 2)
            raw, _ = core.extract_descriptors(self.game, output, profile, 'world', parser,
                                               resume=True, progress=lambda *_: None)
            self.assertEqual(raw, output / 'cache/descriptors')
            self.assertEqual(called.call_count, 2)
            descriptor.write_text('{}')
            with self.assertRaisesRegex(core.MapToolError, 'descriptor changed'):
                core.extract_descriptors(self.game, output, profile, 'world', parser,
                                         resume=True, progress=lambda *_: None)


if __name__ == '__main__':
    unittest.main()
