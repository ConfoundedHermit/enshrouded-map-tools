"""CLI safety, completed-run closure and resume binding without real game data."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from enshrouded_data.map_tool import cli
from enshrouded_data.map_tool.core import MapToolError, digest, read, write_json


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game = self.root / 'game'
        self.game.mkdir()
        (self.game / 'enshrouded.exe').write_bytes(b'fixture executable')
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        (self.bundle / 'bundle.json').write_text('{}')
        self.output = self.root / 'output'
        self.profile = {'build_id': '123'}

    def tearDown(self):
        self.temporary.cleanup()

    def args(self, **changes):
        values = dict(game=self.game, output=self.output, maps='halls', world_layout='both',
                      world_pixels=2560, bundle=self.bundle, resume=True)
        values.update(changes)
        return SimpleNamespace(**values)

    def complete_run(self):
        self.output.mkdir()
        (self.output / 'halls').mkdir()
        (self.output / 'halls/map.png').write_bytes(b'verified image fixture')
        (self.output / 'index.html').write_text('gallery')
        (self.output / 'manual-upload.json').write_text('{}')
        files = cli._file_manifest(self.output)
        for name in ('index.html', 'manual-upload.json'):
            path = self.output / name
            files[name] = {'sha256': digest(path), 'size_bytes': path.stat().st_size}
        request = {'game': str(self.game.resolve()), 'maps': 'halls', 'world_layout': 'both',
                   'world_pixels': 2560, 'build_id': '123',
                   'bundle_sha256': digest(self.bundle / 'bundle.json')}
        run = {'status': 'complete', 'build_id': '123', 'request': request, 'output_files': files}
        write_json(self.output / 'run.json', run)
        return run

    def test_verify_checks_complete_generated_file_set_but_excludes_cache(self):
        run = self.complete_run()
        (self.output / 'cache').mkdir()
        (self.output / 'cache/working.bin').write_bytes(b'working cache is not delivery')
        self.assertEqual(cli.verify_output(self.output)['verified_output_files'], 3)
        (self.output / 'halls/unrecorded.png').write_bytes(b'unrecorded delivery')
        with self.assertRaises(MapToolError):
            cli.verify_output(self.output)
        (self.output / 'halls/unrecorded.png').unlink()
        del run['output_files']['halls/map.png']
        write_json(self.output / 'run.json', run)
        with self.assertRaises(MapToolError):
            cli.verify_output(self.output)

    def test_verify_rejects_changed_missing_or_escaping_file(self):
        run = self.complete_run()
        image = self.output / 'halls/map.png'
        image.write_bytes(b'changed image')
        with self.assertRaises(MapToolError):
            cli.verify_output(self.output)
        image.unlink()
        with self.assertRaises(MapToolError):
            cli.verify_output(self.output)
        secret = self.root / 'outside.txt'
        secret.write_text('outside')
        run['output_files'] = {'../outside.txt': {'sha256': digest(secret), 'size_bytes': secret.stat().st_size}}
        write_json(self.output / 'run.json', run)
        with self.assertRaises(MapToolError):
            cli.verify_output(self.output)

    def test_completed_resume_reuses_only_original_options_and_package(self):
        self.complete_run()
        with patch.object(cli, 'identify_game', return_value=self.profile), \
                patch.object(cli, 'load_bundle', return_value={}), \
                patch.object(cli, 'verify_game') as verify_game, \
                patch.object(cli, 'context') as context, redirect_stdout(io.StringIO()):
            result = cli.generate(self.args())
            self.assertEqual(result['status'], 'passed')
            verify_game.assert_not_called()
            context.assert_not_called()
            for changes in ({'maps': 'all'}, {'world_layout': 'single'}, {'world_pixels': 1280}, {'resume': False}):
                with self.subTest(changes=changes), self.assertRaises(MapToolError):
                    cli.generate(self.args(**changes))
            (self.bundle / 'bundle.json').write_text('{"changed":true}')
            with self.assertRaises(MapToolError):
                cli.generate(self.args())

    def test_unsafe_output_rejected_before_identity_or_extraction(self):
        with patch.object(cli, 'identify_game') as identify, patch.object(cli, 'context') as context:
            for output in (self.game, self.game / 'generated', self.root):
                with self.subTest(output=output), self.assertRaises(MapToolError):
                    cli.generate(self.args(output=output, resume=False))
            identify.assert_not_called()
            context.assert_not_called()

    def test_unsupported_executable_stops_without_creating_output(self):
        before = (self.game / 'enshrouded.exe').read_bytes()
        errors = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
            code = cli.main(['generate', '--game', str(self.game), '--output', str(self.output), '--maps', 'halls'])
        self.assertEqual(code, 2)
        self.assertIn('unsupported or modified', errors.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual((self.game / 'enshrouded.exe').read_bytes(), before)

    def test_modified_archive_stops_before_extraction_and_retains_diagnostic(self):
        with patch.object(cli, 'identify_game', return_value=self.profile), \
                patch.object(cli, 'load_bundle', return_value={}), \
                patch.object(cli, 'verify_game', side_effect=MapToolError('Modified game archive')), \
                patch.object(cli, 'context') as context:
            with self.assertRaisesRegex(MapToolError, 'Modified'):
                cli.generate(self.args(resume=False))
            context.assert_not_called()
        run = read(self.output / 'run.json')
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['game_writes'], 0)
        self.assertIn('Modified game archive', (self.output / 'failure.txt').read_text())


if __name__ == '__main__':
    unittest.main()
