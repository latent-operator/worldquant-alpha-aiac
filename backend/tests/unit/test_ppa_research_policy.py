from copy import deepcopy
import pytest
from backend.services import ppa_research_policy as policy


def candidate(amer=0.8, sharpe=0.9, fitness=0.4, test=0.3, case="REFINE_2"):
    return {"id": 1, "case_id": case, "expression": "rank(returns)",
            "dataset_id": "pv1", "mechanism_id": "reversal",
            "settings": {"decay": 4, "neutralization": "SUBINDUSTRY"},
            "metrics": {"amer_sharpe": amer, "sharpe": sharpe, "fitness": fitness,
                        "test_sharpe": test, "turnover": 0.2, "margin": 0.001,
                        "long_count": 100, "short_count": 100},
            "platform_ppa_evaluation": {"status": "PPA_PENDING", "reasons": ["POWER_POOL_CLASSIFICATION"]}}


@pytest.mark.parametrize("changes", [{"sharpe": -0.1}, {"fitness": -0.1},
                                     {"amer_sharpe": None}, {"test_sharpe": 0}])
def test_negative_or_incomplete_candidates_never_become_seeds(changes):
    row = candidate()
    row["metrics"].update(changes)
    assert not policy.seed_decision(row)["eligible"]


def test_amer_deterioration_cannot_be_offset_by_global_gains():
    assert not policy.effective_improvement(candidate(), candidate(amer=0.7, sharpe=1.5))
    assert policy.effective_improvement(candidate(), candidate(amer=0.86))
    assert not policy.effective_improvement(candidate(), candidate(amer=0.81))


def test_seed_order_prefers_amer_then_test():
    best = candidate(amer=1.1, case="discovery")
    assert policy.select_seeds([candidate(sharpe=3), best])[0]["case_id"] == "discovery"


@pytest.mark.parametrize("case", ["REFINE_1", "REFINE_2", "discovery"])
def test_every_winner_gets_two_distinct_confirmation_routes(case):
    parent = candidate(case=case)
    rows = policy.confirmation_cases(parent, {"decay": [2], "neutralization": ["INDUSTRY"]})
    assert len(rows) == 2
    assert {row["parent_case_id"] for row in rows} == {case}
    assert rows[0]["settings"] != rows[1]["settings"]
    assert all(row["expression"] == parent["expression"] for row in rows)


def test_confirmation_fails_closed_without_two_legal_perturbations():
    with pytest.raises(ValueError, match="TWO_PERTURBATIONS"):
        policy.confirmation_cases(candidate(), {"decay": [4]})


def test_four_failed_improvements_stop_family():
    outcomes = [{"mechanism_id": "reversal", "effective_improvement": False}] * 4
    assert "reversal" in policy.stopped_families(outcomes)
    assert "reversal" not in policy.stopped_families(outcomes[:3])
