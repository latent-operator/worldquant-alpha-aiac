# arXiv news17 transfer experiment — 2026-10-19

## Source and mapping

- [arXiv:2610.04094](https://arxiv.org/abs/2610.04094) studies repeated and unique content across earnings press releases and conference calls. The WQ `news17` catalog lacks the paired texts and the paper's optimal-transport novelty score. `nws17_d1_event_similarity_days` is only a recurrence-timing proxy.
- [arXiv:2609.20198](https://arxiv.org/abs/2609.20198) reports that sentiment measures in its study were not significantly associated with next-day stock returns. A sentiment-only expression is therefore a control, not a positive paper claim.
- The newer [arXiv:2610.19087](https://arxiv.org/abs/2610.19087) needs earnings-call Q&A passiveness. No matching field was found in the currently synced `news17` catalog, so no executable candidate was imported from it.

## AIAC execution

- AIAC task 5, run 7, completed; USA / TOP3000 / Delay 1 / `news17`; one iteration.
- The RAG trace retrieved both imported family IDs. Code generation produced the two expressions below. Validation accepted both. WQ returned two Alpha IDs. No Alpha was submitted.

| Role | Expression | WQ Alpha ID | Sharpe | Fitness | Turnover | Quality |
| --- | --- | --- | ---: | ---: | ---: | --- |
| Sentiment control | `vec_avg(nws17_d1_event_sentiment_score)` | `kqJ0Xled` | 0.06 | 0.00 | 1.0233 | FAIL |
| Paper transfer proxy | `vec_avg(nws17_d1_event_sentiment_score)*rank(vec_avg(nws17_d1_event_similarity_days))` | `O0JZkaK7` | 0.19 | 0.02 | 1.1970 | FAIL |

The proxy's Sharpe was higher than the control's in this matched run, but neither passed quality checks. The proxy has high turnover and does not reproduce the paper's text-novelty construction. One batch is insufficient to establish a robust effect. Treat both expressions as explored failures, not reusable success patterns.

Task 4 was paused after its current round to free the sole AIAC worker, then resumed on existing run 6 after run 7 completed. Task 3 and the old loop were not started.
