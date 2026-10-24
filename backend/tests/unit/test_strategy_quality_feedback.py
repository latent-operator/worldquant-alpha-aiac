from types import SimpleNamespace

import pytest

from backend.agents.strategy_agent import StrategyAgent


def _alpha(expression, status):
    return SimpleNamespace(expression=expression, quality_status=status, metrics={"sharpe": 0.6})


def test_saved_quality_failure_is_not_a_second_attempt_or_success():
    agent = StrategyAgent(llm_service=object())
    metrics = agent.compute_round_metrics(
        [_alpha("rank(news17)", "FAIL"), _alpha("rank(news18)", "OPTIMIZE")],
        [{"expression": "rank(news17)", "error_type": "QUALITY_CHECK_FAILED"}],
    )
    assert metrics.total_alphas == 2
    assert metrics.simulated_alphas == 2
    assert metrics.passed_alphas == 0
    assert metrics.optimize_alphas == 1
    assert metrics.success_rate == 0
    assert "收敛" not in agent._fallback_strategy(metrics, 2, 0, 4).action_summary


@pytest.mark.asyncio
async def test_llm_convergence_claim_is_rejected_without_pass():
    class LLM:
        async def call(self, **kwargs):
            return SimpleNamespace(success=True, parsed={"next_strategy": {
                "action_summary": "成功率良好，强化成功模式",
                "exploration_weight": 0.1,
            }})

    strategy = await StrategyAgent(llm_service=LLM()).generate_strategy(
        iteration=2, max_iterations=10, alphas=[_alpha("rank(news17)", "OPTIMIZE")],
        failures=[], dataset_id="news17", region="USA", cumulative_success=0, target_goal=4,
    )
    assert "成功率良好" not in strategy.action_summary
    assert strategy.exploration_weight >= 0.5
