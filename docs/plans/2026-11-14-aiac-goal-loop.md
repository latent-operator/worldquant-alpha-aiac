# AIAC goal loop implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy OpenSpec and Beads locally so AIAC sessions keep one durable business goal and a verifiable task graph, while using RD-Agent's hypothesis–experiment–feedback loop as a design reference.

**Architecture:** Run pinned CLIs in a Docker tool container against this independent worktree. Store the acceptance contract in OpenSpec and outstanding work in Beads. A small offline gate consumes read-only AIAC result summaries and refuses to infer success from HTTP status, CI, or session completion.

**Tech Stack:** Docker Compose, OpenSpec CLI, Beads CLI, Python/pytest, existing AIAC DeepEval.

---

### Task 1: Deploy the tool container

**Files:** Create `tools/goal-loop/Dockerfile`, `compose.goal-tools.yaml`.

1. Pin CLI versions and build: `docker compose -f compose.goal-tools.yaml build`.
2. Check both versions in a fresh container.
3. Initialize OpenSpec and Beads without modifying existing agent instructions or Git hooks.

### Task 2: Freeze the goal and task graph

**Files:** Create `openspec/specs/aiac-research-goal/spec.md`, an OpenSpec change, and Beads issues.

1. Define evidence levels: unresolved request, result, persisted quality PASS, final PPA_READY, submission.
2. Require every research hypothesis to link to a frozen batch, experiment run, feedback and a next action.
3. Record the current zero-PASS and 45-UNKNOWN blockers as Beads issues, linked to the goal.
4. Validate OpenSpec; check Beads dependencies and ready queue after a new process starts.

### Task 3: Test the evidence gate

**Files:** Create `backend/tests/evals/test_goal_evidence.py`, `backend/evals/goal_evidence.py`, `scripts/goal_evidence.py`.

1. Write tests for zero PASS, pending UNKNOWN, persisted PASS, and an unsupported success claim.
2. Run tests and observe failure before implementation.
3. Implement the smallest deterministic gate, then run tests again.
4. Run the gate against an actual read-only AIAC report.

### Task 4: Verify and deliver

1. Run `python scripts/preflight.py` with no live model or WQ requests.
2. Review changed files for credentials and accidental production changes.
3. Commit, push, and collect CI evidence; report the exact commit and remaining business blocker.
