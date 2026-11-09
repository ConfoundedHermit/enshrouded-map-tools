"""Contributor command line and prompt launcher. All generation stays local."""
import argparse
import html
import json
from pathlib import Path
import sys
import time
import traceback

from . import __version__
from .core import (MapToolError, PACKAGE_ROOT, PROFILE_ROOT, assert_inputs_stable,
    context, digest, identify_game, load_bundle, now, read, separate_output,
    verify_game, write_json)


def _say(message):
    print(message,flush=True)


def _prompt_path(label):
    value = input(label+': ').strip()
    if len(value)>1 and value[0]==value[-1] and value[0] in ('"',"'"):
        value = value[1:-1]
    if not value:
        raise MapToolError(label+' is required.')
    return value


def interactive():
    print('Enshrouded Maps '+__version__+'\nGenerate map images locally, then upload them manually.\n')
    game = _prompt_path('Enshrouded client folder')
    output = _prompt_path('Separate output folder')
    choice = input('Maps: [1] all, [2] Hollow Halls, [3] world (default 1): ').strip() or '1'
    if choice not in ('1','2','3'):
        raise MapToolError('Choose 1, 2 or 3.')
    maps = {'1':'all','2':'halls','3':'world'}[choice]
    layout = 'both'
    if maps!='halls':
        choice = input('World output: [1] single PNG, [2] tiles, [3] both (default 3): ').strip() or '3'
        if choice not in ('1','2','3'):
            raise MapToolError('Choose 1, 2 or 3.')
        layout = {'1':'single','2':'tiles','3':'both'}[choice]
    return ['generate','--game',game,'--output',output,'--maps',maps,'--world-layout',layout]


