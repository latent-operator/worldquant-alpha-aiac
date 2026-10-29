"""Audited candidate requests and a fixed news17 paper-ablation matrix.

The reservation commits before network I/O. An uncertain response consumes the
reservation and is never submitted again under the same run/signature.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any

from sqlalchemy import func, select

from backend.database import AsyncSessionLocal
from backend.models import Alpha, ExperimentRun, MiningTask, ResearchCandidate


FAMILY_ID = "arxiv_2512_19484_news17_structured_events_v1"
SENTIMENT = "nws17_d1_event_sentiment_score"
CONDITION_FIELDS = (
    ("event_relevance", "nws17_d1_event_relevance"),
    ("entity_relevance", "nws17_d1_relevance"),
    ("similarity_days", "nws17_d1_event_similarity_days"),
    ("event_density", "nws17_d1_rp_story_event_count"),
)
WINDOWS = (5, 10, 20, 30, 60)


def paper_matrix() -> list[dict[str, Any]]:
    """Five shared sentiment controls plus 20 vendor-field proxy treatments."""
    result = []
    for window in WINDOWS:
        base = f"rank(vec_avg({SENTIMENT}))"
        result.append({
            "arm": "CONTROL", "group": "event_sentiment", "window": window,
            "pair_id": f"window_{window}",
            "expression": f"ts_mean({base}, {window})",
        })
        for group, field in CONDITION_FIELDS:
            result.append({
                "arm": "TREATMENT", "group": group, "window": window,
                "pair_id": f"window_{window}",
                "expression": f"ts_mean({base} * rank(vec_avg({field})), {window})",
            })
    return result


def _complete_metrics(row: dict) -> bool:
    metrics = row.get("metrics") or {}
    try:
        return all(metrics.get(key) is not None and math.isfinite(float(metrics[key]))
                   for key in ("sharpe", "fitness", "turnover", "margin"))
    except (TypeError, ValueError, OverflowError):
        return False


def paired_improvements(rows: list[dict]) -> list[dict]:
    """Only treatments with complete metrics and both positive paired gains seed GA."""
    controls = {row.get("pair_id"): row for row in rows
                if row.get("arm") == "CONTROL" and _complete_metrics(row)}
    improvements = []
    for row in rows:
        if row.get("arm") not in {"TREATMENT", "GENETIC"} or not _complete_metrics(row):
            continue
        control = controls.get(row.get("pair_id"))
        if control is None:
            continue
        metrics, baseline = row["metrics"], control["metrics"]
        sharpe_delta = float(metrics["sharpe"]) - float(baseline["sharpe"])
        fitness_delta = float(metrics["fitness"]) - float(baseline["fitness"])
        if sharpe_delta > 0 and fitness_delta > 0:
            improvements.append({**row, "sharpe_delta": sharpe_delta,
                                 "fitness_delta": fitness_delta})
    return sorted(improvements, key=lambda row: (row["sharpe_delta"], row["fitness_delta"]), reverse=True)


def constrained_mutations(seeds: list[dict], limit: int = 15) -> list[dict]:
    """Generate bounded grammar mutations from paired seeds, using approved fields only."""
    generated: list[dict] = []
    seen = {case["expression"] for case in paper_matrix()}
    fields = dict(CONDITION_FIELDS)
    for seed in seeds:
        group = seed.get("group")
        field = fields.get(group)
        if not field:
            continue
        window = int(seed.get("window") or str(seed.get("pair_id", "0")).split("_")[-1])
        core = f"rank(vec_avg({SENTIMENT})) * rank(vec_avg({field}))"
        for operator, candidate_window in (
            ("ts_mean", window + 2), ("ts_mean", window + 5),
            ("ts_rank", window), ("ts_sum", window),
        ):
            if candidate_window <= 0 or candidate_window > 120:
                continue
            expression = f"{operator}({core}, {candidate_window})"
            if expression in seen:
                continue
            seen.add(expression)
            generated.append({
                "arm": "GENETIC", "group": group, "window": candidate_window,
                "pair_id": seed["pair_id"], "parent_candidate_id": seed.get("id"),
                "expression": expression,
            })
            if len(generated) >= limit:
                return generated
    return generated


def signature(expression: str, settings: dict) -> tuple[str, str]:
    return (
        sha256(expression.strip().encode()).hexdigest(),
        sha256(json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
    )


def wq_settings_identity(settings: dict, *, region=None, universe=None) -> tuple:
    """Only settings that affect the WQ simulation belong in cross-task dedup."""
    def value(key, default):
        actual = settings.get(key)
        return default if actual is None else actual

    return (
        str(settings.get("region") or region or "").upper(),
        str(settings.get("universe") or universe or "").upper(),
        int(value("delay", 1)),
        int(value("decay", 4)),
        str(value("neutralization", "SUBINDUSTRY")).upper(),
        round(float(value("truncation", 0.08)), 6),
        str(value("test_period", "P2Y0M")),
    )


def safe_metrics(result: dict) -> dict:
    metrics = result.get("metrics") or {}
    def numeric(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError, OverflowError):
            return None

    safe = {key: numeric(metrics.get(key)) for key in (
        "sharpe", "fitness", "turnover", "margin", "returns", "two_year_sharpe",
        "os_sharpe", "os_fitness", "train_sharpe", "train_fitness",
        "test_sharpe", "test_fitness", "long_count", "short_count",
        "amer_sharpe",
    ) if key in metrics}
    for group in ("riskNeutralized", "investabilityConstrained"):
        nested = metrics.get(group) or {}
        if isinstance(nested, dict):
            safe[group] = {key: numeric(nested.get(key)) for key in ("sharpe", "fitness", "turnover")
                           if key in nested}
    return safe


def safe_result(result: dict) -> dict:
    """Enough WQ evidence to finish local persistence after a worker restart."""
    http_status = result.get("http_status")
    if (isinstance(http_status, bool) or not isinstance(http_status, int)
            or not 100 <= http_status <= 599):
        http_status = None
    try:
        retry_after = float(result.get("retry_after"))
        if not math.isfinite(retry_after) or retry_after < 0:
            retry_after = None
    except (TypeError, ValueError, OverflowError):
        retry_after = None

    def identifier(value):
        text = str(value or "")
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", text)
                or text.lower().startswith(("sk-", "bearer", "eyj"))):
            return None
        return text

    classifications = []
    for item in (result.get("classifications") or [])[:20]:
        if not isinstance(item, dict):
            continue
        identity = identifier(item.get("id"))
        name, value = identifier(item.get("name")), identifier(item.get("value"))
        if identity or (name and value and name.upper() in {"POWER_POOL", "REGULAR", "ATOM"}):
            classifications.append({key: content for key, content in
                                    {"id": identity, "name": name, "value": value}.items()
                                    if content})
    themes = []
    for item in (result.get("themes") or [])[:20]:
        if isinstance(item, dict):
            theme_id = identifier(item.get("id"))
            if theme_id:
                themes.append({"id": theme_id})
    checks = []
    for item in (result.get("checks") or [])[:40]:
        if isinstance(item, dict):
            checks.append({
                "name": str(item.get("name") or "")[:60] if re.fullmatch(r"[A-Za-z0-9_]{1,60}", str(item.get("name") or "")) else "UNKNOWN",
                "result": str(item.get("result") or "")[:20] if re.fullmatch(r"[A-Za-z_]{1,20}", str(item.get("result") or "")) else "UNKNOWN",
                "value": safe_metrics({"metrics": {"sharpe": item.get("value")}}).get("sharpe"),
                "themes": [{"id": theme_id} for theme in (item.get("themes") or [])[:20]
                           if isinstance(theme, dict)
                           for theme_id in [identifier(theme.get("id"))] if theme_id],
            })
    os_data = result.get("os") or {}
    return {
        "success": bool(result.get("success")), "alpha_id": result.get("alpha_id"),
        "http_status": http_status, "retry_after": retry_after,
        "stage": result.get("stage") if result.get("stage") in {"IS", "OS"} else None,
        "status": result.get("status") if result.get("status") in {"UNSUBMITTED", "SUBMITTED"} else None,
        "classifications": classifications, "themes": themes,
        "metrics": safe_metrics(result), "checks": checks,
        "can_submit": bool(result.get("can_submit")),
        "platform_can_submit": result.get("platform_can_submit") is True,
        "os": {key: os_data.get(key) for key in ("sharpe", "fitness")
               if isinstance(os_data.get(key), (int, float, type(None)))},
    }


class ResearchLedger:
    def __init__(self, task_id: int, run_id: int, max_requests: int | None,
                 family_id: str = FAMILY_ID, dedup_task_ids: list[int] | None = None):
        self.task_id = task_id
        self.run_id = run_id
        self.max_requests = max_requests
        self.family_id = family_id
        self.dedup_task_ids = sorted({int(task_id), *(int(value) for value in (dedup_task_ids or []))})

    async def reserve(self, expression: str, settings: dict, source: str,
                      metadata: dict | None = None) -> tuple[int | None, str | None]:
        """Lock the run and commit exactly one durable request slot."""
        metadata = metadata or {}
        expr_hash, settings_hash = signature(expression, settings)
        from backend.alpha_semantic_validator import compute_expression_hash
        normalized_alpha_hash = compute_expression_hash(expression)
        async with AsyncSessionLocal() as db:
            async with db.begin():
                run = (await db.execute(
                    select(ExperimentRun).where(ExperimentRun.id == self.run_id).with_for_update()
                )).scalar_one()
                if run.task_id != self.task_id:
                    raise ValueError("Research run does not belong to task")
                task = await db.get(MiningTask, self.task_id)
                existing = (await db.execute(
                    select(ResearchCandidate.id).where(
                        ResearchCandidate.run_id == self.run_id,
                        ResearchCandidate.expression_hash == expr_hash,
                        ResearchCandidate.settings_hash == settings_hash,
                        ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
                    ).limit(1)
                )).scalar_one_or_none()
                prior_duplicate = False
                if not existing and len(self.dedup_task_ids) > 1:
                    identity = wq_settings_identity(settings, region=task.region, universe=task.universe)
                    prior_candidates = (await db.execute(
                        select(ResearchCandidate.settings).where(
                            ResearchCandidate.task_id.in_(self.dedup_task_ids),
                            ResearchCandidate.expression_hash == expr_hash,
                            ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
                        )
                    )).scalars().all()
                    prior_duplicate = any(
                        wq_settings_identity(previous, region=task.region, universe=task.universe) == identity
                        for previous in prior_candidates
                    )
                    if not prior_duplicate:
                        prior_alphas = (await db.execute(
                            select(Alpha.settings, Alpha.region, Alpha.universe, Alpha.delay,
                                   Alpha.decay, Alpha.neutralization, Alpha.truncation).where(
                                Alpha.task_id.in_(self.dedup_task_ids),
                                Alpha.alpha_id.is_not(None),
                                Alpha.expression_hash == normalized_alpha_hash,
                            )
                        )).all()
                        # Historical Alpha rows may predate normalized hashes.
                        # Scan only the finite baseline tasks, never a growing
                        # continuous task, for case-only legacy variations.
                        baseline_ids = [item for item in self.dedup_task_ids if item != self.task_id]
                        if baseline_ids:
                            prior_alphas += (await db.execute(
                                select(Alpha.settings, Alpha.region, Alpha.universe, Alpha.delay,
                                       Alpha.decay, Alpha.neutralization, Alpha.truncation).where(
                                    Alpha.task_id.in_(baseline_ids),
                                    Alpha.alpha_id.is_not(None),
                                    func.lower(Alpha.expression) == expression.strip().lower(),
                                )
                            )).all()
                        prior_duplicate = any(
                            wq_settings_identity({
                                **(saved_settings if isinstance(saved_settings, dict) else {}),
                                "delay": delay, "decay": decay,
                                "neutralization": neutralization, "truncation": truncation,
                            }, region=region, universe=universe) == identity
                            for saved_settings, region, universe, delay, decay, neutralization, truncation in prior_alphas
                        )
                used = 0
                if self.max_requests is not None:
                    used = (await db.execute(
                        select(func.count(ResearchCandidate.id)).where(
                            ResearchCandidate.run_id == self.run_id,
                            ResearchCandidate.request_status.in_(("RESERVED", "REQUESTED", "RESULT", "FAILED", "UNKNOWN")),
                        )
                    )).scalar_one()
                reason = ("TASK_NOT_RUNNING" if not task or task.status != "RUNNING" else
                          "DUPLICATE" if existing or prior_duplicate else
                          "BUDGET_EXHAUSTED" if self.max_requests is not None and used >= self.max_requests else None)
                candidate = ResearchCandidate(
                    task_id=self.task_id, run_id=self.run_id, family_id=self.family_id,
                    experiment_group=metadata.get("group"), pair_id=metadata.get("pair_id"),
                    arm=metadata.get("arm"), window=metadata.get("window"),
                    parent_candidate_id=metadata.get("parent_candidate_id"),
                    source=source, expression=expression,
                    expression_hash=expr_hash, settings_hash=settings_hash, settings=settings,
                    validation_status="VALID" if reason is None else "REJECTED",
                    rejection_stage="DEDUP" if reason == "DUPLICATE" else "REQUEST_BUDGET" if reason else None,
                    rejection_reason=reason,
                    request_status="RESERVED" if reason is None else "NOT_REQUESTED",
                    gate_failures=[], metrics={},
                    result_snapshot={},
                )
                db.add(candidate)
                await db.flush()
                candidate_id = candidate.id
            return (candidate_id if reason is None else None, reason)

    async def reject(self, expression: str, settings: dict, source: str,
                     stage: str, reason: str, metadata: dict | None = None) -> None:
        metadata = metadata or {}
        expr_hash, settings_hash = signature(expression, settings)
        async with AsyncSessionLocal() as db:
            db.add(ResearchCandidate(
                task_id=self.task_id, run_id=self.run_id, family_id=self.family_id,
                experiment_group=metadata.get("group"), pair_id=metadata.get("pair_id"),
                arm=metadata.get("arm"), window=metadata.get("window"),
                parent_candidate_id=metadata.get("parent_candidate_id"),
                source=source, expression=expression, expression_hash=expr_hash,
                settings_hash=settings_hash, settings=settings, validation_status="REJECTED",
                rejection_stage=stage[:40], rejection_reason=reason[:100],
                request_status="NOT_REQUESTED", gate_failures=[], metrics={},
                result_snapshot={},
            ))
            await db.commit()

    async def finish(self, candidate_id: int, result: dict) -> None:
        async with AsyncSessionLocal() as db:
            candidate = await db.get(ResearchCandidate, candidate_id)
            if candidate is None:
                return
            error = str(result.get("error") or "").lower()
            candidate.request_status = (
                "RESULT" if result.get("alpha_id") else
                "UNKNOWN" if any(word in error for word in (
                    "timeout", "timed out", "connection", "uncertain", "fewer results", "missing response",
                )) else
                "FAILED"
            )
            candidate.wq_alpha_id = result.get("alpha_id")
            candidate.metrics = safe_metrics(result)
            candidate.result_snapshot = safe_result(result)
            candidate.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def mark_requested(self, candidate_id: int) -> None:
        async with AsyncSessionLocal() as db:
            candidate = await db.get(ResearchCandidate, candidate_id)
            if candidate is not None and candidate.request_status == "RESERVED":
                candidate.request_status = "REQUESTED"
                await db.commit()

    async def recover(self, expression: str, settings: dict) -> dict | None:
        """Return a prior completed response without another WQ request."""
        expr_hash, settings_hash = signature(expression, settings)
        async with AsyncSessionLocal() as db:
            candidate = (await db.execute(select(ResearchCandidate).where(
                ResearchCandidate.run_id == self.run_id,
                ResearchCandidate.expression_hash == expr_hash,
                ResearchCandidate.settings_hash == settings_hash,
                ResearchCandidate.request_status == "RESULT",
            ).order_by(ResearchCandidate.id).limit(1))).scalar_one_or_none()
            return dict(candidate.result_snapshot or {}) if candidate else None


class ResearchBudgetBrain:
    """Simulation entry shared by generation, optimization, and genetic calls."""

    def __init__(self, brain, ledger: ResearchLedger):
        self.brain = brain
        self.ledger = ledger
        self.current_source = "GENERATION"
        self.metadata: dict[str, dict] = {}
        if hasattr(brain, "no_fallback_on_simulation_error"):
            brain.no_fallback_on_simulation_error = True

    def __getattr__(self, key):
        return getattr(self.brain, key)

    async def __aenter__(self):
        # The worker owns the underlying BRAIN session; MiningAgent enters this
        # wrapper once more around each round.
        return self

    async def __aexit__(self, *_):
        return False

    @contextmanager
    def source(self, value: str):
        previous = self.current_source
        self.current_source = value
        try:
            yield self
        finally:
            self.current_source = previous

    def register(self, expression: str, **metadata):
        self.metadata[expression] = metadata

    async def simulate_alpha(self, expression: str, **settings) -> dict:
        results = await self.simulate_batch([expression], **settings)
        return results[0]

    async def simulate_batch(self, expressions: list[str], **settings) -> list[dict]:
        results: list[dict | None] = [None] * len(expressions)
        allowed: list[str] = []
        reservations: list[tuple[int, int]] = []
        for index, expression in enumerate(expressions):
            candidate_id, reason = await self.ledger.reserve(
                expression, settings, self.current_source, self.metadata.get(expression),
            )
            if candidate_id is None:
                recovered = None
                if reason == "DUPLICATE" and self.metadata.get(expression, {}).get("arm"):
                    recover = getattr(self.ledger, "recover", None)
                    if recover is not None:
                        recovered = await recover(expression, settings)
                results[index] = recovered or {"success": False, "error": reason}
            else:
                allowed.append(expression)
                reservations.append((index, candidate_id))
        if allowed:
            try:
                mark_requested = getattr(self.ledger, "mark_requested", None)
                if mark_requested is not None:
                    for _, candidate_id in reservations:
                        await mark_requested(candidate_id)
                if len(allowed) == 1 and hasattr(self.brain, "simulate_single"):
                    network_results = [await self.brain.simulate_single(allowed[0], **settings)]
                else:
                    network_results = await self.brain.simulate_batch(allowed, **settings)
            except Exception:
                network_results = [{"success": False, "error": "uncertain network result"} for _ in allowed]
            for position, (index, candidate_id) in enumerate(reservations):
                result = (network_results[position] if position < len(network_results) else
                          {"success": False, "error": "uncertain missing response"})
                await self.ledger.finish(candidate_id, result)
                results[index] = result
        return [item or {"success": False, "error": "uncertain missing response"} for item in results]
