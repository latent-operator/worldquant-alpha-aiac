"""Read-only stdio smoke check against the current local AIAC API."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "server.py")])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", len(tools.tools))
            task = await session.call_tool("aiac_get_task", {"task_id": 7})
            runs = await session.call_tool("aiac_list_runs", {"task_id": 7})
            trace = await session.call_tool("aiac_list_trace", {"run_id": 10, "limit": 2})
            alpha = await session.call_tool("aiac_get_alpha", {"alpha_id": 1319})
            report = await session.call_tool("aiac_get_run_report", {"run_id": 10})
            for name, result in (("task", task), ("runs", runs), ("trace", trace), ("alpha", alpha), ("report", report)):
                if result.is_error:
                    raise RuntimeError(f"{name} tool failed: {result.content}")
                value = result.structured_content or json.loads(result.content[0].text)
                if name == "task":
                    print(json.dumps({"task_id": value["id"], "status": value["status"], "latest_run_id": value["latest_run_id"]}))
                elif name == "runs":
                    print(json.dumps({"runs": [row["id"] for row in value["items"]]}))
                elif name == "trace":
                    print(json.dumps({"trace_total": value["total"], "page_size": len(value["items"])}))
                elif name == "alpha":
                    print(json.dumps({"alpha_id": value["id"], "run_id": value["run_id"], "wq_alpha_id": value["alpha_id"], "quality_status": value["quality_status"]}))
                else:
                    print(json.dumps({key: value[key] for key in ("run_id", "generated_candidates", "entered_simulation", "saved_wq_results", "quality_pass", "best_sharpe_alpha")}))


if __name__ == "__main__":
    asyncio.run(main())
