"""Attach exactly the tested ZIP and checksum to their published release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

SHA = re.compile(r'[0-9a-f]{40}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_files(directory, tag, commit):
    """Verify the complete artifact inventory, hashes and tested release identity."""
    checked(TAG.fullmatch(tag) and SHA.fullmatch(commit), 'Invalid release tag or source commit.')
    root = Path(directory).resolve()
    manifest = json.loads((root/'release-assets.json').read_text(encoding='utf-8'))
    version = tag[1:]
    names = {f'EnshroudedMaps-{version}-Windows-x64.zip', 'SHA256SUMS.txt'}
    checked(manifest.get('schema_version') == 1 and manifest.get('tag') == tag
        and manifest.get('version') == version and manifest.get('source_commit') == commit,
        'Build metadata differs from the release tag or tested commit.')
    rows = manifest.get('assets', [])
    checked(len(rows) == 2 and {row.get('name') for row in rows} == names,
        'Expected exactly the versioned Windows ZIP and SHA256SUMS.txt.')
    checked({p.name for p in root.iterdir()} == names | {'release-assets.json'},
        'The release artifact contains unexpected files.')
    for row in rows:
        path = root/row['name']
        checked(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root),
            'Release asset must be a regular file inside its artifact directory.')
        checked(SHA256.fullmatch(row.get('sha256', ''))
            and type(row.get('size_bytes')) is int and path.stat().st_size == row['size_bytes']
            and digest(path) == row['sha256'], 'Release asset differs from its tested hash or size.')
    archive = next(row for row in rows if row['name'].endswith('.zip'))
    expected = archive['sha256']+'  '+archive['name']
    checked((root/'SHA256SUMS.txt').read_text(encoding='ascii').splitlines() == [expected],
        'The checksum file does not describe the exact release archive.')
    return root, rows


class GitHub:
    """Authenticated API and asset-upload requests for one repository."""

    def __init__(self, repository, token, opener=urlopen):
        checked(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Invalid repository.')
        checked(bool(token), 'GH_TOKEN is required for release attachment.')
        self.repository, self.token, self.opener = repository, token, opener
        self.base = 'https://api.github.com/repos/'+repository

    def request(self, url, data=None, content_type=None):
        headers = {'Authorization': 'Bearer '+self.token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'EnshroudedMapToolsRelease/1.0'}
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
        # Callers use repository API URLs or the validated release upload endpoint.
        with self.opener(request, timeout=120) as response:
            return json.load(response)

    def api(self, suffix):
        return self.request(self.base+suffix)

    def upload(self, url, path):
        return self.request(url+'?name='+quote(path.name, safe=''), data=path.read_bytes(),
            content_type='application/zip' if path.suffix == '.zip' else 'text/plain; charset=utf-8')


def tag_commit(client, tag):
    """Resolve lightweight or nested annotated tags to a commit, with a depth limit."""
    ref = client.api('/git/ref/tags/'+quote(tag, safe=''))
    checked(ref.get('ref') == 'refs/tags/'+tag, 'GitHub returned a different tag reference.')
    obj = ref['object']
    for _ in range(6):
        checked(SHA.fullmatch(obj.get('sha', '')), 'Invalid tag object identity.')
        if obj.get('type') == 'commit':
            return obj['sha']
        checked(obj.get('type') == 'tag', 'The release tag does not point to a commit.')
        obj = client.api('/git/tags/'+obj['sha'])['object']
    raise ValueError('Too many annotated tag levels.')


def existing_assets(client, release_id):
    """Read the bounded, paginated asset list and require unique remote names."""
    rows = []
    for page in range(1, 12):
        batch = client.api(f'/releases/{release_id}/assets?per_page=100&page={page}')
        checked(isinstance(batch, list), 'Unexpected release asset list.')
        rows.extend(batch)
        if len(batch) < 100:
            checked(len({row['name'] for row in rows}) == len(rows), 'Duplicate remote asset names.')
            return {row['name']: row for row in rows}
    raise ValueError('Release asset pagination exceeded its bound.')


def matches(remote, local):
    return (remote.get('state') == 'uploaded' and remote.get('size') == local['size_bytes']
        and remote.get('digest') == 'sha256:'+local['sha256'])


def attach(directory, release_id, tag, commit, client):
    """Upload missing tested assets, preserving identical assets and rejecting conflicts."""
    root, rows = validate_files(directory, tag, commit)
    checked(type(release_id) is int and release_id > 0, 'Invalid release ID.')
    release = client.api('/releases/'+str(release_id))
    checked(release.get('id') == release_id and release.get('tag_name') == tag
        and release.get('draft') is False and bool(release.get('published_at')),
        'The release is missing, unpublished or belongs to another tag.')
    checked(tag_commit(client, tag) == commit, 'The current release tag differs from the tested commit.')
    upload_url = release.get('upload_url', '').split('{', 1)[0]
    parsed = urlsplit(upload_url)
    expected_path = f'/repos/{client.repository}/releases/{release_id}/assets'
    checked(parsed.scheme == 'https' and parsed.netloc == 'uploads.github.com'
        and parsed.path == expected_path and not parsed.query and not parsed.fragment,
        'Unexpected release upload endpoint.')
    present = existing_assets(client, release_id)
    # Reject every asset conflict before starting uploads.
    for row in rows:
        if row['name'] in present:
            checked(matches(present[row['name']], row),
                'An existing asset has different or unverifiable bytes: '+row['name'])
    missing = [row for row in rows if row['name'] not in present]
    checked(not missing or not release.get('immutable', False),
        'This release is immutable; attach assets to a draft before publishing it.')
    uploaded, skipped = [], []
    for row in rows:
        if row['name'] in present:
            skipped.append(row['name'])
            continue
        result = client.upload(upload_url, root/row['name'])
        checked(result.get('name') == row['name'] and matches(result, row),
            'GitHub upload acknowledgement differs from the tested asset: '+row['name'])
        uploaded.append(row['name'])
    # Recheck remote asset hashes and release identity after all uploads.
    final = existing_assets(client, release_id)
    checked(all(row['name'] in final and matches(final[row['name']], row) for row in rows),
        'Final release asset verification failed.')
    current = client.api('/releases/'+str(release_id))
    checked(current.get('id') == release_id and current.get('tag_name') == tag
        and current.get('draft') is False and bool(current.get('published_at')),
        'The release identity changed during attachment.')
    checked(tag_commit(client, tag) == commit, 'The release tag moved during attachment.')
    result = {'status': 'passed', 'release_id': release_id, 'tag': tag, 'source_commit': commit,
        'uploaded': uploaded, 'already_present': skipped}
    print(json.dumps(result, indent=2))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--release-id', type=int, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    client = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
    attach(args.directory, args.release_id, args.tag, args.commit, client)


if __name__ == '__main__':
    main()
