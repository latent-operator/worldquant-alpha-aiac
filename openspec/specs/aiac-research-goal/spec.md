# AIAC research goal

## Purpose

Keep the current WQ research objective stable across sessions, code changes and experiments. The durable goal ID is `AIAC-PPA-QUALIFIED-ALPHA`. The current stage seeks a candidate with persisted AIAC quality `PASS` and final `PPA_READY` under the active theme. Alpha submission and production release remain separate decisions.

## Requirements

### Requirement: Business outcome evidence
AIAC SHALL report goal progress from persisted candidate quality and final PPA evaluation, with the candidate ID, WQ Alpha ID, task/run ID, frozen batch and code commit when known.

#### Scenario: Result without quality pass
- **WHEN** a simulation returns a result but persisted `quality_status` is not `PASS`
- **THEN** the goal remains unproven, regardless of Sharpe, HTTP status, CI success or AgentsView session completion.

#### Scenario: Quality pass without final PPA readiness
- **WHEN** a candidate has persisted quality `PASS` but final `PPA_READY` is absent
- **THEN** AIAC records a quality observation and keeps the PPA goal open.

#### Scenario: Final PPA readiness
- **WHEN** a candidate has persisted quality `PASS` and final `PPA_READY`, both traceable to the same candidate and WQ Alpha
- **THEN** AIAC records the goal observation and pauses for review before any submission.

### Requirement: Request uncertainty
AIAC SHALL preserve unresolved request positions and report evidence as insufficient while a compared experiment contains `UNKNOWN`, `REQUESTED` or `RESERVED` requests.

#### Scenario: Unresolved experiment
- **WHEN** an experiment has a result subset and unresolved requests
- **THEN** it cannot claim a completed comparison or a business outcome improvement.

### Requirement: Hypothesis to feedback loop
Each new research batch SHALL record an economic hypothesis, frozen candidate and settings signatures, experiment run, WQ outcome, feedback and a next action. The loop structure is inspired by RD-Agent; AIAC's WQ ledger is the authority for outcomes.

#### Scenario: Repeated weak family
- **WHEN** three iterations of one signal family produce no persisted quality `PASS`
- **THEN** the next plan documents a mechanism change, a new dataset or a stop decision before further requests.

### Requirement: Cross-session handoff
New work SHALL link an OpenSpec goal requirement to a Beads issue and record its expected business evidence. A completed code task SHALL NOT close the research goal without the business evidence above.

#### Scenario: CI succeeds but research does not
- **WHEN** an implementation passes CI and the WQ ledger still has zero quality `PASS`
- **THEN** the implementation issue may close, while `AIAC-PPA-QUALIFIED-ALPHA` remains open.

### Requirement: Validated goal closure
The common local workflow SHALL reject research goal closure without reconciled complete lineage, explicit request states, unique candidate/Alpha identities and a manifest reference. It SHALL record the report hash, code commit and session reference before closing the goal.

#### Scenario: Contradictory report
- **WHEN** request summary counts disagree with complete lineage or declared request status counts
- **THEN** acceptance fails and no goal-close command is issued.

#### Scenario: Cross-worktree recovery
- **WHEN** an AIAC session starts in another local worktree
- **THEN** the shared workspace entry loads the goal specification, Beads memories and ready queue.
