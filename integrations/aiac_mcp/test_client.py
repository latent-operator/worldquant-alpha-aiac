import unittest
import httpx
from integrations.aiac_mcp.client import AIACClient

class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_endpoints_and_report_do_not_invent_quality(self):
        calls=[]
        def handle(req):
            calls.append((req.method,req.url.path))
            responses={
              '/api/v1/runs/1':{'id':1,'task_id':1,'status':'COMPLETED','config_snapshot':{'secret':'private'}},
              '/api/v1/runs/1/trace':[{'id':2,'step_type':'SIMULATE','status':'SUCCESS','output_data':{'request_status':'RESULT'}}],
              '/api/v1/runs/1/alphas':{'total':1,'items':[{'id':3,'alpha_id':'EXAMPLE','quality_status':'FAIL','metrics':{'sharpe':2}}]},
              '/api/v1/tasks/1':{'id':1,'status':'PAUSED','config':{'secret':'private'}},
            }
            return httpx.Response(200,json=responses[req.url.path])
        client=AIACClient(transport=httpx.MockTransport(handle))
        self.assertNotIn('config',await client.task(1))
        report=await client.report(1)
        self.assertEqual(report['saved_wq_results'],1)
        self.assertEqual(report['quality_pass'],0)
        self.assertEqual(report['request_states'],{'RESULT':1})
        self.assertEqual(report['ppa_status'],'UNPROVEN')
        self.assertNotIn('config_snapshot',report['run'])
        self.assertTrue(all(m=='GET' for m,p in calls))
        self.assertFalse(any('summary' in p or 'research-report' in p for m,p in calls))

    async def test_http_error_is_not_empty_success(self):
        client=AIACClient(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
        with self.assertRaises(RuntimeError): await client.task(1)

    async def test_request_states_remain_distinct(self):
        def handle(req):
            if req.url.path.endswith('/trace'):
                return httpx.Response(200,json=[{'output_data':{'request_status':s}} for s in ('UNKNOWN','REQUESTED','NOT_REQUESTED')])
            if req.url.path.endswith('/alphas'):return httpx.Response(200,json={'items':[],'total':0})
            return httpx.Response(200,json={'id':2,'status':'PAUSED'})
        report=await AIACClient(transport=httpx.MockTransport(handle)).report(2)
        self.assertEqual(report['request_states'],{'UNKNOWN':1,'REQUESTED':1,'NOT_REQUESTED':1})
        self.assertEqual(report['saved_wq_results'],0)

if __name__=='__main__': unittest.main()
