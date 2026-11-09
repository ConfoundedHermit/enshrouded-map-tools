"""Local input verification and fresh descriptor extraction; no network code."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
from types import SimpleNamespace
import uuid

from ..kfc import KFCIndex


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = Path(__file__).with_name('profiles')


class MapToolError(RuntimeError):
    """An unsupported input or incomplete operation with a useful local report."""


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.writing')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    temporary.replace(path)


def within(root, name):
    root = Path(root).resolve()
    path = (root/name).resolve()
    if not path.is_relative_to(root):
        raise MapToolError('A package path escapes its directory: '+str(name))
    return path


def separate_output(game, output):
    game, output = Path(game).resolve(), Path(output).resolve()
    if output.is_relative_to(game) or game.is_relative_to(output):
        raise MapToolError('Choose an output folder separate from the game folder and its parent folders.')
    return game, output


def identify_game(game, profiles=None):
    game = Path(game).resolve()
    executable = game/'enshrouded.exe'
    if not executable.is_file():
        raise MapToolError('The game folder must contain enshrouded.exe and the client data files.')
    checksum = digest(executable)
    choices = profiles if profiles is not None else read(PROFILE_ROOT/'builds.json')['profiles']
    matching = [p for p in choices if p['executable_sha256']==checksum]
    if len(matching)!=1:
        raise MapToolError('This client executable is unsupported or modified. Use a supported, unmodified client installation. SHA-256: '+checksum)
    return matching[0]


def verify_game(game, profile, progress=print):
    """Check the client archives against the supported build before extracting."""
    game = Path(game).resolve()
    records = []
    for number, expected in enumerate(profile['files'], 1):
        path = within(game, expected['name'])
        if not path.is_file() or path.stat().st_size!=expected['size_bytes']:
            raise MapToolError('Missing or modified supported-build input: '+expected['name'])
        before = path.stat()
        progress(f"Checking input {number}/{len(profile['files'])}: {path.name}")
        checksum = digest(path)
        after = path.stat()
        if checksum!=expected['sha256'] or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise MapToolError('Modified or changing game input: '+path.name+'. Select an unmodified installation; the tool will not change it.')
        records.append({'name':path.name,'size_bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'sha256':checksum})
    index = KFCIndex(game)
    if index.version!=profile['kfc_version']:
        raise MapToolError('Game resource version differs from the supported profile.')
    return {'at':now(),'status':'verified_supported_client','build_id':profile['build_id'],
            'game':str(game),'kfc_version':index.version,'files':records,
            'verified_bytes':sum(r['size_bytes'] for r in records),'game_writes':0}


def assert_inputs_stable(game, verification):
    for record in verification['files']:
        stat = (Path(game)/record['name']).stat()
        if (stat.st_size,stat.st_mtime_ns)!=(record['size_bytes'],record['mtime_ns']):
            raise MapToolError('Game input changed during generation: '+record['name'])


def load_bundle(root):
    root = Path(root).resolve()
    path = root/'bundle.json'
    if not path.is_file():
        raise MapToolError('The prebuilt helper package is missing. Run the launcher from the extracted contributor ZIP.')
    bundle = read(path)
    for name, expected in bundle['files'].items():
        if digest(within(root,name))!=expected:
            raise MapToolError('Package integrity check failed: '+name+'. Extract a fresh copy of the release ZIP.')
    return bundle


def indexed_resources(index):
    offset, count = index.location(96)
    if offset+count*32>len(index.data):
        raise MapToolError('Resource identity table exceeds the KFC index.')
    records = []
    for i in range(count):
        at = offset+i*32
        records.append((str(uuid.UUID(bytes_le=index.data[at:at+16])), *struct.unpack_from('<II',index.data,at+16)))
    if len(set(records))!=len(records):
        raise MapToolError('Duplicate resource identities in the client index.')
    return records


def extraction_selection(index, profile, maps):
    types = profile['resource_types']
    scene = profile['main_scene_guid']
    wanted = set()
    for guid,kind,part in indexed_resources(index):
        hall = maps in ('all','halls') and (
            kind==types['keen::ecs::TemplateResource']
            or guid==scene and kind in {types['keen::SceneResource'],types['keen::SceneEntityChunkResource'],types['keen::VoxelWorldResource']})
        world = maps in ('all','world') and (
            guid==profile['map_guid'] and kind in {types['keen::UiMapResource'],types['keen::UiTextureResource']}
            or guid==profile['shader_guid'] and kind==types['keen::GpcResource']
            or guid==profile['ui_guid'] and kind==types['keen::FbUiBundle']
            or kind==types['keen::BalancingTable'])
        if hall or world:
            wanted.add((guid,kind,part))
    if not wanted:
        raise MapToolError('No selected map resources found.')
    return wanted


def _run_parser(arguments, log, progress):
    progress(' '.join([Path(arguments[0]).name,*arguments[1:2]])+' …')
    result = subprocess.run(arguments,capture_output=True,text=True,encoding='utf-8',errors='replace')
    Path(log).write_text(result.stdout+result.stderr,encoding='utf-8')
    if result.returncode:
        raise MapToolError('Game resource extraction failed. Inspect '+str(log))
    return result.stdout+result.stderr


def extract_descriptors(game, output, profile, maps, parser, *, resume=False, progress=print):
    """Put parser/reflection beside the output, never beside the game executable."""
    output = Path(output)
    raw = output/'cache/descriptors'
    receipt = output/'cache/extraction.json'
    selected = extraction_selection(KFCIndex(Path(game)),profile,maps)
    selection_key = hashlib.sha256(json.dumps(sorted(selected)).encode()).hexdigest()
    if resume and receipt.is_file():
        previous = read(receipt)
        if previous['selection_sha256']!=selection_key or previous['profile_build_id']!=profile['build_id']:
            raise MapToolError('Existing extraction belongs to different selected inputs.')
        actual = {p.relative_to(raw).as_posix() for p in raw.rglob('*.json')}
        if actual!=set(previous['descriptors']):
            raise MapToolError('Existing extraction file set is incomplete or changed.')
        for name, checksum in previous['descriptors'].items():
            if digest(within(raw,name))!=checksum:
                raise MapToolError('Existing extracted descriptor changed: '+name)
        reflection = output/'cache/parser/reflection_data.json'
        if digest(reflection)!=previous['reflection_sha256']:
            raise MapToolError('Existing extraction reflection changed.')
        progress('Reusing verified descriptor extraction.')
        return raw, reflection
    if raw.exists() or receipt.exists():
        raise MapToolError('An unfinished descriptor extraction exists. Use a fresh output folder.')
    parser_dir = output/'cache/parser'
    parser_dir.mkdir(parents=True,exist_ok=True)
    private_parser = parser_dir/'kfc-parser.exe'
    shutil.copyfile(parser,private_parser)
    if digest(private_parser)!=profile['parser_sha256']:
        raise MapToolError('Pinned extraction utility changed.')
    _run_parser([str(private_parser.resolve()),'extract-types','-g',str(Path(game).resolve()),'--file-name','enshrouded'],
                output/'cache/reflection.log',progress)
    reflection = parser_dir/'reflection_data.json'
    registry = read(reflection)
    if registry['version']!=profile['kfc_version']:
        raise MapToolError('Fresh reflection version differs from the selected client profile.')
    types = {t['qualifiedName']:t['qualifiedHash'] for t in registry['types']}
    if any(types.get(name)!=kind for name,kind in profile['resource_types'].items()):
        raise MapToolError('Fresh reflection resource types differ from the supported client profile.')
    # Use type filters for complete types, keeping the command below Windows' argv limit.
    complete = {'keen::ecs::TemplateResource'} if maps in ('all','halls') else set()
    if maps in ('all','world'):
        complete.add('keen::BalancingTable')
    ids = sorted({g for g,k,p in selected if k not in {types[n] for n in complete}})
    # Guid filters in this CLI require full type/part identities. For numerous scene
    # chunks use the complete type then verify the main-scene subset separately.
    filters = ['t'+name for name in sorted(complete)]
    entity_kind = types['keen::SceneEntityChunkResource']
    if maps in ('all','halls'):
        filters.append('tkeen::SceneEntityChunkResource')
    filters += [f'{g}_{k:08x}_{p}' for g,k,p in sorted(selected)
                if k not in {types[n] for n in complete} and k!=entity_kind]
    log = _run_parser([str(private_parser.resolve()),'-t','4','unpack','-g',str(Path(game).resolve()),
        '--file-name','enshrouded','-o',str(raw.resolve()),'-f',','.join(filters)],output/'cache/extraction.log',progress)
    if re.search(r'\([1-9][0-9]* raw|[1-9][0-9]* failed\)',log) or list(raw.rglob('*.bin')):
        raise MapToolError('Resource extraction contains raw or failed descriptors. Inspect cache/extraction.log.')
    decoded = {}
    descriptor_hashes = {}
    for path in sorted(raw.rglob('*.json')):
        value = read(path)
        try:
            key = (value['$guid'],types[value['$type']],value['$part'])
        except KeyError as error:
            raise MapToolError('Unidentified extracted descriptor: '+str(path)) from error
        if key in decoded:
            raise MapToolError('Duplicate extracted resource identity.')
        decoded[key] = path
        descriptor_hashes[path.relative_to(raw).as_posix()] = digest(path)
    absent = selected-decoded.keys()
    allowed = set(indexed_resources(KFCIndex(Path(game))))
    extras = decoded.keys()-selected
    if absent or any(k not in allowed or k[1]!=entity_kind for k in extras):
        raise MapToolError('Extracted descriptors do not cover the requested indexed resources.')
    write_json(receipt,{'at':now(),'profile_build_id':profile['build_id'],'maps':maps,
        'selection_sha256':selection_key,'selected_resources':len(selected),
        'decoded_resources':len(decoded),'types':dict(Counter(read(p)['$type'] for p in decoded.values())),
        'reflection_sha256':digest(reflection),'parser_sha256':digest(private_parser),
        'descriptors':descriptor_hashes,'game_writes':0})
    return raw, reflection


def context(game, output, profile, bundle_root, bundle, maps, *, resume=False, progress=print, world_pixels=2560):
    raw, reflection = extract_descriptors(game,output,profile,maps,within(bundle_root,bundle['parser']),resume=resume,progress=progress)
    return SimpleNamespace(game=Path(game),raw_root=raw,index=KFCIndex(Path(game)),profile=profile,
        parser=within(bundle_root,bundle['parser']),reflection=reflection,
        hall_helper=within(bundle_root,bundle['hall_helper']),
        world_helper=within(bundle_root,bundle['world_helper']),
        jpeg_helper=within(bundle_root,bundle['jpeg_helper']),
        resume=resume,world_pixels=world_pixels)
