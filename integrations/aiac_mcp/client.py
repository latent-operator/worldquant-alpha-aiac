"""Read-only compatibility bridge to the original AIAC API. No WQ calls."""
from collections import Counter
from datetime import datetime,timezone
import httpx

BASE='http://127.0.0.1:8003/api/v1'
def public(row):
    return {k:v for k,v in row.items() if k in ('id','task_id','run_id','task_name','status','region','universe','quality_status','alpha_id','expression','created_at','updated_at','started_at','finished_at','step_type','step_order','iteration','metrics','checks','is_metrics','os_metrics','settings')}

class AIACClient:
    def __init__(self,transport=None): self.transport=transport
    async def get(self,path,**params):
        async with httpx.AsyncClient(base_url=BASE,transport=self.transport,trust_env=False,timeout=20) as client:
            try: response=await client.get(path,params={k:v for k,v in params.items() if v is not None})
            except httpx.HTTPError: raise RuntimeError('Local AIAC unavailable; no request was retried') from None
        if response.status_code!=200: raise RuntimeError(f'Local AIAC HTTP {response.status_code}')
        return response.json()
    async def task(self,task_id): return public(await self.get(f'/tasks/{task_id}'))
    async def run(self,run_id): return public(await self.get(f'/runs/{run_id}'))
    async def tasks(self,status=None,limit=20,offset=0):
        return [public(x) for x in await self.get('/tasks',status=status,limit=limit,offset=offset)]
    async def runs(self,task_id): return [public(x) for x in await self.get(f'/tasks/{task_id}/runs')]
    async def alphas(self,run_id,quality_status=None,limit=50,offset=0):
        page=await self.get(f'/runs/{run_id}/alphas',quality_status=quality_status,limit=limit,offset=offset)
        return {'items':[public(x) for x in page['items']],'total':page['total']}
    async def trace(self,run_id,limit=30,offset=0):
        rows=await self.get(f'/runs/{run_id}/trace')
        return {'items':[public(x)|{'request_status':(x.get('output_data') or {}).get('request_status')} for x in rows[offset:offset+limit]],'total':len(rows)}
    async def report(self,run_id):
        run=await self.run(run_id)
        trace=await self.get(f'/runs/{run_id}/trace')
        alphas=[]
        while True:
            page=await self.alphas(run_id,limit=200,offset=len(alphas))
            alphas.extend(page['items'])
            if not page['items'] or len(alphas)>=page['total']:break
        results=[a for a in alphas if a.get('alpha_id')]
        return {'run':run,'run_id':run_id,'request_states':dict(Counter(x['output_data']['request_status'] for x in trace if (x.get('output_data') or {}).get('request_status'))),
                'saved_wq_results':len(results),'quality_pass':sum(a.get('quality_status')=='PASS' for a in results),
                'ppa_status':'UNPROVEN','alphas':results,'source':'native AIAC API','observed_at':datetime.now(timezone.utc).isoformat()}
