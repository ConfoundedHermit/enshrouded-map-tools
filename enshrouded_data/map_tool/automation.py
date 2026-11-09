"""Build a release from a clean source checkout, with verified dependencies.

Unlike the offline portable builder, this developer command downloads pinned
build dependencies. It never reads game files or needs a graphics device.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from . import __version__
from .core import digest, load_bundle, read, write_json
from . import portable_build as build


BOOTSTRAP_URL = ('https://github.com/ConfoundedHermit/enshrouded-map-tools/'
    'releases/download/v0.1.0/EnshroudedMaps-0.1.0-Windows-x64.zip')
BOOTSTRAP_SHA256 = 'f670331ab6c2d67c592953fedb08cd35f8da90618a64af48ecf7dec9c12490ac'
BOOTSTRAP_MEMBER = 'EnshroudedMaps-0.1.0-Windows-x64/third-party/kfc-parser-source.zip'
MAX_DOWNLOAD = 64 * 1024 * 1024
TAG_PATTERN = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{number}'
    for prefix in ('COM', 'LPT') for number in range(1, 10)}


def release_tag(tag=None, version=__version__):
    tag = tag or 'v' + version
    if not TAG_PATTERN.fullmatch(tag) or tag != 'v' + version:
        raise ValueError('Release tag must equal the checked-in version: v' + version)
    return tag


def _https(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Dependency URLs must be absolute HTTPS URLs without credentials or fragments.')
    return url


def fetch_verified(url, destination, checksum, size_bytes=None, opener=urlopen):
    """Keep good cached files, reject changed ones, and atomically commit downloads."""
    _https(url)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file() or digest(destination) != checksum:
            raise ValueError('Existing dependency differs from its pin: ' + str(destination))
        if size_bytes is not None and destination.stat().st_size != size_bytes:
            raise ValueError('Existing dependency size differs from its pin.')
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + '.', suffix='.part', delete=False) as output:
            temporary = Path(output.name)
            request = Request(url, headers={'User-Agent': 'enshrouded-map-tools-release/' + __version__})
            with opener(request, timeout=60) as response:
                _https(response.geturl())
                total = 0
                checksum_state = hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD or (size_bytes is not None and total > size_bytes):
                        raise ValueError('Dependency download exceeds its allowed size.')
                    checksum_state.update(chunk)
                    output.write(chunk)
            if checksum_state.hexdigest() != checksum or (size_bytes is not None and total != size_bytes):
                raise ValueError('Downloaded dependency differs from its pinned SHA-256 or size: ' + url)
        # This command runs in a private build directory; never replace a cache
        # file that appeared while the download was being checked.
        if destination.exists() or destination.is_symlink():
            raise ValueError('Dependency destination appeared during download.')
        temporary.rename(destination)
        return destination
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _zip_members(archive, max_bytes=256 * 1024 * 1024):
    """Validate all names before writing, including Windows aliases and links."""
    members, seen, total = [], set(), 0
    for info in archive.infolist():
        # ZipInfo normalizes backslashes on Windows and truncates NUL names.
        # Validate the original header text before either transformation.
        name = info.orig_filename
        parts = PurePosixPath(name).parts
        if ('\\' in name or ':' in name or '\x00' in name or not parts or
                name.startswith('/') or any(part in ('', '.', '..') for part in name.rstrip('/').split('/'))):
            raise ValueError('Unsafe ZIP member: ' + name)
        if any(part.endswith((' ', '.')) or part.split('.')[0].upper() in RESERVED for part in parts):
            raise ValueError('Unsafe Windows ZIP member: ' + name)
        key = '/'.join(parts).casefold()
        if key in seen:
            raise ValueError('Duplicate or case-aliased ZIP member: ' + name)
        seen.add(key)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not info.is_dir()):
            raise ValueError('ZIP links and special files are not supported: ' + name)
        total += info.file_size
        if info.flag_bits & 1 or total > max_bytes or len(members) >= 20000:
            raise ValueError('ZIP exceeds extraction limits or contains encrypted entries.')
        members.append(info)
    files = {str(PurePosixPath(info.filename)).casefold() for info in members if not info.is_dir()}
    for info in members:
        if any(str(parent).casefold() in files for parent in PurePosixPath(info.filename).parents if str(parent) != '.'):
            raise ValueError('ZIP file is also a parent directory: ' + info.filename)
    return members


def extract_verified_zip(archive_path, destination, expected_root=None):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Extraction requires a fresh destination: ' + str(destination))
    with zipfile.ZipFile(archive_path) as archive:
        members = _zip_members(archive)
        if expected_root and any(PurePosixPath(info.filename).parts[0] != expected_root for info in members):
            raise ValueError('ZIP has an unexpected top-level directory.')
        destination.mkdir(parents=True)
        for info in members:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError('ZIP destination escapes its directory.')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
    return destination


def _parser_source(pin, destination, dependencies, progress):
    if destination.exists():
        return build._checked_file(destination, pin['source_archive_sha256'], 'pinned parser source archive')
    # Git archive normally reproduces the original ZIP. Git/zlib versions can
    # change ZIP bytes; a different archive never weakens the package hash pin.
    repository = dependencies / 'parser-source-git'
    generated = dependencies / 'parser-source-generated.zip'
    if repository.exists() or generated.exists():
        raise ValueError('Preserve partial source preparation; use a fresh dependency directory.')
    try:
        subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
        subprocess.run(['git', '-C', str(repository), 'fetch', '--quiet', '--depth=1',
            _https(pin['source_repository']), pin['source_commit']], check=True)
        commit = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'FETCH_HEAD'], text=True).strip()
        if commit != pin['source_commit']:
            raise ValueError('Parser source fetch did not resolve to its pinned commit.')
        subprocess.run(['git', '-C', str(repository), 'archive', '--format=zip',
            '--output=' + str(generated), commit], check=True)
        if digest(generated) == pin['source_archive_sha256']:
            generated.rename(destination)
            return destination
        progress('Git archive bytes differ; obtaining the exact pinned source ZIP from v0.1.0.')
    except (OSError, subprocess.CalledProcessError):
        progress('Git source preparation unavailable; obtaining the exact pinned source ZIP from v0.1.0.')
    bootstrap = fetch_verified(BOOTSTRAP_URL, dependencies / 'source-bootstrap-v0.1.0.zip', BOOTSTRAP_SHA256)
    with zipfile.ZipFile(bootstrap) as archive:
        _zip_members(archive)
        info = archive.getinfo(BOOTSTRAP_MEMBER)
        if info.file_size > MAX_DOWNLOAD:
            raise ValueError('Parser source archive exceeds its allowed size.')
        data = archive.read(info)
    if hashlib.sha256(data).hexdigest() != pin['source_archive_sha256']:
        raise ValueError('Bootstrap parser source differs from the original source ZIP pin.')
    with destination.open('xb') as output:
        output.write(data)
    return destination


def prepare_dependencies(source, dependencies, progress=print):
    source, dependencies = Path(source).resolve(), Path(dependencies).resolve()
    dependencies.mkdir(parents=True, exist_ok=True)
    lock, _ = build._lock(source)
    pin = lock['parser']
    parser = fetch_verified(pin['url'], dependencies / 'kfc-parser.exe', pin['sha256'], pin['size_bytes'])
    parser_source = _parser_source(pin, dependencies / 'kfc-parser-source.zip', dependencies, progress)
    vulkan = lock['vulkan_headers']
    headers = dependencies / Path(vulkan['default_path']).name
    if headers.exists():
        build.verify_vulkan_headers(headers, lock)
    else:
        archive = fetch_verified(vulkan['url'], dependencies / 'Vulkan-Headers.zip', vulkan['archive_sha256'])
        extraction = extract_verified_zip(archive, dependencies / 'vulkan-extracted', expected_root=headers.name)
        build.verify_vulkan_headers(extraction / headers.name, lock)
        (extraction / headers.name).rename(headers)
    return {'parser': parser, 'parser_source': parser_source, 'vulkan_headers': headers}


def source_commit(source):
    result = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], text=True)
    if result.strip():
        raise ValueError('Release builds require a clean source checkout, including untracked files.')
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ValueError('Cannot identify the source commit.')
    return commit


def verify_package_sources(source, commit):
    """Every public source must be a regular file in the captured commit."""
    sources = build.package_sources(source)
    tree = subprocess.check_output(['git', '-C', str(source), 'ls-tree', '-rz',
        commit, '--', '.'], text=True, encoding='utf-8')
    regular = set()
    for entry in tree.split('\0'):
        if not entry:
            continue
        metadata, name = entry.split('\t', 1)
        mode, kind, _ = metadata.split()
        if kind == 'blob' and mode in ('100644', '100755'):
            regular.add(name)
    missing = sources.keys() - regular
    if missing:
        raise ValueError('Public package sources must be committed regular files: ' + ', '.join(sorted(missing)))


def write_assets(output, archive, tag, commit, version=__version__):
    """Bind the exact two public assets to a version and checked-out commit."""
    output, archive = Path(output).resolve(), Path(archive).resolve()
    release_tag(tag, version)
    expected = 'EnshroudedMaps-' + version + '-Windows-x64.zip'
    if archive.parent != output or archive.name != expected or not archive.is_file():
        raise ValueError('Unexpected release archive path or name.')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ValueError('Invalid release source commit.')
    checksum = output / 'SHA256SUMS.txt'
    with checksum.open('x', encoding='ascii', newline='\n') as stream:
        stream.write(digest(archive) + '  ' + archive.name + '\n')
    manifest = {'schema_version': 1, 'tag': tag, 'version': version, 'source_commit': commit,
        'assets': [{'name': path.name, 'sha256': digest(path), 'size_bytes': path.stat().st_size}
                   for path in (archive, checksum)]}
    write_json(output / 'release-assets.json', manifest)
    return manifest


def build_release(output, dependencies, source=build.SOURCE_ROOT, tag=None, progress=print):
    tag = release_tag(tag)
    source, output = Path(source).resolve(), Path(output).resolve()
    commit = source_commit(source)
    verify_package_sources(source, commit)
    build._fresh_output(output, source)
    # Keep build/dependency artifacts out of the source and final package.
    output.mkdir(parents=True)
    deps = prepare_dependencies(source, dependencies, progress)
    helpers = build.build_helpers(output / 'helpers', source=source,
        vulkan_headers=deps['vulkan_headers'], progress=progress)
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    environment['ENSHROUDED_MAP_JPEG_HELPER'] = str(helpers / 'map-jpeg.exe')
    subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
        '-p', 'test_map_tool*.py'], cwd=source, env=environment, check=True)
    name = 'EnshroudedMaps-' + __version__ + '-Windows-x64'
    package = build.build_package(output / name, helpers, source=source, progress=progress, **deps)
    archive = output / (name + '.zip')
    build.zip_package(package, archive)
    clean = extract_verified_zip(archive, output / 'package-smoke', expected_root=name) / name
    bundle = load_bundle(clean)
    if bundle['version'] != __version__:
        raise ValueError('Packaged version differs from the release version.')
    for args in (['--help'], ['profiles']):
        subprocess.run([sys.executable, '-B', '-m', 'enshrouded_data.map_tool', *args],
            cwd=clean, env=environment, check=True)
    command_shell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/cmd.exe'
    subprocess.run([str(command_shell), '/d', '/c', 'EnshroudedMaps.cmd --help'],
        cwd=clean, env=environment, check=True)
    # Confirm tests/building did not modify any tracked source after provenance
    # was captured. All generated files should live in ignored directories.
    if source_commit(source) != commit:
        raise ValueError('The source commit changed during the release build.')
    return write_assets(output, archive, tag, commit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='Must equal v plus the checked-in package version.')
    parser.add_argument('--output', type=Path, required=True, help='Fresh build and asset directory.')
    parser.add_argument('--dependencies', type=Path, default=Path('.dependencies'))
    arguments = parser.parse_args(argv)
    try:
        manifest = build_release(**vars(arguments))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Automated build failed: ' + str(error) + '\n')
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
