import asyncio
import importlib.util
import unittest

import httpx


def implementation():
    spec = importlib.util.find_spec('clean_runtime.bounded')
    assert spec is not None, 'Bounded native execution is not implemented'
    from clean_runtime import bounded
    return bounded


def config():
    return {'execution_mode': 'bounded', 'hypothesis': 'A frozen economic mechanism',
            'fields': ['close'], 'max_create_requests': 2,
            'candidates': [{'id': 'a', 'expression': 'rank(close)', 'decay': 4},
                           {'id': 'b', 'expression': 'rank(close)', 'decay': 8}]}


class ContractTests(unittest.TestCase):
    def test_budget_must_cover_frozen_candidates(self):
        b = implementation(); c = config(); c['max_create_requests'] = 1
        with self.assertRaises(ValueError): b.freeze(c, 'USA', 'TOP3000', set())

    def test_history_exclusion_ignores_whitespace(self):
        b = implementation()
        with self.assertRaises(ValueError): b.freeze(config(), 'USA', 'TOP3000', {'rank(close)'})

    def test_settings_are_part_of_signature(self):
        b = implementation(); batch = b.freeze(config(), 'USA', 'TOP3000', set())
        self.assertNotEqual(batch['cases'][0]['signature'], batch['cases'][1]['signature'])
        self.assertEqual(batch['simulation_mode'], 'single')

    def test_duplicate_cases_blocked(self):
        b = implementation(); c = config(); c['candidates'][1]['decay'] = 4
        with self.assertRaises(ValueError): b.freeze(c, 'USA', 'TOP3000', set())

    def test_no_unknown_location_requests(self):
        b = implementation()
        for url in ['https://example.org/simulations/a', 'http://api.worldquantbrain.com/simulations/a',
                    'https://api.worldquantbrain.com/alphas/a', 'https://user@api.worldquantbrain.com/simulations/a']:
            with self.assertRaises(ValueError): b.simulation_url(url)

    def test_http_success_is_not_quality_pass(self):
        b = implementation()
        self.assertEqual(b.quality({'id': 'a', 'is': {'sharpe': 2}})[0], 'PENDING')
        self.assertEqual(b.quality({'id': 'a', 'is': {'sharpe': .29, 'fitness': .09}})[0], 'FAIL')

    def test_nonfinite_special_metrics_never_pass(self):
        b=implementation()
        alpha={'settings':{'region':'GLB'},'stage':'IS','status':'UNSUBMITTED','regular':{'operatorCount':float('nan')},
               'is':{'sharpe':2,'fitness':2,'margin':.002,'turnover':.1,'riskNeutralized':{'sharpe':2,'fitness':2},
                     'glbAmer':{'sharpe':float('nan')},'checks':[{'name':'LOW_2Y_SHARPE','value':2,'result':'PASS'},
                         {'name':'SELF_CORRELATION','value':float('nan'),'result':'PASS'},
                         {'name':'PROD_CORRELATION','value':float('nan'),'result':'PASS'}]}}
        self.assertNotEqual(b.quality(alpha)[0],'PASS')


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_post_is_never_retried(self):
        b = implementation(); requests = []
        def transport(r):
            requests.append(r); raise httpx.ReadTimeout('test only')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await b.post_once(client, {'regular': 'rank(close)'})
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['request_status'], 'UNKNOWN')

    async def test_redirect_is_not_reposted(self):
        b = implementation(); calls = []
        def transport(r):
            calls.append(r); return httpx.Response(307, headers={'location': '/simulations/other'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            result = await b.post_once(client, {})
        self.assertEqual(len(calls), 1); self.assertEqual(result['request_status'], 'UNKNOWN')

    async def test_rate_limit_stops_without_retry(self):
        b = implementation(); calls = []
        def transport(r):
            calls.append(r); return httpx.Response(429, headers={'retry-after': '10'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await b.post_once(client, {})
        self.assertEqual(len(calls), 1); self.assertEqual(result['http_status'], 429)
        self.assertEqual(result['request_status'], 'REJECTED')

    async def test_single_request_is_object_not_batch_array(self):
        b = implementation(); bodies = []
        def transport(r):
            bodies.append(r.content); return httpx.Response(201, headers={'location': b.BASE + '/simulations/one'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await b.post_once(client, {'type': 'REGULAR', 'regular': 'rank(close)'})
        self.assertTrue(bodies[0].startswith(b'{')); self.assertEqual(result['request_status'], 'ACCEPTED')


if __name__ == '__main__': unittest.main()
