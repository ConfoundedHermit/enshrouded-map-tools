"""Build and package the Windows tool offline using verified local dependencies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from . import __version__
from .core import digest, read, write_json


HELPERS = {
    'hollow-geometry.exe': 'hollow_geometry.cpp',
    'world-renderer.exe': 'world_renderer.cpp',
    'map-jpeg.exe': 'map_jpeg.cpp',
}
SOURCE_ROOT = Path(__file__).resolve().parents[2]
LOCK_NAME = 'enshrouded_data/map_tool/build-dependencies.json'
# An explicit public inventory excludes unlisted workspace files from packages.
PACKAGE_SOURCE_FILES = (
    'LICENSE',
    'README.md',
    'THIRD_PARTY.md',
    'docs/AUTOMATION.md',
    'docs/BUILDING.md',
    'docs/HOW-IT-WORKS.md',
    'docs/VALIDATION.md',
    'docs/manual-upload.md',
    'docs/wiki-guide.wikitext',
    'enshrouded_data/__init__.py',
    'enshrouded_data/kfc.py',
    'enshrouded_data/map_tool/__init__.py',
    'enshrouded_data/map_tool/__main__.py',
    'enshrouded_data/map_tool/automation.py',
    LOCK_NAME,
    'enshrouded_data/map_tool/cli.py',
    'enshrouded_data/map_tool/core.py',
    'enshrouded_data/map_tool/delivery.py',
    'enshrouded_data/map_tool/hall_flatten.py',
    'enshrouded_data/map_tool/hall_render.py',
    'enshrouded_data/map_tool/halls.py',
    'enshrouded_data/map_tool/halls_geometry.py',
    'enshrouded_data/map_tool/halls_scene.py',
    'enshrouded_data/map_tool/native/hollow_geometry.cpp',
    'enshrouded_data/map_tool/native/map_jpeg.cpp',
    'enshrouded_data/map_tool/native/world_renderer.cpp',
    'enshrouded_data/map_tool/portable_build.py',
    'enshrouded_data/map_tool/profiles/builds.json',
    'enshrouded_data/map_tool/profiles/halls-26430625.json',
    'enshrouded_data/map_tool/profiles/world-26430625.json',
    'enshrouded_data/map_tool/world.py',
)


def _within(root, name):
    root = Path(root).resolve()
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Expected a relative dependency path: ' + str(name))
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('A dependency path escapes its directory: ' + str(name))
    return path


def _lock(source):
    path = Path(source) / LOCK_NAME
    lock = read(path)
    if lock.get('schema_version') != 1:
        raise ValueError('Unsupported build dependency lock schema.')
    return lock, digest(path)


def _checked_file(path, checksum, label):
    path = Path(path).resolve()
    if not path.is_file() or digest(path) != checksum:
        raise ValueError('Missing or changed ' + label + ': ' + str(path))
    return path


def _fresh_output(output, source):
    output, source = Path(output).resolve(), Path(source).resolve()
    if source.is_relative_to(output):
        raise ValueError('Build output must not contain the source tree.')
    if output.exists():
        raise ValueError('Preserve previous builds; choose a fresh output directory.')
    return output


def _version_key(path):
    try:
        return tuple(int(part) for part in Path(path).name.split('.'))
    except ValueError:
        return ()


def discover_compiler(explicit=None, environ=None):
    """Find MSVC through an override, the current PATH or Microsoft's vswhere."""
    environment = os.environ if environ is None else environ
    if explicit:
        compiler = Path(explicit).resolve()
    elif found := shutil.which('cl.exe', path=environment.get('PATH', '')):
        compiler = Path(found).resolve()
    else:
        default = Path(environment.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        vswhere = shutil.which('vswhere.exe', path=environment.get('PATH', ''))
        if not vswhere and default.is_file():
            vswhere = str(default)
        if not vswhere:
            raise ValueError('Install Visual Studio C++ Build Tools, use an x64 Developer Command Prompt, or pass --compiler.')
        result = subprocess.run([vswhere, '-latest', '-products', '*', '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
            capture_output=True, text=True, check=False)
        installation = result.stdout.strip()
        if result.returncode or not installation:
            raise ValueError('vswhere could not find Visual Studio C++ Build Tools.')
        versions = sorted((Path(installation) / 'VC/Tools/MSVC').glob('*'), key=_version_key, reverse=True)
        candidates = [version / 'bin/Hostx64/x64/cl.exe' for version in versions]
        compiler = next((p.resolve() for p in candidates if p.is_file()), None)
        if compiler is None:
            raise ValueError('Visual Studio does not contain the x64 C++ compiler.')
    if not compiler.is_file():
        raise ValueError('The C++ compiler does not exist: ' + str(compiler))
    return compiler


def discover_sdk(explicit_root=None, explicit_version=None, environ=None):
    """Select a complete x64 Windows SDK, honoring explicit root and version values."""
    environment = os.environ if environ is None else environ
    roots = []
    if explicit_root:
        roots.append(Path(explicit_root))
    else:
        if environment.get('WindowsSdkDir'):
            roots.append(Path(environment['WindowsSdkDir']))
        if os.name == 'nt':
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r'SOFTWARE\Microsoft\Windows Kits\Installed Roots',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
                    roots.append(Path(winreg.QueryValueEx(key, 'KitsRoot10')[0]))
            except OSError:
                pass
        roots.append(Path(environment.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Windows Kits/10')
    for root in roots:
        versions = [root / 'Include' / explicit_version] if explicit_version else sorted(
            (root / 'Include').glob('*'), key=_version_key, reverse=True)
        for include in versions:
            version = include.name
            required = [include / name for name in ('ucrt', 'shared', 'um')]
            required += [root / 'Lib' / version / name / 'x64' for name in ('ucrt', 'um')]
            if all(path.is_dir() for path in required):
                return root.resolve(), version
    raise ValueError('No complete x64 Windows SDK was found. Pass --sdk-root and optionally --sdk-version.')


def verify_vulkan_headers(path, lock):
    """Check the Vulkan header and license files against the dependency lock."""
    root = Path(path).resolve()
    for name, checksum in lock['vulkan_headers']['files'].items():
        _checked_file(_within(root, name), checksum, 'pinned Vulkan-Headers file')
    return root


def build_helpers(output, source=SOURCE_ROOT, compiler=None, sdk_root=None,
                  sdk_version=None, vulkan_headers=None, progress=print):
    """Compile the native helpers and record their source, toolchain and binary hashes."""
    source = Path(source).resolve()
    output = _fresh_output(output, source)
    lock, lock_sha256 = _lock(source)
    headers = verify_vulkan_headers(vulkan_headers or _within(source,
        lock['vulkan_headers']['default_path']), lock)
    compiler = discover_compiler(compiler)
    sdk, sdk_version = discover_sdk(sdk_root, sdk_version)
    # Standard MSVC layout: <version>/bin/Hostx64/x64/cl.exe.
    vc = compiler.parents[3]
    if not (vc / 'include').is_dir() or not (vc / 'lib/x64').is_dir():
        raise ValueError('Use an x64 MSVC compiler in its installed VC/Tools/MSVC directory.')
    # Configure the x64 include and library paths independently of the calling shell.
    environment = os.environ.copy()
    environment['PATH'] = str(compiler.parent) + os.pathsep + environment.get('PATH', '')
    includes = [headers / 'include', vc / 'include']
    includes += [sdk / 'Include' / sdk_version / name for name in ('ucrt', 'shared', 'um', 'winrt')
                 if (sdk / 'Include' / sdk_version / name).is_dir()]
    libraries = [vc / 'lib/x64'] + [sdk / 'Lib' / sdk_version / name / 'x64' for name in ('ucrt', 'um')]
    environment['INCLUDE'] = os.pathsep.join(map(str, includes))
    environment['LIB'] = os.pathsep.join(map(str, libraries))
    # Require all helper sources before creating build output.
    sources = {name: _within(source, 'enshrouded_data/map_tool/native/' + cpp)
               for name, cpp in HELPERS.items()}
    if any(not path.is_file() for path in sources.values()):
        raise ValueError('A native helper source is missing from the source checkout.')
    output.mkdir(parents=True)
    records = []
    for name, cpp in sources.items():
        binary = output / name
        arguments = [str(compiler), '/nologo', '/EHsc', '/std:c++17', '/O2', '/MT', '/Brepro',
            str(cpp), '/Fo:' + str(output / (binary.stem + '.obj')), '/Fe:' + str(binary)]
        result = subprocess.run(arguments, env=environment, cwd=output, capture_output=True, text=True)
        (output / (name + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode or not binary.is_file():
            raise ValueError('Native build failed; see ' + str(output / (name + '.log')))
        records.append({'source': cpp.relative_to(source).as_posix(), 'source_sha256': digest(cpp),
            'binary': name, 'sha256': digest(binary), 'size_bytes': binary.stat().st_size,
            'arguments': arguments})
        progress('Built ' + name)
    shutil.copyfile(headers / 'LICENSE.md', output / 'Vulkan-Headers-LICENSE.md')
    write_json(output / 'build.json', {'schema_version': 1, 'version': __version__,
        'dependency_lock_sha256': lock_sha256, 'platform': 'Windows-x64',
        'compiler': {'path': str(compiler), 'sha256': digest(compiler), 'msvc_toolset': vc.name},
        'windows_sdk': {'path': str(sdk), 'version': sdk_version},
        'vulkan_headers': {'version': lock['vulkan_headers']['version'],
            'files': lock['vulkan_headers']['files']}, 'helpers': records})
    return output


def _parser_license(source_archive, pin):
    _checked_file(source_archive, pin['source_archive_sha256'], 'pinned parser corresponding source archive')
    with zipfile.ZipFile(source_archive) as archive:
        if not {'LICENSE', 'Cargo.toml', 'Cargo.lock'}.issubset(archive.namelist()):
            raise ValueError('The pinned parser source archive lacks its license or Rust source metadata.')
        return archive.read('LICENSE')


def package_sources(source):
    """Validate the explicit source inventory without Git or scanning other files."""
    source = Path(source).resolve()
    files = {}
    for name in PACKAGE_SOURCE_FILES:
        # Check unresolved ancestors to reject links and junctions within the tree too.
        _within(source, name)
        path = source / name
        current = path
        while current != source:
            if current.is_symlink() or current.is_junction():
                raise ValueError('Public package sources must not use links: ' + name)
            current = current.parent
        if not path.is_file():
            raise ValueError('Missing public package source: ' + name)
        files[name] = path
    return files


def _copy_sources(files, output):
    for name, path in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _public_build_receipt(build):
    """Retain build provenance without publishing local machine paths."""
    return {'schema_version': 1, 'version': build['version'], 'platform': build['platform'],
        'dependency_lock_sha256': build['dependency_lock_sha256'],
        'compiler': {'name': Path(build['compiler']['path']).name,
            'sha256': build['compiler']['sha256'], 'msvc_toolset': build['compiler'].get('msvc_toolset')},
        'windows_sdk': {'version': build['windows_sdk']['version']},
        'vulkan_headers': build['vulkan_headers'],
        'compiler_flags': ['/nologo', '/EHsc', '/std:c++17', '/O2', '/MT', '/Brepro'],
        'helpers': [{key: record[key] for key in
            ('source', 'source_sha256', 'binary', 'sha256', 'size_bytes')} for record in build['helpers']]}


def build_package(output, helpers, source=SOURCE_ROOT, parser=None, parser_source=None,
                  vulkan_headers=None, progress=print):
    """Validate package inputs, then copy the runtime, licenses and hash manifest."""
    source = Path(source).resolve()
    output = _fresh_output(output, source)
    sources = package_sources(source)
    helpers = Path(helpers).resolve()
    lock, lock_sha256 = _lock(source)
    pin = lock['parser']
    parser = _checked_file(parser or _within(source, pin['default_path']), pin['sha256'], 'pinned parser binary')
    if parser.stat().st_size != pin['size_bytes']:
        raise ValueError('The pinned parser size differs from the dependency lock.')
    parser_source = Path(parser_source or _within(source, pin['default_source_path'])).resolve()
    parser_license = _parser_license(parser_source, pin)
    # Bind the helper binaries to the current dependency lock and source bytes.
    build = read(helpers / 'build.json')
    if build.get('dependency_lock_sha256') != lock_sha256 or build.get('schema_version') != 1:
        raise ValueError('Helpers were not built with the current public dependency lock.')
    records = build['helpers']
    if len(records) != len(HELPERS) or {record['binary'] for record in records} != set(HELPERS):
        raise ValueError('The helper receipt must contain each of the three expected helpers exactly once.')
    for record in records:
        name = record['binary']
        expected_source = 'enshrouded_data/map_tool/native/' + HELPERS[name]
        if record['source'] != expected_source:
            raise ValueError('Unexpected native source in the helper receipt.')
        _checked_file(_within(source, expected_source), record['source_sha256'], 'native source used for helper build')
        binary = _checked_file(_within(helpers, name), record['sha256'], 'built native helper')
        if binary.stat().st_size != record['size_bytes']:
            raise ValueError('A built native helper size differs from its receipt.')
    # Verify all license inputs before creating the package directory.
    if vulkan_headers:
        headers_license = verify_vulkan_headers(vulkan_headers, lock) / 'LICENSE.md'
    else:
        headers_license = _checked_file(helpers / 'Vulkan-Headers-LICENSE.md',
            lock['vulkan_headers']['files']['LICENSE.md'], 'Vulkan-Headers license')
    apache_pin = lock['vulkan_headers']['apache_license']
    apache_license = _checked_file(_within(source, apache_pin['path']),
        apache_pin['sha256'], 'complete Apache-2.0 license text')
    first_party_license = source / 'LICENSE'
    if not first_party_license.is_file() or 'MIT License' not in first_party_license.read_text(encoding='utf-8'):
        raise ValueError('The source root must contain the project MIT LICENSE.')
    if not (source / 'README.md').is_file():
        raise ValueError('The source root must contain the contributor README.md.')
    # Copy only the validated inventory and required third-party redistribution files.
    output.mkdir(parents=True)
    _copy_sources(sources, output)
    (output / 'helpers').mkdir()
    for name in HELPERS:
        shutil.copyfile(helpers / name, output / 'helpers' / name)
    shutil.copyfile(parser, output / 'helpers/kfc-parser.exe')
    third_party = output / 'third-party'
    third_party.mkdir()
    (third_party / 'kfc-parser-LICENSE.txt').write_bytes(parser_license)
    shutil.copyfile(parser_source, third_party / 'kfc-parser-source.zip')
    shutil.copyfile(headers_license, third_party / 'Vulkan-Headers-LICENSE.md')
    shutil.copyfile(apache_license, third_party / 'Apache-2.0.txt')
    write_json(third_party / 'native-build.json', _public_build_receipt(build))
    (third_party / 'README.txt').write_text(
        'First-party map-tool source is covered by the root MIT LICENSE.\n'
        'kfc-parser is a separate upstream GPL-3.0-or-later program.\n'
        'Upstream: ' + pin['source_repository'] + '\n'
        'Corresponding source commit: ' + pin['source_commit'] + '\n'
        'The exact corresponding source, Cargo.lock and upstream license are included.\n'
        'Vulkan-Headers are compile-time declarations; their upstream license is retained.\n'
        'The Vulkan loader and Windows imaging libraries come from the local system.\n', encoding='utf-8')
    # Use py -3.13 when the launcher exists; otherwise use Python on PATH.
    launcher = ('@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
        'where py >nul 2>nul\r\nif errorlevel 1 goto usepython\r\n'
        'py -3.13 -B -m enshrouded_data.map_tool %*\r\nexit /b %errorlevel%\r\n'
        ':usepython\r\npython -B -m enshrouded_data.map_tool %*\r\nexit /b %errorlevel%\r\n')
    (output / 'EnshroudedMaps.cmd').write_bytes(launcher.encode('ascii'))
    bundle = {'version': __version__, 'platform': 'Windows-x64', 'python': '3.13',
        'parser': 'helpers/kfc-parser.exe', 'hall_helper': 'helpers/hollow-geometry.exe',
        'world_helper': 'helpers/world-renderer.exe', 'jpeg_helper': 'helpers/map-jpeg.exe',
        'network_requests': 0, 'files': {}}
    # Hash every payload file; bundle.json itself is written after the inventory.
    bundle['files'] = {path.relative_to(output).as_posix(): digest(path)
                       for path in sorted(output.rglob('*')) if path.is_file()}
    write_json(output / 'bundle.json', bundle)
    progress('Built portable package at ' + str(output))
    return output


def zip_package(root, output):
    """Write a deterministic ZIP with a single enclosing package directory."""
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError('Put the ZIP archive outside the package directory.')
    if output.exists():
        raise ValueError('Preserve previous archives; choose a fresh ZIP filename.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(root.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Package files must not escape the package directory.')
            info = zipfile.ZipInfo(root.name + '/' + path.relative_to(root).as_posix(),
                                   date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with path.open('rb') as source_stream, archive.open(info, 'w') as destination:
                shutil.copyfileobj(source_stream, destination, length=1024 * 1024)
    return {'archive': str(output), 'sha256': digest(output), 'size_bytes': output.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    helpers = subs.add_parser('helpers', help='Compile all three native helpers.')
    package = subs.add_parser('package', help='Create the contributor runtime package.')
    for command in (helpers, package):
        command.add_argument('--source', type=Path, default=SOURCE_ROOT)
        command.add_argument('--output', type=Path, required=True)
        command.add_argument('--vulkan-headers', type=Path)
    helpers.add_argument('--compiler', type=Path)
    helpers.add_argument('--sdk-root', type=Path)
    helpers.add_argument('--sdk-version')
    package.add_argument('--helpers', type=Path, required=True)
    package.add_argument('--parser', type=Path)
    package.add_argument('--parser-source', type=Path)
    package.add_argument('--archive', type=Path)
    arguments = vars(parser.parse_args(argv))
    command = arguments.pop('command')
    archive = arguments.pop('archive', None)
    try:
        result = build_helpers(**arguments) if command == 'helpers' else build_package(**arguments)
        if archive:
            print(json.dumps(zip_package(result, archive), indent=2))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, 'Build failed: ' + str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
