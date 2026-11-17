"""Offline interface checks for the operator skill; no WQ, LLM or production writes.

These test the existing CLI contract used by the skill, not model instruction adherence.
See docs/plans/2026-11-17-aiac-operator-validation.md for the decision walkthrough.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.update(BRAIN_EMAIL='', BRAIN_PASSWORD='', OPENAI_API_KEY='offline',
                  OPENAI_BASE_URL='http://127.0.0.1:1/v1', REDIS_URL='redis://127.0.0.1:1/0')
from ops import research
from backend.routers.tasks import TaskCreateRequest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / '.agents/skills/aiac-operator/references/task-example.json'


class OperatorInterfaceTests(unittest.TestCase):
    def call(self, argv, responses):
        out = io.StringIO()
        with patch.object(sys, 'argv', ['research.py', *argv]), \
             patch.object(research, 'request', side_effect=responses) as request, \
             contextlib.redirect_stdout(out):
            research.main()
        return request.call_args_list, json.loads(out.getvalue())

    def test_example_matches_native_schema_and_preserves_research_context(self):
        payload = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        model = TaskCreateRequest.model_validate(payload)
        self.assertEqual(model.config, payload['config'])
        self.assertEqual(model.max_iterations, 10)
        self.assertEqual(model.config['simulation_batch_size'], 4)

    def test_create_and_start_are_separate_single_requests(self):
        calls, result = self.call(['create','--manifest',str(EXAMPLE)], [{'id':90}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[:2], ('http://127.0.0.1:8003/api/v1/tasks','POST'))
        self.assertEqual(result['id'], 90)
        calls, result = self.call(['start','--task-id','90'], [{'run_id':91,'celery_task_id':'offline'}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, ('http://127.0.0.1:8003/api/v1/tasks/90/start','POST',{}))
        self.assertEqual(result['run_id'], 91)

    def test_native_report_is_read_only_and_keeps_failed_quality(self):
        run = {'id':91,'status':'COMPLETED','config_snapshot':{'task':{'config':{}}}}
        alphas = {'items':[{'alpha_id':'offline','quality_status':'FAIL','metrics':{'sharpe':-.2}}], 'total':1}
        trace = [{'step_type':'ROUND_SUMMARY','output_data':{'passed_count':0}}]
        calls, result = self.call(['report','--run-id','91'], [run,alphas,trace])
        self.assertEqual(result, {'run':run,'alphas':alphas,'trace':trace})
        self.assertTrue(all(len(c.args)==1 for c in calls))

    def test_native_report_exposes_total_for_required_manual_pagination(self):
        run = {'id':91,'config_snapshot':{}}
        calls, result = self.call(['report','--run-id','91'], [run,{'items':[],'total':145},[]])
        self.assertEqual(result['alphas']['total'],145)
        self.assertIn('limit=100',calls[1].args[0])

    def test_retired_report_is_read_only(self):
        run = {'id':5,'config_snapshot':{'bounded_manifest':{'cases':[]}}}
        receipt = {'run_id':5,'requests':{'RESULT':10},'quality_PASS':0}
        calls, result = self.call(['report','--run-id','5'], [run,receipt])
        self.assertEqual(result, receipt)
        self.assertEqual(calls[1].args, ('http://127.0.0.1:8003/api/v1/runs/5/bounded',))

    def test_start_timeout_does_not_retry(self):
        with patch.object(sys,'argv',['research.py','start','--task-id','90']), \
             patch.object(research,'request',side_effect=TimeoutError('unknown response')) as request:
            with self.assertRaises(TimeoutError): research.main()
        self.assertEqual(request.call_count,1)

    def test_retired_create_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'old.json'
            path.write_text(json.dumps({'config':{'execution_mode':'bounded'}}))
            with patch.object(sys,'argv',['research.py','create','--manifest',str(path)]), \
                 patch.object(research,'request') as request:
                with self.assertRaisesRegex(ValueError,'retired'): research.main()
            request.assert_not_called()


if __name__=='__main__': unittest.main()
