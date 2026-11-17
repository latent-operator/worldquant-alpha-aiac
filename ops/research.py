"""Dispatch or inspect native AIAC research; never poll in a resident agent loop."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request


def request(url, method='GET', body=None):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=20) as response: return json.load(response)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8003')
    commands=parser.add_subparsers(dest='command',required=True)
    create=commands.add_parser('create'); create.add_argument('--manifest',type=Path,required=True)
    for name in ('start','resume','pause','stop'):
        p=commands.add_parser(name); p.add_argument('--task-id',type=int,required=True)
    p=commands.add_parser('report'); p.add_argument('--run-id',type=int,required=True)
    commands.add_parser('health')
    args=parser.parse_args(); base=args.url.rstrip('/')
    if not base.startswith(('http://127.0.0.1:','http://localhost:')): raise ValueError('Use a local AIAC API')
    if args.command=='create':
        manifest=json.loads(args.manifest.read_text(encoding='utf-8-sig'))
        if manifest.get('config',{}).get('execution_mode')=='bounded': raise ValueError('Bounded execution is retired')
        result=request(base+'/api/v1/tasks','POST',manifest)
    elif args.command=='start': result=request(base+f'/api/v1/tasks/{args.task_id}/start','POST',{})
    elif args.command in ('pause','resume','stop'):
        result=request(base+f'/api/v1/tasks/{args.task_id}/intervene','POST',{'action':args.command.upper()})
    elif args.command=='report':
        run=request(base+f'/api/v1/runs/{args.run_id}')
        if run.get('config_snapshot',{}).get('bounded_manifest'):
            result=request(base+f'/api/v1/runs/{args.run_id}/bounded')
        else:
            result={'run':run,'alphas':request(base+f'/api/v1/runs/{args.run_id}/alphas?limit=100'),
                    'trace':request(base+f'/api/v1/runs/{args.run_id}/trace')}
    else: result=request(base+'/api/v1/runs/bounded/health')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try: main()
    except urllib.error.HTTPError as exc:
        print(json.dumps({'status':exc.code,'error':exc.read().decode()[:1500]})); raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'detail':str(exc)[:400]})); raise SystemExit(1)
