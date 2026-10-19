# WQ Loop Research Context Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Freeze the original local WQ loop and make its provenance-tracked research family hypotheses available to future AIAC generations without copying credentials or treating unpassed ideas as validated patterns.

**Architecture:** Keep the source loop paused and unchanged. Store a curated, auditable migration manifest in AIAC, then have category- and delay-aware RAG retrieval expose only compatible family hypotheses to the hypothesis and expression prompts. Preserve zero-pass evidence and require generated formulas to use only fields present in the active AIAC dataset.

**Tech Stack:** Python, Pydantic/LangGraph state, SQLAlchemy-backed RAG service, JSON research manifest.

---

### Task 1: Record the migration contract and source evidence

**Files:**
- Create: `docs/plans/2026-10-17-wq-loop-to-aiac.md`
- Create: `data/research_migrations/wq-local-family-plans-2026-10-17.json`

**Steps:**
1. Record that the source loop is PAUSED, shadow mode, auto-submit disabled, with no active or in-flight work.
2. Capture the four current research families, provenance, field constraints, and candidate/complete/pass counts.
3. Mark each family as an unvalidated hypothesis; record the stale family-plan hash mismatch and use the current round ledger as source.
4. Keep account credentials, API keys, raw alpha history, and submission capability out of the manifest.

### Task 2: Add category- and delay-aware family-plan retrieval

**Files:**
- Modify: `backend/agents/services/rag_service.py`
- Modify: `backend/agents/graph/state.py`
- Modify: `backend/agents/graph/nodes/generation.py`

**Steps:**
1. Load the curated manifest from the AIAC repository path and select entries only when dataset category and requested delay are compatible.
2. Preserve source evidence and confidence labels in RAG results and graph state.
3. Do not treat family hypotheses as success patterns or update learned-success counters.

### Task 3: Pass the hypotheses and guardrails into future prompts

**Files:**
- Modify: `backend/agents/graph/nodes/generation.py`
- Modify: `backend/agents/prompts/base.py`
- Modify: `backend/agents/prompts/hypothesis.py`
- Modify: `backend/agents/prompts/generation.py`

**Steps:**
1. Add a concise research-family context section to hypothesis and code-generation prompts.
2. State that source plans have zero passing candidates and are exploration seeds only.
3. Require exact use of fields listed by the current dataset; prohibit guessed or cross-category fields.
4. Preserve existing simulation and evaluation gates as the authority for candidate status.

### Task 4: Verify the frozen source and review the migration diff

**Files:**
- Review: AIAC files listed above
- Review: `F:\wq-capability-loop` status only; do not modify its source or runtime state

**Steps:**
1. Confirm the original loop remains paused with zero active/in-flight work and auto-submit disabled.
2. Inspect AIAC changes and perform syntax/import-level checks without starting a new mining task or restarting the running worker.
3. Report which source families are eligible for the active category and which remain excluded due to category mismatch or unavailable fields.
