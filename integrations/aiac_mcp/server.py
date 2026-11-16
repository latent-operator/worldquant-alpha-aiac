"""Thin read-only MCP for the current original AIAC API."""
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from client import AIACClient,public

api=AIACClient()
server=MCPServer(name='aiac-local',instructions='Read persisted AIAC facts. Completed runs and HTTP success do not prove quality PASS or final PPA readiness. Research execution remains on hold; this bridge exposes no writes.')
read=ToolAnnotations(readOnlyHint=True,openWorldHint=False)
def positive(value):
    if type(value) is not int or value<1:raise ValueError('Positive identity required')
    return value
def pagination(limit,offset):
    if not 1<=limit<=100 or offset<0:raise ValueError('Invalid pagination')

@server.tool(annotations=read)
async def aiac_list_tasks(status:str|None=None,limit:int=20,offset:int=0)->dict:
    pagination(limit,offset)
    return {'items':await api.tasks(status,limit,offset)}
@server.tool(annotations=read)
async def aiac_get_task(task_id:int)->dict:return await api.task(positive(task_id))
@server.tool(annotations=read)
async def aiac_list_runs(task_id:int)->dict:return {'items':await api.runs(positive(task_id))}
@server.tool(annotations=read)
async def aiac_get_run(run_id:int)->dict:return await api.run(positive(run_id))
@server.tool(annotations=read)
async def aiac_list_run_alphas(run_id:int,quality_status:str|None=None,limit:int=50,offset:int=0)->dict:
    pagination(limit,offset)
    return await api.alphas(positive(run_id),quality_status,limit,offset)
@server.tool(annotations=read)
async def aiac_get_alpha(alpha_id:int)->dict:return public(await api.get(f'/alphas/{positive(alpha_id)}'))
@server.tool(annotations=read)
async def aiac_list_trace(run_id:int,limit:int=30,offset:int=0)->dict:
    pagination(limit,offset)
    return await api.trace(positive(run_id),limit,offset)
@server.tool(annotations=read)
async def aiac_get_run_report(run_id:int)->dict:return await api.report(positive(run_id))

if __name__=='__main__':server.run()
