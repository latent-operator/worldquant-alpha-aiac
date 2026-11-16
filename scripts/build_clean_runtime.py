"""Build an auditable native runtime from exact upstream plus bounded changes.

Never modifies the pinned source checkout. The release manifest hashes every
Python/JS source file; the launcher verifies these before starting services.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from scripts.clean_install import UPSTREAM_COMMIT, validate_source

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path, before, after):
    value=path.read_text(encoding='utf-8')
    if value.count(before)!=1: raise ValueError('Pinned integration anchor changed: '+str(path.name))
    path.write_text(value.replace(before,after),encoding='utf-8')


def build(source: Path, target: Path):
    validate_source(source)
    if target.exists(): raise ValueError('Build destination must not exist')
    target.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as folder:
        archive=Path(folder)/'upstream.tar'
        subprocess.run(['git','-C',str(source),'archive',UPSTREAM_COMMIT,'-o',str(archive)],check=True)
        with tarfile.open(archive) as tar: tar.extractall(target,filter='data')
    shutil.copyfile(ROOT/'clean_runtime/bounded.py',target/'backend/services/bounded_research.py')
    service=target/'backend/services/task_service.py'
    replace_once(service,'        valid_start_statuses = ["PENDING", "PAUSED", "STOPPED", "FAILED", "COMPLETED"]',
                 '        from backend.services.bounded_research import start\n'
                 '        return await start(self.db, task_id)\n\n'
                 '        valid_start_statuses = ["PENDING", "PAUSED", "STOPPED", "FAILED", "COMPLETED"]')
    # Existing old generic start code remains unreachable; all native API/MCP starts share this gate.
    replace_once(service,'        action = action.upper()','        action = action.upper()\n'
                 '        if action == "RESUME":\n'
                 '            from backend.services.bounded_research import start\n'
                 '            return await start(self.db, task_id, resume=True)\n'
                 '        if action == "ADJUST":\n'
                 '            raise ValueError("Freeze a new task instead of changing a bounded run")')
    tasks=target/'backend/tasks/mining_tasks.py'
    replace_once(tasks,'    return run_async(_run())','    from backend.services.bounded_research import execute\n'
                 '    result = run_async(execute(task_id, run_id))\n'
                 '    if result.get("status") == "BUSY":\n'
                 '        raise self.retry(countdown=15, max_retries=None)\n'
                 '    return result')
    with tasks.open('a',encoding='utf-8') as f:
        f.write('\n\nfrom celery.signals import worker_ready\n'
                '@worker_ready.connect\ndef recover_bounded_runs(**kwargs):\n'
                '    from backend.services.bounded_research import recover\n'
                '    run_async(recover())\n')
    routes=target/'backend/routers/runs.py'
    replace_once(routes,'# =============================================================================\n# RESPONSE MODELS',
                 '@router.get("/bounded/health")\nasync def bounded_health():\n'
                 '    from backend.services.bounded_research import health\n    return await health()\n\n'
                 '# =============================================================================\n# RESPONSE MODELS')
    with routes.open('a',encoding='utf-8') as f:
        f.write('\n\n@router.get("/{run_id}/bounded")\n'
                'async def bounded_report(run_id: int, db: AsyncSession = Depends(get_db)):\n'
                '    from backend.services.bounded_research import report\n'
                '    try:\n        return await report(db, run_id)\n'
                '    except ValueError as exc:\n        raise HTTPException(404, str(exc))\n')
    # Show deterministic metrics even when a subjective score has not been generated.
    page=target/'frontend/src/pages/TaskDetail.jsx'
    replace_once(page,".filter(a => typeof a.metrics?._score === 'number')", ".filter(a => typeof a.metrics?.sharpe === 'number')")
    replace_once(page,'.sort((a, b) => (b.metrics?._score ?? -Infinity) - (a.metrics?._score ?? -Infinity))',
                 '.sort((a, b) => (b.metrics?.sharpe ?? -Infinity) - (a.metrics?.sharpe ?? -Infinity))')
    replace_once(page,'  const queryClient = useQueryClient()',
                 '  const { data: bounded } = useQuery({\n'
                 '    queryKey: ["boundedRun", selectedRunId],\n'
                 '    queryFn: async () => { const r = await fetch(`/api/v1/runs/${selectedRunId}/bounded`); if (!r.ok) throw new Error("Report unavailable"); return r.json() },\n'
                 '    enabled: !!selectedRunId, refetchInterval: task?.status === "RUNNING" ? 10000 : false,\n'
                 '  })\n\n  const queryClient = useQueryClient()')
    # Anchor before existing run summary, without introducing another application panel.
    replace_once(page,'  const runAlphaSummary = React.useMemo(() => {','  const runAlphaSummary = React.useMemo(() => {')
    # Insert an inline status block into the existing page layout after its loading guard.
    marker='<Card className="glass-card" title="本次 Run 摘要"'
    content=page.read_text(encoding='utf-8')
    if marker in content:
        # Wrap inside Card content using exact opening tag discovered from pinned source.
        start=content.index(marker); end=content.index('>',start)+1
        block='\n{bounded?.max_create_requests && <Space wrap style={{ marginBottom: 16 }}><Tag>请求 {bounded.sent}/{bounded.max_create_requests}</Tag><Tag>结果 {bounded.requests?.RESULT || 0}</Tag><Tag color="orange">未知 {bounded.requests?.UNKNOWN || 0}</Tag><Tag>后台心跳 {bounded.heartbeat?.worker_heartbeat || "等待执行"}</Tag>{bounded.error && <Text type="warning">{bounded.error}</Text>}</Space>}\n'
        page.write_text(content[:end]+block+content[end:],encoding='utf-8')
    else: raise ValueError('Pinned UI integration anchor changed')
    files={str(p.relative_to(target)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in target.rglob('*') if p.is_file() and p.suffix in ('.py','.jsx','.js','.json','.yaml','.yml') and 'dist' not in p.parts}
    manifest={'upstream_commit':UPSTREAM_COMMIT,'recipe_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
              'bounded_source_sha256':hashlib.sha256((ROOT/'clean_runtime/bounded.py').read_bytes()).hexdigest(),'files':files}
    (target/'bounded-build.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    # PLY attempts to rewrite a tracked upstream parser table at import time.
    if (target/'parsetab.py').exists(): (target/'parsetab.py').chmod(0o444)
    return manifest


def validate_build(source: Path):
    manifest=json.loads((source/'bounded-build.json').read_text(encoding='utf-8'))
    if manifest['upstream_commit']!=UPSTREAM_COMMIT: raise ValueError('Upstream build identity mismatch')
    recipe=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    if manifest['recipe_commit']!=recipe: raise ValueError('Build is not from this release commit')
    if manifest['bounded_source_sha256']!=hashlib.sha256((ROOT/'clean_runtime/bounded.py').read_bytes()).hexdigest():
        raise ValueError('Bounded release recipe mismatch')
    for name,expected in manifest['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected: raise ValueError('Changed runtime source: '+name)
    for name,expected in manifest.get('ui_files',{}).items():
        if hashlib.sha256((source/'frontend/dist'/name).read_bytes()).hexdigest()!=expected: raise ValueError('Changed UI artifact')
    return manifest


def seal_ui(source: Path):
    path=source/'bounded-build.json'
    manifest=json.loads(path.read_text(encoding='utf-8'))
    manifest['ui_files']={str(p.relative_to(source/'frontend/dist')).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (source/'frontend/dist').rglob('*') if p.is_file()}
    if 'index.html' not in manifest['ui_files']: raise ValueError('Built UI missing')
    path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True); parser.add_argument('--target',type=Path,required=True)
    args=parser.parse_args(); m=build(args.source.resolve(),args.target.resolve())
    print(json.dumps({'target':str(args.target),'upstream_commit':m['upstream_commit'],'source_files':len(m['files'])}))
