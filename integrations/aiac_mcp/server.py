"""Codex -> local AIAC bridge. No WQ client or credential access."""

from __future__ import annotations

from functools import wraps
import logging
import re
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from client import AIACClient, AIACError, observed_at, public_alpha, public_run, safe_text

logging.getLogger("httpx").setLevel(logging.WARNING)


def bounded(value: int, lower: int, upper: int, label: str) -> int:
    if not lower <= value <= upper:
        raise AIACError(f"{label} 必须在 {lower} 到 {upper} 之间。")
    return value


def code_filter(value: str | None, label: str) -> str | None:
    if value is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", value):
        raise AIACError(f"{label} 只能使用大写状态代码。")
    return value


def contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in ("password", "apikey", "secret", "token", "credential")):
                return True
            if contains_credentials(child):
                return True
    elif isinstance(value, list):
        return any(contains_credentials(child) for child in value)
    elif isinstance(value, str):
        return bool(re.search(r"\bsk-[A-Za-z0-9_-]{8,}\b", value))
    return False


def tool_errors(fn):
    """Turn known local API failures into concise, actionable MCP tool errors."""
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except AIACError as exc:
            raise ToolError(str(exc)) from None
    return wrapper


def build_server(api: AIACClient | None = None) -> MCPServer:
    api = api or AIACClient()
    server = MCPServer(
        name="aiac-local",
        description="Read and control the local AIAC mining service. Simulation remains inside AIAC workers.",
        instructions="Read AIAC facts through these tools. Do not equate HTTP success with Alpha quality PASS. Never call task controls unless the user instructed the action.",
    )
    read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)

    @server.tool(name="aiac_list_tasks", annotations=read)
    @tool_errors
    async def aiac_list_tasks(status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """List local AIAC mining tasks, optionally filtering by status; paginate with limit/offset."""
        return await api.list_tasks(code_filter(status, "status"), bounded(limit, 1, 100, "limit"), bounded(offset, 0, 1_000_000, "offset"))

    @server.tool(name="aiac_get_task", annotations=read)
    @tool_errors
    async def aiac_get_task(task_id: int) -> dict:
        """Get one lightweight task summary including latest run and waiting state."""
        result = await api.task(bounded(task_id, 1, 1_000_000_000, "task_id"))
        return result | {"source": "AIAC API", "observed_at": observed_at()}

    @server.tool(name="aiac_list_runs", annotations=read)
    @tool_errors
    async def aiac_list_runs(task_id: int) -> dict:
        """List a task's run history without checkpoints or full traces."""
        rows = await api.runs(bounded(task_id, 1, 1_000_000_000, "task_id"))
        return {"task_id": task_id, "items": [public_run(row) for row in rows], "source": "AIAC API", "observed_at": observed_at()}

    @server.tool(name="aiac_get_run", annotations=read)
    @tool_errors
    async def aiac_get_run(run_id: int) -> dict:
        """Get a run's status, timing, block reason, and retry time without config snapshots."""
        row = await api.request("GET", f"/runs/{bounded(run_id, 1, 1_000_000_000, 'run_id')}/summary")
        return public_run(row) | {"source": "AIAC API", "observed_at": observed_at()}

    @server.tool(name="aiac_list_run_alphas", annotations=read)
    @tool_errors
    async def aiac_list_run_alphas(run_id: int, quality_status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """List saved WQ Alpha results for a run, using AIAC limit/offset pagination."""
        return await api.list_run_alphas(bounded(run_id, 1, 1_000_000_000, "run_id"), code_filter(quality_status, "quality_status"), bounded(limit, 1, 200, "limit"), bounded(offset, 0, 1_000_000, "offset"))

    @server.tool(name="aiac_get_alpha", annotations=read)
    @tool_errors
    async def aiac_get_alpha(alpha_id: int) -> dict:
        """Get one stored Alpha by AIAC database ID, including expression and key metrics."""
        row = await api.request("GET", f"/alphas/{bounded(alpha_id, 1, 1_000_000_000, 'alpha_id')}")
        return public_alpha(row, include_expression=True) | {"source": "AIAC API", "observed_at": observed_at()}

    @server.tool(name="aiac_list_trace", annotations=read)
    @tool_errors
    async def aiac_list_trace(run_id: int, limit: int = 30, offset: int = 0, iteration: int | None = None, step_type: str | None = None, detail: bool = False) -> dict:
        """Read a bounded trace page. Detail adds only safe numeric counters and short reasons."""
        return await api.list_trace(bounded(run_id, 1, 1_000_000_000, "run_id"), bounded(limit, 1, 100, "limit"), bounded(offset, 0, 1_000_000, "offset"), bounded(iteration, 1, 1_000_000, "iteration") if iteration is not None else None, code_filter(step_type, "step_type"), detail)

    @server.tool(name="aiac_get_run_report", annotations=read)
    @tool_errors
    async def aiac_get_run_report(run_id: int) -> dict:
        """Summarize generated candidates, simulation attempts, saved WQ results and quality PASS separately."""
        return await api.report(bounded(run_id, 1, 1_000_000_000, "run_id"))

    @server.tool(name="aiac_get_research_report", annotations=read)
    @tool_errors
    async def aiac_get_research_report(run_id: int) -> dict:
        """Read persisted candidate funnel, 40-request budget, paired deltas and actual quality PASS."""
        return await api.research_report(bounded(run_id, 1, 1_000_000_000, "run_id"))

    @server.tool(name="aiac_list_research_candidates", annotations=read)
    @tool_errors
    async def aiac_list_research_candidates(run_id: int, limit: int = 50, offset: int = 0) -> dict:
        """Read a bounded page of candidate lineage and rejection outcomes without expressions."""
        return await api.research_candidates(
            bounded(run_id, 1, 1_000_000_000, "run_id"),
            bounded(limit, 1, 100, "limit"), bounded(offset, 0, 1_000_000, "offset"),
        )

    @server.tool(name="aiac_create_task", annotations=write)
    @tool_errors
    async def aiac_create_task(name: str, region: str = "USA", universe: str = "TOP3000", dataset_strategy: str = "AUTO", target_datasets: list[str] | None = None, agent_mode: str = "AUTONOMOUS", daily_goal: int = 4, max_iterations: int = 10, continuous_mode: bool = False, config: dict[str, Any] | None = None) -> dict:
        """Create one AIAC task. This does not start a run; call only on a user instruction."""
        if not name.strip() or len(name) > 255:
            raise AIACError("任务名称必须为 1–255 个字符。")
        config = config or {}
        if len(str(config)) > 20_000 or contains_credentials(config):
            raise AIACError("任务配置过大或含凭证字段；MCP 不接收凭证。")
        body = {"name": name, "region": region, "universe": universe, "dataset_strategy": dataset_strategy, "target_datasets": target_datasets or [], "agent_mode": agent_mode, "daily_goal": bounded(daily_goal, 1, 1000, "daily_goal"), "max_iterations": bounded(max_iterations, 1, 100000, "max_iterations"), "continuous_mode": continuous_mode, "config": config}
        result = await api.write_task("POST", "/tasks", body)
        return (result if result.get("outcome") == "UNCERTAIN" else {"id": result.get("id"), "task_name": safe_text(result.get("task_name"), limit=255), "status": result.get("status")}) | {"source": "AIAC API", "observed_at": observed_at()}

    @server.tool(name="aiac_start_task", annotations=write)
    @tool_errors
    async def aiac_start_task(task_id: int) -> dict:
        """Start an inactive AIAC task once. Active tasks are not restarted; PAUSED must use resume."""
        return await api.start(bounded(task_id, 1, 1_000_000_000, "task_id"))

    @server.tool(name="aiac_pause_task", annotations=write)
    @tool_errors
    async def aiac_pause_task(task_id: int) -> dict:
        """Pause a RUNNING or WAITING_LLM task; preserve its run for later resume."""
        return await api.intervene(bounded(task_id, 1, 1_000_000_000, "task_id"), "PAUSE")

    @server.tool(name="aiac_resume_task", annotations=write)
    @tool_errors
    async def aiac_resume_task(task_id: int) -> dict:
        """Resume a PAUSED AIAC task through its existing run; never create a new run."""
        return await api.intervene(bounded(task_id, 1, 1_000_000_000, "task_id"), "RESUME")

    @server.tool(name="aiac_stop_task", annotations=write)
    @tool_errors
    async def aiac_stop_task(task_id: int) -> dict:
        """Stop an active or paused AIAC task through its task API."""
        return await api.intervene(bounded(task_id, 1, 1_000_000_000, "task_id"), "STOP")

    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")
