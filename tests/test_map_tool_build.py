"""Portable public build and packaging contracts, without a compiler or game."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from enshrouded_data.map_tool import portable_build as build
from enshrouded_data.map_tool.core import digest, load_bundle, read, write_json


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source with spaces'
        self.module = self.source / 'enshrouded_data/map_tool'
        (self.module / 'native').mkdir(parents=True)
        (self.module / 'profiles').mkdir()
        (self.source / 'enshrouded_data/kfc.py').write_text('# fixture parser interface\n')
        (self.module / '__init__.py').write_text('__version__ = "0.1.0"\n')
        (self.module / 'cli.py').write_text('# fixture runtime\n')
        (self.module / 'release.py').write_text('# private workspace build\n')
        (self.module / 'secret.txt').write_text('must not ship\n')
        (self.module / 'profiles/builds.json').write_text('{}')
        for cpp in build.HELPERS.values():
            (self.module / 'native' / cpp).write_text('// ' + cpp + '\n')
        (self.source / 'LICENSE').write_text('MIT License\nCopyright (c) fixture\n')
        (self.source / 'README.md').write_text('# Contributor guide\n')
        (self.source / 'docs').mkdir()
        (self.source / 'docs/MANUAL_UPLOAD.md').write_text('Upload maps manually.\n')
        (self.source / 'third-party').mkdir()
        (self.source / 'third-party/Apache-2.0.txt').write_text('Apache License fixture complete text\n')
        self.vulkan = self.root / 'Vulkan headers'
        (self.vulkan / 'include/vulkan').mkdir(parents=True)
        (self.vulkan / 'include/vulkan/vulkan_core.h').write_text('// fixture Vulkan header\n')
        (self.vulkan / 'LICENSE.md').write_text('Apache License, Version 2.0\n')
        self.parser = self.root / 'pinned parser.exe'
        self.parser.write_bytes(b'MZ fixture parser')
        self.parser_source = self.root / 'parser source.zip'
        with zipfile.ZipFile(self.parser_source, 'w') as archive:
            archive.writestr('LICENSE', 'GNU GENERAL PUBLIC LICENSE Version 3\n')
            archive.writestr('Cargo.toml', '[package]\nname="fixture"\n')
            archive.writestr('Cargo.lock', '# pinned Rust dependency graph\n')
        self.lock = {'schema_version': 1, 'parser': {
            'sha256': digest(self.parser), 'size_bytes': self.parser.stat().st_size,
            'default_path': '.dependencies/parser.exe',
            'default_source_path': '.dependencies/parser-source.zip',
            'source_archive_sha256': digest(self.parser_source),
            'source_commit': 'a' * 40, 'source_repository': 'https://example.invalid/upstream'},
            'vulkan_headers': {'version': 'v1.fixture', 'default_path': '.dependencies/Vulkan-Headers',
                'apache_license': {'path': 'third-party/Apache-2.0.txt',
                    'sha256': digest(self.source / 'third-party/Apache-2.0.txt')},
                'files': {name: digest(self.vulkan / name) for name in
                    ('LICENSE.md', 'include/vulkan/vulkan_core.h')}}}
        write_json(self.source / build.LOCK_NAME, self.lock)
        self.vc = self.root / 'Visual Studio/VC/Tools/MSVC/14.51.00000'
        self.compiler = self.vc / 'bin/Hostx64/x64/cl.exe'
        self.compiler.parent.mkdir(parents=True)
        self.compiler.write_bytes(b'MZ fixture compiler')
        (self.vc / 'include').mkdir()
        (self.vc / 'lib/x64').mkdir(parents=True)
        self.sdk = self.root / 'Windows SDK'
        for version in ('10.0.1.0', '10.0.3.0'):
            for name in ('ucrt', 'shared', 'um'):
                (self.sdk / 'Include' / version / name).mkdir(parents=True)
            for name in ('ucrt', 'um'):
                (self.sdk / 'Lib' / version / name / 'x64').mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def fake_compile(self, arguments, **kwargs):
        target = Path(next(value[4:] for value in arguments if value.startswith('/Fe:')))
        target.write_bytes(b'MZ built ' + target.name.encode('ascii'))
        self.assertEqual(kwargs['cwd'], target.parent)
        self.assertIn(str(self.vulkan / 'include'), kwargs['env']['INCLUDE'].split(os.pathsep))
        self.assertIn(str(self.vc / 'lib/x64'), kwargs['env']['LIB'].split(os.pathsep))
        self.assertIn('/MT', arguments)
        return subprocess.CompletedProcess(arguments, 0, 'fixture compile\n', '')

    def helpers(self):
        output = self.root / 'compiled helpers'
        with patch.object(build.subprocess, 'run', side_effect=self.fake_compile):
            build.build_helpers(output, self.source, self.compiler, self.sdk,
                                vulkan_headers=self.vulkan, progress=lambda *_: None)
        return output

    def package(self, helpers, output=None):
        output = output or self.root / 'EnshroudedMaps-fixture'
        return build.build_package(output, helpers, self.source, self.parser,
            self.parser_source, progress=lambda *_: None)

    def test_sdk_discovers_newest_complete_version_and_honors_override(self):
        self.assertEqual(build.discover_sdk(self.sdk), (self.sdk.resolve(), '10.0.3.0'))
        self.assertEqual(build.discover_sdk(self.sdk, '10.0.1.0')[1], '10.0.1.0')
        with self.assertRaisesRegex(ValueError, 'complete x64 Windows SDK'):
            build.discover_sdk(self.sdk, '10.0.2.0')

    def test_compiler_uses_vswhere_and_installed_x64_toolchain(self):
        installation = self.root / 'Visual Studio'
        fake_vswhere = self.root / 'vswhere.exe'
        fake_vswhere.write_bytes(b'MZ fixture vswhere')

        def which(name, **kwargs):
            return str(fake_vswhere) if name == 'vswhere.exe' else None

        with patch.object(build.shutil, 'which', side_effect=which), patch.object(
                build.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, str(installation), '')) as run:
            self.assertEqual(build.discover_compiler(environ={'PATH': ''}), self.compiler.resolve())
            self.assertIn('Microsoft.VisualStudio.Component.VC.Tools.x86.x64', run.call_args.args[0])

    def test_compiler_explicit_path_and_developer_prompt(self):
        self.assertEqual(build.discover_compiler(self.compiler), self.compiler.resolve())
        with patch.object(build.shutil, 'which', return_value=str(self.compiler)):
            self.assertEqual(build.discover_compiler(environ={'PATH': 'fixture'}), self.compiler.resolve())

    def test_native_build_records_sources_dependencies_and_actual_binary_hashes(self):
        helpers = self.helpers()
        receipt = read(helpers / 'build.json')
        self.assertEqual(receipt['dependency_lock_sha256'], digest(self.source / build.LOCK_NAME))
        self.assertEqual(receipt['compiler']['sha256'], digest(self.compiler))
        self.assertEqual(receipt['windows_sdk']['version'], '10.0.3.0')
        self.assertEqual({r['binary'] for r in receipt['helpers']}, set(build.HELPERS))
        for record in receipt['helpers']:
            self.assertEqual(record['sha256'], digest(helpers / record['binary']))
            self.assertEqual(record['source_sha256'], digest(self.source / record['source']))

    def test_changed_vulkan_header_fails_before_output_or_compiler(self):
        (self.vulkan / 'include/vulkan/vulkan_core.h').write_text('changed')
        output = self.root / 'not created'
        with patch.object(build, 'discover_compiler') as compiler, self.assertRaisesRegex(ValueError, 'pinned Vulkan'):
            build.build_helpers(output, self.source, vulkan_headers=self.vulkan)
        self.assertFalse(output.exists())
        compiler.assert_not_called()

    def test_failed_compile_preserves_log_without_success_receipt(self):
        output = self.root / 'failed build'
        with patch.object(build.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'compiler error')), self.assertRaisesRegex(ValueError, 'Native build failed'):
            build.build_helpers(output, self.source, self.compiler, self.sdk, vulkan_headers=self.vulkan)
        self.assertIn('compiler error', (output / 'hollow-geometry.exe.log').read_text())
        self.assertFalse((output / 'build.json').exists())

    def test_package_has_closed_hash_manifest_and_corresponding_source_and_licenses(self):
        package = self.package(self.helpers())
        bundle = load_bundle(package)
        self.assertEqual(set(bundle['files']), {p.relative_to(package).as_posix()
            for p in package.rglob('*') if p.is_file() and p.name != 'bundle.json'})
        self.assertEqual(digest(package / 'third-party/kfc-parser-source.zip'), digest(self.parser_source))
        self.assertIn('GNU GENERAL PUBLIC LICENSE', (package / 'third-party/kfc-parser-LICENSE.txt').read_text())
        self.assertIn('MIT License', (package / 'LICENSE').read_text())
        self.assertTrue((package / 'docs/MANUAL_UPLOAD.md').is_file())
        self.assertTrue((package / 'third-party/Apache-2.0.txt').is_file())
        self.assertFalse((package / 'DISTRIBUTION-STATUS.txt').exists())
        self.assertFalse((package / 'enshrouded_data/map_tool/secret.txt').exists())
        self.assertFalse((package / 'enshrouded_data/map_tool/release.py').exists())

    def test_package_rejects_changed_helper_source_before_creating_output(self):
        helpers = self.helpers()
        (self.module / 'native/world_renderer.cpp').write_text('// new source')
        with self.assertRaisesRegex(ValueError, 'native source'):
            self.package(helpers)
        self.assertFalse((self.root / 'EnshroudedMaps-fixture').exists())

    def test_public_receipt_retains_hashes_and_versions_without_local_machine_paths(self):
        helpers = self.helpers()
        package = self.package(helpers)
        public_text = (package / 'third-party/native-build.json').read_text()
        public = json.loads(public_text)
        self.assertNotIn(str(self.root), public_text)
        self.assertNotIn(str(self.root).replace('\\', '\\\\'), public_text)
        self.assertNotIn('arguments', public_text)
        self.assertNotIn('path', public['compiler'])
        self.assertEqual(public['compiler']['name'], 'cl.exe')
        self.assertEqual(public['compiler']['sha256'], digest(self.compiler))
        self.assertEqual(public['compiler']['msvc_toolset'], '14.51.00000')
        self.assertEqual(public['windows_sdk'], {'version': '10.0.3.0'})
        self.assertEqual(public['helpers'][0]['sha256'], digest(helpers / 'hollow-geometry.exe'))
        self.assertIn(str(self.root).replace('\\', '\\\\'), (helpers / 'build.json').read_text())

    def test_package_rejects_changed_binary_source_archive_and_dependency_lock(self):
        helpers = self.helpers()
        binary = helpers / 'map-jpeg.exe'
        original = binary.read_bytes()
        binary.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'built native helper'):
            self.package(helpers)
        binary.write_bytes(original)
        original_source = self.parser_source.read_bytes()
        self.parser_source.write_bytes(b'wrong source archive')
        with self.assertRaisesRegex(ValueError, 'corresponding source archive'):
            self.package(helpers)
        self.parser_source.write_bytes(original_source)
        self.lock['new_metadata'] = True
        write_json(self.source / build.LOCK_NAME, self.lock)
        with self.assertRaisesRegex(ValueError, 'current public dependency lock'):
            self.package(helpers)

    def test_package_rejects_duplicate_or_unexpected_helper_receipts(self):
        helpers = self.helpers()
        receipt = read(helpers / 'build.json')
        receipt['helpers'].append(receipt['helpers'][0])
        write_json(helpers / 'build.json', receipt)
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            self.package(helpers)

    def test_preserve_existing_outputs_and_reject_escaping_dependency_paths(self):
        with self.assertRaisesRegex(ValueError, 'contain the source tree'):
            build._fresh_output(self.root, self.source)
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            build._fresh_output(self.vulkan, self.source)
        for name in ('../escape', str(self.root)):
            with self.assertRaises(ValueError):
                build._within(self.source, name)

    def test_zip_is_reproducible_and_cannot_include_itself(self):
        package = self.package(self.helpers())
        first = build.zip_package(package, self.root / 'first.zip')
        second = build.zip_package(package, self.root / 'second.zip')
        self.assertEqual(first['sha256'], second['sha256'])
        with zipfile.ZipFile(first['archive']) as archive:
            self.assertTrue(all(name.startswith(package.name + '/') for name in archive.namelist()))
        with self.assertRaisesRegex(ValueError, 'outside the package'):
            build.zip_package(package, package / 'recursive.zip')
        with self.assertRaisesRegex(ValueError, 'fresh ZIP'):
            build.zip_package(package, self.root / 'first.zip')


if __name__ == '__main__':
    unittest.main()
