"""Replay outputs frozen from the pre-extraction implementation, without WQ."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIXTURE = json.loads((Path(__file__).parents[1] / 'fixtures/quality_policy_golden.json').read_text(encoding='utf-8'))


class ReplayBrain:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def check_correlation(self, alpha_id, check_type):
        self.calls.append(check_type)
        reply = self.replies[check_type]
        if isinstance(reply, dict) and 'raise' in reply:
            raise RuntimeError(reply['raise'])
        return deepcopy(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['case_id'])
async def test_quality_replay_preserves_status_mutation_and_network_order(case):
    from backend.services.alpha_quality_evaluation import evaluate_alpha_quality
    brain = ReplayBrain(case['replies'])
    metrics = deepcopy(case['metrics'])
    status = await evaluate_alpha_quality(brain=brain, task=SimpleNamespace(config=case['config']),
        expression=case['expression'], metrics=metrics, fields=case['fields'], alpha_id=case['alpha_id'])
    assert {'status': status, 'metrics': metrics, 'calls': brain.calls} == case['expected']


@pytest.mark.asyncio
async def test_mining_agent_wrapper_matches_public_evaluator_without_construction(monkeypatch):
    from backend.agents.mining_agent import MiningAgent
    from backend.services import alpha_quality_evaluation
    called = []

    async def evaluate(**kwargs):
        called.append(kwargs)
        return 'OPTIMIZE'

    monkeypatch.setattr(alpha_quality_evaluation, 'evaluate_alpha_quality', evaluate)
    brain = object()
    task = SimpleNamespace(config={})
    result = await MiningAgent._quality_status_for_optimization_result(SimpleNamespace(brain=brain),
        task, 'rank(sample_field)', {}, [], 'synthetic-alpha')
    assert result == 'OPTIMIZE'
    assert called[0]['brain'] is brain


def test_pure_quality_policy_does_not_mutate_input_metrics():
    from backend.services.alpha_quality_policy import ExpressionFacts, local_quality_decision, optimization_thresholds
    metrics = deepcopy(FIXTURE['cases'][0]['metrics'])
    before = deepcopy(metrics)
    decision = local_quality_decision(metrics, {'failed_checks': []}, optimization_thresholds({}),
                                      ExpressionFacts(('sample_field',), ('rank',), 1), 'synthetic-alpha')
    assert decision is None
    assert metrics == before


def test_graph_compatibility_exports_use_shared_rules(monkeypatch):
    from backend.agents.graph.nodes import evaluation
    from backend.services import alpha_quality_evaluation, alpha_quality_policy
    seen = []
    def shared_gate(*args):
        seen.append(args[-1])
        return ['SHARED_GATE']
    monkeypatch.setattr(alpha_quality_evaluation, 'policy_gate_failures', shared_gate)
    assert evaluation._strict_gate_failures({}, [], None, None, {},
        expression='rank(sample_field)', fields=[{'id': 'sample_field'}]) == ['SHARED_GATE']
    assert seen[0].operator_count == 1
    assert evaluation._risk_neutralized_metrics is alpha_quality_policy.risk_neutralized_metrics
    assert evaluation._operator_call_count is alpha_quality_policy.operator_call_count
