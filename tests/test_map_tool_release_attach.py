"""Release attachment checks using local assets and simulated GitHub responses."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'.github/scripts/attach_release.py'
SPEC = importlib.util.spec_from_file_location('release_attach', SCRIPT)
attach = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(attach)
COMMIT = 'a'*40
REPO = 'owner/maps'


class FakeGitHub:
    """Simulate paginated assets, annotated tags and release changes during uploads."""

    repository = REPO

    def __init__(self):
        self.release = {'id': 17, 'tag_name': 'v0.1.0', 'draft': False,
            'published_at': '2026-11-09T00:00:00Z', 'immutable': False,
            'upload_url': 'https://uploads.github.com/repos/'+REPO+'/releases/17/assets{?name,label}'}
        self.assets, self.uploads, self.calls = {}, [], []
        self.commit, self.annotated, self.move_after_upload = COMMIT, False, False
        self.retag_after_upload = False

    def api(self, suffix):
        self.calls.append(suffix)
        if suffix == '/releases/17':
            if self.retag_after_upload and self.uploads:
                return {**self.release, 'tag_name': 'v9.0.0'}
            return self.release
        if suffix.startswith('/git/ref/tags/'):
            sha = 'b'*40 if self.move_after_upload and self.uploads else self.commit
            return {'ref': 'refs/tags/v0.1.0', 'object': {
                'type': 'tag' if self.annotated else 'commit', 'sha': sha}}
        if suffix.startswith('/git/tags/'):
            return {'object': {'type': 'commit', 'sha': self.commit}}
        if suffix.startswith('/releases/17/assets?'):
            page = int(suffix.rsplit('=', 1)[1])
            return list(self.assets.values())[(page-1)*100:page*100]
        raise AssertionError(suffix)

    def upload(self, url, path):
        self.uploads.append(path.name)
        row = {'name': path.name, 'state': 'uploaded', 'size': path.stat().st_size,
            'digest': 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
        self.assets[path.name] = row
        return row


class ReleaseAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeGitHub()
        name = 'EnshroudedMaps-0.1.0-Windows-x64.zip'
        (self.root/name).write_bytes(b'tested package bytes')
        checksum = hashlib.sha256((self.root/name).read_bytes()).hexdigest()
        (self.root/'SHA256SUMS.txt').write_bytes((checksum+'  '+name+'\n').encode())
        self.rows = [{'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
            'size_bytes': p.stat().st_size} for p in (self.root/name, self.root/'SHA256SUMS.txt')]
        self.manifest = {'schema_version': 1, 'version': '0.1.0', 'tag': 'v0.1.0',
            'source_commit': COMMIT, 'assets': self.rows}
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self):
        (self.root/'release-assets.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def present(self, row):
        return {'name': row['name'], 'state': 'uploaded', 'size': row['size_bytes'],
            'digest': 'sha256:'+row['sha256']}

    def run_attachment(self):
        with redirect_stdout(io.StringIO()):
            return attach.attach(self.root, 17, 'v0.1.0', COMMIT, self.client)

    def test_uploads_exactly_two_assets_and_verifies_them(self):
        result = self.run_attachment()
        self.assertEqual(result['uploaded'], [row['name'] for row in self.rows])
        self.assertNotIn('release-assets.json', self.client.uploads)
        self.assertEqual(result['status'], 'passed')

    def test_identical_existing_assets_are_idempotent(self):
        self.client.assets = {row['name']: self.present(row) for row in self.rows}
        self.assertEqual(self.run_attachment()['uploaded'], [])
        self.assertEqual(self.client.uploads, [])

    def test_second_asset_conflict_prevents_first_upload(self):
        conflict = self.present(self.rows[1])
        conflict['digest'] = 'sha256:'+'0'*64
        self.client.assets[conflict['name']] = conflict
        with self.assertRaisesRegex(ValueError, 'existing asset'):
            self.run_attachment()
        self.assertEqual(self.client.uploads, [])

    def test_unverifiable_existing_asset_is_not_overwritten(self):
        row = self.present(self.rows[0])
        del row['digest']
        self.client.assets[row['name']] = row
        with self.assertRaises(ValueError):
            self.run_attachment()
        self.assertFalse(self.client.uploads)

    def test_release_and_current_tag_must_match_tested_source(self):
        self.client.commit = 'b'*40
        with self.assertRaisesRegex(ValueError, 'tested commit'):
            self.run_attachment()
        self.assertFalse(self.client.uploads)
        self.client.commit = COMMIT
        self.client.release['tag_name'] = 'v9.0.0'
        with self.assertRaisesRegex(ValueError, 'another tag'):
            self.run_attachment()
        self.assertFalse(self.client.uploads)

    def test_annotated_tags_are_resolved(self):
        self.client.annotated = True
        self.assertEqual(self.run_attachment()['status'], 'passed')

    def test_tag_movement_after_upload_is_reported(self):
        self.client.move_after_upload = True
        with self.assertRaisesRegex(ValueError, 'moved'):
            self.run_attachment()

    def test_release_retagging_during_attachment_is_reported(self):
        self.client.retag_after_upload = True
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.run_attachment()

    def test_manifest_and_corrupted_bytes_rejected_before_network(self):
        self.manifest['source_commit'] = 'b'*40
        self.save_manifest()
        with self.assertRaises(ValueError):
            self.run_attachment()
        self.assertFalse(self.client.calls)
        self.manifest['source_commit'] = COMMIT
        self.save_manifest()
        (self.root/self.rows[0]['name']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.run_attachment()
        self.assertFalse(self.client.calls)

    def test_extra_files_and_checksum_misdirection_rejected(self):
        extra = self.root/'unrelated.txt'
        extra.write_text('extra')
        with self.assertRaises(ValueError):
            self.run_attachment()
        extra.unlink()
        checksum = self.root/'SHA256SUMS.txt'
        checksum.write_text('0'*64+'  different.zip\n')
        self.rows[1].update(sha256=hashlib.sha256(checksum.read_bytes()).hexdigest(),
                            size_bytes=checksum.stat().st_size)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'checksum file'):
            self.run_attachment()
        self.assertFalse(self.client.calls)

    def test_immutable_release_allows_only_already_identical_assets(self):
        self.client.release['immutable'] = True
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.run_attachment()
        self.assertFalse(self.client.uploads)
        self.client.assets = {row['name']: self.present(row) for row in self.rows}
        self.assertEqual(self.run_attachment()['uploaded'], [])

    def test_draft_and_foreign_upload_hosts_are_rejected(self):
        self.client.release['draft'] = True
        with self.assertRaises(ValueError):
            self.run_attachment()
        self.client.release['draft'] = False
        self.client.release['upload_url'] = 'https://example.invalid/upload'
        with self.assertRaisesRegex(ValueError, 'endpoint'):
            self.run_attachment()
        self.assertFalse(self.client.uploads)

    def test_asset_pagination_is_complete_before_collision_checks(self):
        for number in range(100):
            name = f'other-{number}'
            self.client.assets[name] = {'name': name}
        self.client.assets.update({row['name']: self.present(row) for row in self.rows})
        self.assertEqual(self.run_attachment()['uploaded'], [])
        self.assertTrue(any('page=2' in call for call in self.client.calls))


if __name__ == '__main__':
    unittest.main()
