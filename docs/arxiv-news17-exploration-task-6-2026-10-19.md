# arXiv:2610.04094 follow-up exploration — AIAC task 6

Task 6 was created specifically for the `news17` Delay 1 transfer experiment and completed four iterations in run 8. Task 4 ("0") and its run 6 were stopped before task 6 started. Task 3 stayed paused. No Alpha was submitted.

The paper compares repeated and unique text across earnings press releases and conference calls. `nws17_d1_event_similarity_days` is only a recurrence-timing proxy; these simulations cannot reproduce the paper's text novelty result. The matched sentiment-only control uses `nws17_d1_event_sentiment_score`.

| Window | Role | WQ Alpha ID | Sharpe | Fitness | Turnover | Quality |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 5 days | Sentiment control | `2rjLnzMN` | -0.10 | -0.01 | 0.3677 | FAIL |
| 5 days | Recurrence-conditioned sentiment | `O0JZ5POg` | -0.09 | -0.01 | 0.4105 | FAIL |
| 10 days | Sentiment control | `omol31lJ` | -0.24 | -0.05 | 0.2176 | FAIL |
| 10 days | Recurrence-conditioned sentiment | `ZYmn2RKY` | -0.19 | -0.03 | 0.2368 | FAIL |

All four use USA/TOP3000, Delay 1, `news17`, and the same default simulation settings. The 5-day expressions use `ts_mean(...,5)` and the 10-day expressions use `ts_mean(...,10)` around the original control and interaction signals. The 10-day window reduced turnover below 30%, but both 10-day signals had negative Sharpe. The proxy marginally outperformed its matched control in both windows, without passing quality checks.

During iteration 1, AIAC's structural deduplication misclassified the 10-day window as identical to the 5-day window. An opt-in `allow_parameter_variants` rule corrected this, and the same run resumed from iteration 1. Iteration 2 then validated and simulated both 10-day expressions. Before the field restriction was loaded, iteration 2 also simulated two LLM-generated `nws17_acb` expressions unrelated to the paper. Both failed quality checks and are excluded from the table and paper conclusion. `research_only_fields` and `research_required_field` restricted iterations 3 and 4 to the two mapped fields. No eligible new candidates were produced in those last two iterations, so no further WQ simulations occurred.

Across the original run 7 and follow-up run 8, three matched pairs were completed with zero quality passes. This rejects the tested WQ proxy constructions under the current settings; it does not test or falsify the paper's original paired-text measure.
