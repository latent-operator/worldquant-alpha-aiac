"""Launch the original application from the current local release pointer."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

def git(source,*args):
    return subprocess.check_output(['git','-C',str(source),*args],text=True).strip()

def validate_release(release):
    if release.get('worker_enabled'):
        raise ValueError('Research worker remains disabled pending a separate bounded implementation')
    if git(release['source'],'rev-parse','HEAD')!=release['commit']:
        raise ValueError('Source commit does not match the release pointer')
    if git(release['source'],'status','--porcelain','--untracked-files=no'):
        raise ValueError('Release source has tracked modifications')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--service',choices=['api','ui'],required=True)
    args=parser.parse_args()
    release=json.loads((args.runtime/'current.json').read_text(encoding='utf-8-sig'))
    validate_release(release)
    from dotenv import load_dotenv
    load_dotenv(args.runtime/'.env',override=True)
    if os.environ.get('POSTGRES_DB')!='aiac_clean': raise ValueError('Unexpected production database')
    if args.service=='ui':
        from aiohttp import web
        from ops.serve_ui import make_app
        web.run_app(make_app(Path(release['ui_dist']),release['commit'],f"http://127.0.0.1:{release['api_port']}"),host='127.0.0.1',port=release['ui_port'],access_log=None)
    else:
        os.chdir(release['source']); sys.path.insert(0,release['source'])
        import uvicorn
        uvicorn.run('backend.main:app',host='127.0.0.1',port=release['api_port'],access_log=False)

if __name__=='__main__':main()
