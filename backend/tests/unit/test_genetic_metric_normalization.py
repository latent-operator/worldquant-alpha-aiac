"""Missing WQ metrics must not disable genetic exploration or imply OS evidence."""

import math

from backend.genetic_optimizer import GeneticOptimizer, Individual


def test_seed_accepts_null_and_nonfinite_metrics_without_fabricating_os_result():
    optimizer = GeneticOptimizer()
    optimizer.initialize("rank(news_field)", {
        "sharpe": 1.19,
        "fitness": 0.54,
        "turnover": None,
        "os_sharpe": None,
    })

    seed = optimizer.population.individuals[0]
    assert seed.overall_fitness >= 0
    assert seed.turnover is None
    assert seed.to_dict()["os_sharpe"] is None

    other = Individual(expression="rank(other)", sharpe=float("nan"), fitness="bad", turnover=None)
    other.calculate_fitness()
    assert math.isfinite(other.overall_fitness)


def test_simulation_metrics_are_normalized_before_genetic_scoring():
    optimizer = GeneticOptimizer()
    individual = Individual(expression="rank(news_field)")
    optimizer.update_individual(individual, {
        "alpha_id": "WQ-ID",
        "is": {"sharpe": "1.2", "fitness": None, "turnover": float("inf")},
        "os": {"sharpe": "not-a-number"},
    })

    assert individual.sharpe == 1.2
    assert individual.fitness is None
    assert individual.turnover is None
    assert individual.to_dict()["os_sharpe"] is None
    assert math.isfinite(individual.overall_fitness)
