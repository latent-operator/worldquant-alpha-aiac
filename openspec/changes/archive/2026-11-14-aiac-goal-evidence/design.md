# Design

The Docker tool image pins OpenSpec and Beads. OpenSpec holds the stable goal and change rationale in Git. Beads holds issue dependencies and resumable handoff state in `.beads/`. A small Python gate reads the existing lab report shape and emits an evidence status. It does not infer final PPA readiness from preliminary platform checks.

The gate is intentionally narrower than the WQ runtime: it evaluates existing reports and cannot send requests. Existing AIAC ResearchLedger and DeepEval metrics remain the source of request and quality facts. Each future batch should attach hypothesis, frozen manifest hash, task/run/candidate/WQ Alpha identifiers, result and feedback to a Beads issue.