def _file_manifest(root):
    files = {}
    for directory in ('halls','world'):
        if not (root/directory).exists():
            continue
        for path in sorted((root/directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                files[path.relative_to(root).as_posix()] = {'sha256':digest(path),'size_bytes':path.stat().st_size}
    return files


def verify_output(root):
    root = Path(root).resolve()
    run = read(root/'run.json')
    if run.get('status')!='complete':
        raise MapToolError('This output run has not completed. Use generate --resume with its original options.')
    actual = set(_file_manifest(root)) | {'manual-upload.json','index.html'}
    if actual != set(run['output_files']):
        raise MapToolError('Generated output file set differs from the completion receipt.')
    for name,pin in run['output_files'].items():
        path = (root/name).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or path.stat().st_size!=pin['size_bytes'] or digest(path)!=pin['sha256']):
            raise MapToolError('Generated output changed or is missing: '+name)
    return {'status':'passed','verified_output_files':len(run['output_files']),
            'build_id':run['build_id'],'network_requests':0,'game_writes':0}


def _hall_uploads(root, halls):
    directory = root/'halls/descriptions'
    directory.mkdir(exist_ok=True)
    rows = []
    for hall in halls['maps']:
        local = 'halls/'+hall['image']
        description = 'halls/descriptions/'+hall['slug']+'.wikitext'
        filename = hall['name']+' Map.png'
        text = ('== Summary ==\nFlattened interior map of [['+hall['name']+']], generated from '
                'Enshrouded client build '+halls['build_id']+'. Upper and lower surfaces are combined '
                'in one overhead projection. North is up. The scale bar represents 50 meters.\n\n'
                'Gold circles mark checkpoints; the entrance and return portal are labeled. '
                'Overlapping passages may be at different heights and do not necessarily connect. '
                'Some objects and changing obstacles are not shown.\n')
        if hall['slug']=='nomad-highlands':
            text += '\nPurple circles mark internal portal positions. Coincident symbols may represent different heights. Portal connections are not shown.\n'
        (root/description).write_text(text,encoding='utf-8')
        rows.append({'local_file':local,'wiki_filename':filename,'description_file':description,
            'sha256':digest(root/local),'size_bytes':(root/local).stat().st_size,
            'dimensions':hall['dimensions'],'upload_eligible':(root/local).stat().st_size<=15*1024*1024})
    return rows


def _gallery(root, run, halls, world):
    esc = html.escape
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Enshrouded generated maps</title><style>body{font:16px/1.6 system-ui;margin:2rem auto;padding:0 1rem;max-width:1300px;background:#f2eedf;color:#2c403c}a{color:#245b64}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}article{padding:1rem;background:#f9f7ed}img{display:block;width:100%;height:260px;object-fit:contain}code{overflow-wrap:anywhere}</style>',
        '<h1>Generated Enshrouded maps</h1><p>Client build '+esc(run['build_id'])+
        '. Review the maps and validation report before manual upload. This tool makes no wiki edits.</p>',
        '<p><a href="run.json">Validation and provenance</a> · <a href="manual-upload.json">Upload manifest</a></p>']
    if world:
        parts.append('<p><a href="world/index.html">Open world map exports and upload instructions</a></p>')
    if halls:
        parts.append('<h2>Hollow Halls</h2><section>')
        for hall in halls['maps']:
            path = 'halls/'+hall['image']
            parts.append('<article><h3>'+esc(hall['name'])+'</h3><a href="'+esc(path,quote=True)+'"><img src="'+
                esc(path,quote=True)+'" alt="'+esc(hall['name'],quote=True)+'"></a><p>'+str(hall['dimensions'][0])+' × '+str(hall['dimensions'][1])+' pixels</p></article>')
        parts.append('</section>')
    parts.append('<h2>Manual upload</h2><p>Sign into your own wiki account. Review the current wiki image before replacing it. '
        'Match every proposed filename and world tile position. For existing files use Upload a new version and preserve the current description; '
        'for new files use the generated description after review. Check current upload limits and the final rendered pages.</p>'
        '<p>A full-resolution world master may exceed the wiki limit. The manifest marks files against the configured 15 MiB limit. '
        'A changed protected background definition may require an administrator. No contributor credentials are stored or requested.</p></html>')
    (root/'index.html').write_text('\n'.join(parts),encoding='utf-8')


def generate(args):
    game, output = separate_output(args.game,args.output)
    profile = identify_game(game)
    bundle_root = Path(args.bundle).resolve() if args.bundle else PACKAGE_ROOT
    bundle = load_bundle(bundle_root)
    request = {'game':str(game),'maps':args.maps,'world_layout':args.world_layout,
        'world_pixels':args.world_pixels,'build_id':profile['build_id'],
        'bundle_sha256':digest(bundle_root/'bundle.json')}
    if output.exists() and any(output.iterdir()):
        if not args.resume or not (output/'run.json').is_file():
            raise MapToolError('Output folder is not empty. Choose a fresh folder, or use --resume for this tool\'s existing run.')
        previous = read(output/'run.json')
        if previous['request']!=request:
            raise MapToolError('Resume options, game folder or tool package differ from the original run.')
        if previous['status']=='complete':
            report = verify_output(output)
            _say(json.dumps(report,indent=2))
            return report
    output.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    run = {'version':__version__,'started_at':now(),'status':'running','request':request,
           'build_id':profile['build_id'],'game_writes':0,'network_requests':0}
    write_json(output/'run.json',run)
    try:
        verification = verify_game(game,profile,_say)
        write_json(output/'inputs.json',verification)
        ctx = context(game,output,profile,bundle_root,bundle,args.maps,
                      resume=args.resume,progress=_say,world_pixels=args.world_pixels)
        halls = world = None
        if args.maps in ('all','halls'):
            from .halls import run_halls
            halls = run_halls(ctx,output/'halls',progress=_say)
        if args.maps in ('all','world'):
            from .world import run_world
            from .delivery import write_world_delivery
            rendered = run_world(ctx,output/'cache/world-render',progress=_say)
            tiles = rendered['tiles'] if isinstance(rendered,dict) else rendered
            _say('Preparing world '+args.world_layout+' output; a full-resolution master can take several minutes.')
            world = write_world_delivery(tiles,output/'world',layout=args.world_layout,
                build_id=profile['build_id'],jpeg_helper=ctx.jpeg_helper,resume=args.resume)
        uploads = _hall_uploads(output,halls) if halls else []
        if world:
            uploads += [{**u,'local_file':'world/'+u['local_file'],
                'description_file':'world/'+u['description_file']} for u in world['uploads']]
        write_json(output/'manual-upload.json',{'build_id':profile['build_id'],'uploads':uploads,
            'network_requests':0,'instructions':'Review each image, current wiki identity, file limit and description before manual upload.'})
        assert_inputs_stable(game,verification)
        run.update(status='complete',finished_at=now(),elapsed_seconds=round(time.monotonic()-started,3),
            hall_map_count=0 if halls is None else halls['map_count'],
            world_layout=None if world is None else world['layout'],output_files=_file_manifest(output),
            manual_upload_files=len(uploads))
        _gallery(output,run,halls,world)
        for name in ('manual-upload.json','index.html'):
            run['output_files'][name]={'sha256':digest(output/name),'size_bytes':(output/name).stat().st_size}
        write_json(output/'run.json',run)
        _say('Complete. Open '+str(output/'index.html'))
        return run
    except BaseException as error:
        run.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',
            finished_at=now(),elapsed_seconds=round(time.monotonic()-started,3),
            error_type=type(error).__name__,error=str(error))
        write_json(output/'run.json',run)
        (output/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv or argv==['--interactive']:
            argv = interactive()
        parser = argparse.ArgumentParser(prog='EnshroudedMaps',description='Generate map images locally from a supported Enshrouded client.')
        parser.add_argument('--version',action='version',version=__version__)
        subs = parser.add_subparsers(dest='command',required=True)
        generate_parser = subs.add_parser('generate',help='Generate maps from your own game files')
        generate_parser.add_argument('--game',type=Path,required=True)
        generate_parser.add_argument('--output',type=Path,required=True)
        generate_parser.add_argument('--maps',choices=['all','halls','world'],default='all')
        generate_parser.add_argument('--world-layout',choices=['single','tiles','both'],default='both')
        generate_parser.add_argument('--world-pixels',choices=[1280,2560],type=int,default=2560,help='Pixels per world tile; 2560 matches the published density')
        generate_parser.add_argument('--resume',action='store_true')
        generate_parser.add_argument('--bundle',type=Path,help=argparse.SUPPRESS)
        check_parser = subs.add_parser('verify',help='Verify a completed output without reading the game or contacting the wiki')
        check_parser.add_argument('--output',type=Path,required=True)
        subs.add_parser('profiles',help='List supported client builds')
        args = parser.parse_args(argv)
        if args.command=='generate':
            generate(args)
        elif args.command=='verify':
            _say(json.dumps(verify_output(args.output),indent=2))
        else:
            _say(json.dumps([{'build_id':p['build_id'],'mode':p['mode']} for p in read(PROFILE_ROOT/'builds.json')['profiles']],indent=2))
        return 0
    except (MapToolError,ValueError,OSError,RuntimeError) as error:
        print('Map generation stopped: '+str(error),file=sys.stderr,flush=True)
        return 2
    except KeyboardInterrupt:
        print('Interrupted. Outputs are preserved; use --resume with the same options.',file=sys.stderr,flush=True)
        return 130


if __name__=='__main__':
    raise SystemExit(main())
