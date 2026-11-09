"""Release downloads, archive boundaries and source/asset identity guards."""
import hashlib
import io
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from enshrouded_data.map_tool import automation as release
from enshrouded_data.map_tool.core import digest, read


class Response(io.BytesIO):
    def __init__(self, data, url='https://example.invalid/pinned'):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_verified_download_commits_only_exact_bytes_and_reuses_good_cache(self):
        payload = b'pin this exact dependency'
        target = self.root / 'dependency.exe'
        checksum = hashlib.sha256(payload).hexdigest()
        calls = []

        def open_fixture(request, timeout):
            calls.append((request.full_url, timeout))
            return Response(payload)

        release.fetch_verified('https://example.invalid/pinned', target, checksum, len(payload), open_fixture)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(calls, [('https://example.invalid/pinned', 60)])
        release.fetch_verified('https://example.invalid/pinned', target, checksum, len(payload), open_fixture)
        self.assertEqual(len(calls), 1)
        target.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'Existing dependency differs'):
            release.fetch_verified('https://example.invalid/pinned', target, checksum, opener=open_fixture)
        self.assertEqual(target.read_bytes(), b'tampered')
        self.assertEqual(len(calls), 1)

    def test_wrong_hash_short_download_and_oversize_never_commit_or_leave_partial(self):
        for label, payload, checksum, size in (
                ('wrong hash', b'wrong', 'a' * 64, None),
                ('short', b'a', hashlib.sha256(b'a').hexdigest(), 2),
                ('long', b'abc', hashlib.sha256(b'abc').hexdigest(), 2)):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, 'download|Downloaded'):
                release.fetch_verified('https://example.invalid/pinned', self.root / 'download', checksum,
                    size, lambda *args, **kwargs: Response(payload))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_download_requires_https_including_redirect_destination(self):
        for url in ('http://example.invalid/file', 'https://user:secret@example.invalid/file',
                    'https://example.invalid/file#fragment', 'file:///tmp/input'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'HTTPS'):
                release.fetch_verified(url, self.root / 'output', 'a' * 64)
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            release.fetch_verified('https://example.invalid/pinned', self.root / 'output', 'a' * 64,
                opener=lambda *a, **k: Response(b'bad', 'http://example.invalid/redirect'))
        self.assertEqual(list(self.root.iterdir()), [])

    def archive(self, entries, name='fixture.zip'):
        target = self.root / name
        with zipfile.ZipFile(target, 'w') as archive:
            for entry, data in entries:
                if isinstance(entry, str):
                    info = zipfile.ZipInfo(entry)
                    # Keep deliberately unsafe separators; ZipInfo's Windows
                    # constructor normally converts them to forward slashes.
                    info.filename = entry
                else:
                    info = entry
                archive.writestr(info, data)
        return target

    def test_safe_extraction_preserves_nested_files_and_checks_root(self):
        archive = self.archive([('package/sub/file.txt', b'expected')])
        destination = release.extract_verified_zip(archive, self.root / 'extracted', expected_root='package')
        self.assertEqual((destination / 'package/sub/file.txt').read_bytes(), b'expected')
        with self.assertRaisesRegex(ValueError, 'fresh'):
            release.extract_verified_zip(archive, destination)
        with self.assertRaisesRegex(ValueError, 'top-level'):
            release.extract_verified_zip(archive, self.root / 'wrong', expected_root='other')
        self.assertFalse((self.root / 'wrong').exists())

    def test_archive_traversal_windows_aliases_duplicates_and_links_fail_before_writes(self):
        link = zipfile.ZipInfo('root/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[('../escape', b'x')], [('/absolute', b'x')], [('C:/drive', b'x')],
            [('root\\escape', b'x')], [('root//empty', b'x')], [('root/./dot', b'x')],
            [('root/NUL.txt', b'x')], [('root/trailing. ', b'x')],
            [('root/file', b'a'), ('root/FILE', b'b')],
            [('root/file', b'a'), ('root/file/child', b'b')], [(link, b'../outside')]]
        for index, entries in enumerate(cases):
            with self.subTest(index=index):
                archive = self.archive(entries, str(index) + '.zip')
                destination = self.root / ('extract-' + str(index))
                with self.assertRaises(ValueError):
                    release.extract_verified_zip(archive, destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_archive_unpacked_budget_is_checked_before_writes(self):
        archive = self.archive([('first', b'1234'), ('second', b'5678')])
        with zipfile.ZipFile(archive) as opened, self.assertRaisesRegex(ValueError, 'limits'):
            release._zip_members(opened, max_bytes=7)

    def test_release_tag_must_match_committed_version_and_has_safe_filename_syntax(self):
        self.assertEqual(release.release_tag(None, '1.2.3'), 'v1.2.3')
        for tag in ('v1.2.4', '1.2.3', 'v01.2.3', 'v1.2.3-rc.1', 'v1.2.3/escape', 'v1.2.3$(whoami)', 'v1.2.3\n'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'checked-in version'):
                release.release_tag(tag, '1.2.3')

    def test_dirty_or_unidentified_sources_cannot_claim_a_release_commit(self):
        with patch.object(release.subprocess, 'check_output', return_value='?? untracked.py\n') as command:
            with self.assertRaisesRegex(ValueError, 'clean source'):
                release.source_commit(self.root)
            self.assertIn('--untracked-files=all', command.call_args.args[0])
            self.assertEqual(command.call_count, 1)
        with patch.object(release.subprocess, 'check_output', side_effect=['', 'not a commit']):
            with self.assertRaisesRegex(ValueError, 'source commit'):
                release.source_commit(self.root)
        with patch.object(release.subprocess, 'check_output', side_effect=['', 'a' * 40 + '\n']):
            self.assertEqual(release.source_commit(self.root), 'a' * 40)

    def test_source_bootstrap_requires_both_outer_release_and_nested_source_hashes(self):
        source = b'exact original corresponding source ZIP'
        bootstrap = self.archive([(release.BOOTSTRAP_MEMBER, source)])
        pin = {'source_archive_sha256': hashlib.sha256(source).hexdigest(),
            'source_repository': 'https://example.invalid/parser.git', 'source_commit': 'b' * 40}
        calls = []

        def fetch(url, destination, checksum):
            calls.append((url, checksum))
            return bootstrap

        with (patch.object(release.subprocess, 'run', side_effect=FileNotFoundError('git')),
                patch.object(release, 'fetch_verified', side_effect=fetch)):
            result = release._parser_source(pin, self.root / 'source.zip', self.root, lambda *_: None)
        self.assertEqual(result.read_bytes(), source)
        self.assertEqual(calls, [(release.BOOTSTRAP_URL, release.BOOTSTRAP_SHA256)])
        pin['source_archive_sha256'] = 'c' * 64
        with (patch.object(release.subprocess, 'run', side_effect=FileNotFoundError('git')),
                patch.object(release, 'fetch_verified', return_value=bootstrap)):
            with self.assertRaisesRegex(ValueError, 'original source ZIP pin'):
                release._parser_source(pin, self.root / 'bad-source.zip', self.root, lambda *_: None)
        self.assertFalse((self.root / 'bad-source.zip').exists())

    def test_exact_git_source_archive_does_not_need_release_bootstrap(self):
        source = b'pinned git ZIP'
        pin = {'source_archive_sha256': hashlib.sha256(source).hexdigest(),
            'source_repository': 'https://example.invalid/parser.git', 'source_commit': 'b' * 40}

        def run(arguments, **kwargs):
            if 'archive' in arguments:
                Path(next(arg[len('--output='):] for arg in arguments if arg.startswith('--output='))).write_bytes(source)
            return subprocess.CompletedProcess(arguments, 0)

        with patch.object(release.subprocess, 'run', side_effect=run), patch.object(
                release.subprocess, 'check_output', return_value='b' * 40 + '\n'), patch.object(release, 'fetch_verified') as fetch:
            result = release._parser_source(pin, self.root / 'source.zip', self.root, lambda *_: None)
        self.assertEqual(result.read_bytes(), source)
        fetch.assert_not_called()

    def test_assets_bind_the_exact_archive_and_checksum_to_tag_and_commit(self):
        archive = self.root / 'EnshroudedMaps-1.2.3-Windows-x64.zip'
        archive.write_bytes(b'build archive')
        manifest = release.write_assets(self.root, archive, 'v1.2.3', 'd' * 40, version='1.2.3')
        self.assertEqual(manifest, read(self.root / 'release-assets.json'))
        self.assertEqual(manifest['source_commit'], 'd' * 40)
        self.assertEqual([asset['name'] for asset in manifest['assets']], [archive.name, 'SHA256SUMS.txt'])
        self.assertEqual((self.root / 'SHA256SUMS.txt').read_bytes(),
            (digest(archive) + '  ' + archive.name + '\n').encode('ascii'))
        for asset in manifest['assets']:
            path = self.root / asset['name']
            self.assertEqual(asset['sha256'], digest(path))
            self.assertEqual(asset['size_bytes'], path.stat().st_size)
        with self.assertRaises(FileExistsError):
            release.write_assets(self.root, archive, 'v1.2.3', 'd' * 40, version='1.2.3')

    def test_wrong_version_archive_name_or_commit_cannot_create_checksum(self):
        archive = self.root / 'unexpected.zip'
        archive.write_bytes(b'archive')
        with self.assertRaisesRegex(ValueError, 'archive path or name'):
            release.write_assets(self.root, archive, 'v1.2.3', 'd' * 40, version='1.2.3')
        self.assertFalse((self.root / 'SHA256SUMS.txt').exists())

    def test_invalid_tag_fails_before_source_checks_or_network_access(self):
        with patch.object(release, 'source_commit') as commit, patch.object(
                release, 'prepare_dependencies') as dependencies:
            with self.assertRaisesRegex(ValueError, 'checked-in version'):
                release.build_release(self.root / 'dist', self.root / 'dependencies', tag='v999.999.999')
        commit.assert_not_called()
        dependencies.assert_not_called()
        self.assertFalse((self.root / 'dist').exists())

    def test_test_failure_prevents_packaging_or_checksum_publication(self):
        output = self.root / 'dist'
        dependencies = {'vulkan_headers': self.root / 'headers'}
        with (patch.object(release, 'source_commit', return_value='d' * 40),
                patch.object(release, 'prepare_dependencies', return_value=dependencies),
                patch.object(release.build, 'build_helpers', return_value=output / 'helpers'),
                patch.object(release.build, 'build_package') as package,
                patch.object(release.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['unittest']))):
            with self.assertRaises(subprocess.CalledProcessError):
                release.build_release(output, self.root / 'dependencies')
        package.assert_not_called()
        self.assertFalse((output / 'SHA256SUMS.txt').exists())
        self.assertFalse((output / 'release-assets.json').exists())


if __name__ == '__main__':
    unittest.main()
