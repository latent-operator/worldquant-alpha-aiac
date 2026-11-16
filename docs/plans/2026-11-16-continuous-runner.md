# Continuous runner plan

Owner requests unlimited total research and maximum throughput; confirms WQ allowance of 10 concurrent simulations.

Reuse one native worker. Submit up to 10 single requests per frozen batch before waiting for results. Preserve pre-POST receipts, account serialization, historical exclusions and halt on uncertainty/rate limits. Total batches are unlimited, individual manifests stay bounded.

After persisted review, generate the next hypothesis/candidates with the existing LLM service and verified catalog fields, validate/freeze into another native run under the same task. Recover gaps on worker startup. Pause/stop must also stop continuation. Invalid planning or failed review pauses; no Alpha submission.

Local checks: concurrency/receipt order, duplicate delivery, partial submission, continuous next-run idempotency and owner intervention. Then deploy via ops/local.ps1 and start a new task with a different mechanism from the failed families. Confirm ten live accepted requests, return IDs without waiting for every future batch. No new scheduler or schema.
