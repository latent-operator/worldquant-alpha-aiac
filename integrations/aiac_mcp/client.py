"""Narrow local AIAC API client. The MCP entry point uses only FIXED_BASE_URL."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


FIXED_BASE_URL = "http://127.0.0.1:8003/api/v1"


class AIACError(RuntimeError):
    pass


def observed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_text(value: Any, limit: int = 300) -> str:
    """Avoid echoing free-form API errors, prompts, or credential-like strings."""
    import re

    text = str(value or "")[:limit]
    text = re.sub(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+)", "[redacted]", text)
    text = re.sub(r"(?i)(password|api[_ -]?key|secret|token)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    return text


def public_run(run: dict) -> dict:
    keys = ("id", "task_id", "status", "trigger_source", "started_at", "finished_at", "next_retry_at")
    return {key: run.get(key) for key in keys} | {"block_reason": safe_text(run.get("block_reason")) or None, "error_summary": safe_text(run.get("error_message")) or None}


def public_alpha(alpha: dict, *, include_expression: bool = False) -> dict:
    keys = ("id", "alpha_id", "task_id", "run_id", "region", "dataset_id", "quality_status", "created_at")
    result = {key: alpha.get(key) for key in keys}
    metrics = alpha.get("metrics") or {}
    metric_keys = ("sharpe", "fitness", "turnover", "margin", "returns", "two_year_sharpe", "os_sharpe", "os_fitness")
    result["metrics"] = {key: metrics[key] for key in metric_keys if key in metrics}
    if include_expression:
        result["expression"] = safe_text(alpha.get("expression"), limit=10_000)
    return result


def public_trace(step: dict, *, detail: bool = False) -> dict:
    keys = ("id", "run_id", "iteration", "step_type", "step_order", "status", "duration_ms", "created_at")
    result = {key: step.get(key) for key in keys}
    result["error_summary"] = safe_text(step.get("error_message")) or None
    if detail:
        output = step.get("output_data") or {}
        allowed = ("alphas_generated", "new_llm_alphas", "valid_count", "invalid_count", "duplicate_count", "simulated_count", "success_count", "saved_alphas", "saved_failures", "pass_count", "fail_count", "optimize_count", "reason")
        result["output_summary"] = {key: (safe_text(output[key]) if key == "reason" else output[key]) for key in allowed if key in output}
    return result


class AIACClient:
    def __init__(self, base_url: str = FIXED_BASE_URL, transport: httpx.AsyncBaseTransport | None = None):
        # base_url/transport injection is for isolated tests. MCP tools expose neither.
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, write: bool = False) -> Any:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=20.0, trust_env=False) as client:
                response = await client.request(method, path, params={k: v for k, v in (params or {}).items() if v is not None}, json=json)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
            if write:
                raise AIACError("AIAC 写请求结果不确定；已停止，先查询任务状态，不能自动重发。") from exc
            raise AIACError("无法连接本地 AIAC API（127.0.0.1:8003）；这不表示没有任务。") from exc
        if response.status_code >= 400:
            raise AIACError(f"AIAC API 返回 HTTP {response.status_code}；请检查本地服务和任务状态。")
        try:
            return response.json()
        except ValueError as exc:
            raise AIACError("AIAC API 返回了无效 JSON。") from exc

    async def task(self, task_id: int) -> dict:
        result = await self.request("GET", f"/tasks/{task_id}/summary")
        result["task_name"] = safe_text(result.get("task_name"), limit=255)
        result["block_reason"] = safe_text(result.get("block_reason")) or None
        return result

    async def runs(self, task_id: int) -> list[dict]:
        return await self.request("GET", f"/tasks/{task_id}/runs/summary")

    async def write_task(self, method: str, path: str, body: dict | None = None, task_id: int | None = None) -> dict:
        try:
            return await self.request(method, path, json=body, write=True)
        except AIACError as exc:
            if "结果不确定" not in str(exc):
                raise
            state = None
            if task_id is not None:
                try:
                    state = await self.task(task_id)
                except AIACError:
                    pass
            possible_created_tasks = []
            if path == "/tasks" and body and body.get("name"):
                try:
                    recent = await self.list_tasks(limit=100)
                    possible_created_tasks = [{"id": item["id"], "task_name": item["task_name"], "status": item["status"]} for item in recent["items"] if item.get("task_name") == body["name"]]
                except AIACError:
                    pass
            return {"outcome": "UNCERTAIN", "reason": str(exc), "observed_task": state, "possible_created_tasks": possible_created_tasks, "retried": False, "source": "AIAC API", "observed_at": observed_at()}

    async def list_tasks(self, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        items = await self.request("GET", "/tasks", params={"status": status, "limit": limit, "offset": offset})
        for item in items:
            item["task_name"] = safe_text(item.get("task_name"), limit=255)
        return {"items": items, "limit": limit, "offset": offset, "has_more": len(items) == limit, "source": "AIAC API", "observed_at": observed_at()}

    async def list_run_alphas(self, run_id: int, quality_status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        page = await self.request("GET", f"/runs/{run_id}/alphas", params={"quality_status": quality_status, "limit": limit, "offset": offset})
        return {"run_id": run_id, "items": [public_alpha(item) for item in page["items"]], "total": page["total"], "limit": limit, "offset": offset, "has_more": offset + len(page["items"]) < page["total"], "source": "AIAC API", "observed_at": observed_at()}

    async def list_trace(self, run_id: int, limit: int = 30, offset: int = 0, iteration: int | None = None, step_type: str | None = None, detail: bool = False) -> dict:
        page = await self.request("GET", f"/runs/{run_id}/trace-page", params={"limit": limit, "offset": offset, "iteration": iteration, "step_type": step_type})
        return {"run_id": run_id, "items": [public_trace(item, detail=detail) for item in page["items"]], "total": page["total"], "limit": limit, "offset": offset, "has_more": page["has_more"], "source": "AIAC API", "observed_at": observed_at()}

    async def report(self, run_id: int) -> dict:
        run = await self.request("GET", f"/runs/{run_id}/summary")
        alphas: list[dict] = []
        offset = 0
        while True:
            page = await self.request("GET", f"/runs/{run_id}/alphas", params={"limit": 200, "offset": offset})
            alphas.extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                break
        rounds: dict[int, dict[str, int]] = {}
        offset = 0
        while True:
            page = await self.request("GET", f"/runs/{run_id}/trace-page", params={"limit": 100, "offset": offset})
            for step in page["items"]:
                if step.get("status") != "SUCCESS":
                    continue
                iteration = int(step.get("iteration") or 0)
                if not iteration:
                    continue
                counts = rounds.setdefault(iteration, {"generated": 0, "simulated": 0})
                output = step.get("output_data") or {}
                if step.get("step_type") == "CODE_GEN":
                    value = output.get("alphas_generated")
                    if value is None:
                        value = len(output.get("expressions") or [])
                    counts["generated"] = max(counts["generated"], int(value or 0))
                elif step.get("step_type") == "SIMULATE":
                    counts["simulated"] = max(counts["simulated"], int(output.get("simulated_count") or 0))
            offset += len(page["items"])
            if offset >= page["total"] or not page["items"]:
                break
        with_wq_id = [item for item in alphas if item.get("alpha_id")]
        passes = [item for item in with_wq_id if item.get("quality_status") == "PASS"]

        def metric(item: dict, name: str) -> float | None:
            raw = (item.get("metrics") or {}).get(name)
            if raw is None:
                raw = ((item.get("metrics") or {}).get("is") or {}).get(name)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        best = max(
            with_wq_id,
            key=lambda item: metric(item, "sharpe") if metric(item, "sharpe") is not None else float("-inf"),
            default=None,
        )
        best_summary = None
        if best is not None:
            best_summary = {
                "id": best["id"], "wq_alpha_id": best["alpha_id"],
                "sharpe": metric(best, "sharpe"), "fitness": metric(best, "fitness"),
                "quality_status": best.get("quality_status"),
            }
        return {
            "run": public_run(run), "run_id": run_id,
            "generated_candidates": sum(row["generated"] for row in rounds.values()),
            "entered_simulation": sum(row["simulated"] for row in rounds.values()),
            "saved_wq_results": len(with_wq_id), "quality_pass": len(passes),
            "alpha_rows_total": len(alphas),
            "wq_alpha_ids_sample": [item["alpha_id"] for item in with_wq_id[:10]],
            "best_sharpe_alpha": best_summary,
            "source": {
                "run": "AIAC /runs/{id}/summary",
                "candidates": "AIAC CODE_GEN trace",
                "simulation": "AIAC SIMULATE trace",
                "wq_results_and_pass": "AIAC /runs/{id}/alphas; PASS is quality_status",
            },
            "observed_at": observed_at(),
        }

    async def research_report(self, run_id: int) -> dict:
        result = await self.request("GET", f"/runs/{run_id}/research-report")
        # The API report contains only counters, IDs, gate names and metrics.
        return result | {"source": "AIAC research-report API", "observed_at": observed_at()}

    async def research_candidates(self, run_id: int, limit: int = 50, offset: int = 0) -> dict:
        page = await self.request("GET", f"/runs/{run_id}/research-candidates",
                                  params={"limit": limit, "offset": offset})
        items = []
        for row in page["items"]:
            item = {key: row.get(key) for key in (
                "id", "run_id", "family_id", "source", "arm", "experiment_group",
                "pair_id", "window", "parent_candidate_id", "expression_hash",
                "settings_hash", "validation_status", "rejection_stage", "rejection_reason",
                "request_status", "wq_alpha_id", "quality_status", "gate_failures",
                "metrics", "trace_step_id", "created_at",
            )}
            items.append(item)
        return {"run_id": run_id, "items": items, "total": page["total"],
                "limit": limit, "offset": offset, "has_more": page["has_more"],
                "source": "AIAC research-candidates API", "observed_at": observed_at()}

    async def start(self, task_id: int) -> dict:
        task = await self.task(task_id)
        if task["status"] in ("RUNNING", "WAITING_LLM"):
            return {"outcome": "ALREADY_ACTIVE", "task_id": task_id, "run_id": task.get("latest_run_id"), "source": "AIAC API", "observed_at": observed_at()}
        if task["status"] == "PAUSED":
            raise AIACError("任务已暂停；请使用 aiac_resume_task 沿用原 run。")
        result = await self.write_task("POST", f"/tasks/{task_id}/start", task_id=task_id)
        return result | {"source": "AIAC API", "observed_at": observed_at()}

    async def intervene(self, task_id: int, action: str) -> dict:
        task = await self.task(task_id)
        status = task["status"]
        wanted = {"PAUSE": "PAUSED", "STOP": "STOPPED", "RESUME": "RUNNING"}[action]
        if status == wanted or (action == "RESUME" and status == "WAITING_LLM"):
            return {"outcome": "ALREADY_IN_STATE", "task_id": task_id, "run_id": task.get("latest_run_id"), "status": status, "source": "AIAC API", "observed_at": observed_at()}
        allowed = {"PAUSE": ("RUNNING", "WAITING_LLM"), "STOP": ("RUNNING", "WAITING_LLM", "PAUSED"), "RESUME": ("PAUSED",)}[action]
        if status not in allowed:
            raise AIACError(f"任务当前状态 {status} 不支持 {action}。")
        old_run_id = task.get("latest_run_id")
        result = await self.write_task("POST", f"/tasks/{task_id}/intervene", {"action": action, "parameters": {}}, task_id=task_id)
        if action == "RESUME" and result.get("outcome") != "UNCERTAIN":
            queued_run_id = result.get("run_id")
            new_task = await self.task(task_id)
            result["same_run"] = new_task.get("latest_run_id") == old_run_id
            result["run_id"] = new_task.get("latest_run_id")
            result["resume_enqueued"] = queued_run_id is not None and queued_run_id == old_run_id
            if not result["resume_enqueued"]:
                result["outcome"] = "RESUME_UNVERIFIED"
        return result | {"source": "AIAC API", "observed_at": observed_at()}
