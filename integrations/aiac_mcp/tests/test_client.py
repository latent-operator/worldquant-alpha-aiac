"""Isolated API tests; never contact live AIAC or WQ."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client import AIACClient, AIACError
from server import build_server
from mcp.server.mcpserver.exceptions import ToolError


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_names(self):
        names = {tool.name for tool in await build_server().list_tools()}
        self.assertEqual(names, {
            "aiac_list_tasks", "aiac_get_task", "aiac_list_runs", "aiac_get_run",
            "aiac_list_run_alphas", "aiac_get_alpha", "aiac_list_trace",
            "aiac_get_run_report", "aiac_create_task", "aiac_start_task",
            "aiac_pause_task", "aiac_resume_task", "aiac_stop_task",
        })

    async def test_report_uses_limit_offset_and_separates_counts(self):
        requests = []
        def handler(request):
            requests.append((request.url.path, dict(request.url.params)))
            path = request.url.path
            offset = int(request.url.params.get("offset", 0))
            if path == "/api/v1/runs/5/summary":
                return httpx.Response(200, json={"id": 5, "task_id": 7, "status": "COMPLETED"})
            if path.endswith("/alphas"):
                return httpx.Response(200, json={"total": 2, "items": [
                    {"id": 1, "alpha_id": "WQ1", "quality_status": "PASS", "metrics": {"sharpe": 1.7, "fitness": 1.1}},
                    {"id": 2, "alpha_id": "WQ2", "quality_status": "FAIL", "metrics": {"sharpe": 0.2}},
                ] if offset == 0 else []})
            if path.endswith("/trace-page"):
                return httpx.Response(200, json={"total": 2, "items": [
                    {"iteration": 1, "status": "SUCCESS", "step_type": "CODE_GEN", "output_data": {"alphas_generated": 4}},
                    {"iteration": 1, "status": "SUCCESS", "step_type": "SIMULATE", "output_data": {"simulated_count": 2}},
                ] if offset == 0 else []})
            return httpx.Response(404)
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        report = await api.report(5)
        self.assertEqual((report["generated_candidates"], report["entered_simulation"], report["saved_wq_results"], report["quality_pass"]), (4, 2, 2, 1))
        self.assertEqual(report["best_sharpe_alpha"]["wq_alpha_id"], "WQ1")
        self.assertTrue(all("page" not in params for _, params in requests))
        self.assertTrue(any(params.get("limit") == "200" and params.get("offset") == "0" for _, params in requests))

    async def test_optional_trace_filters_are_omitted(self):
        seen = []
        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"items": [], "total": 0, "has_more": False})
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        await api.list_trace(5)
        self.assertNotIn("iteration", seen[0])
        self.assertNotIn("step_type", seen[0])

    async def test_active_start_is_noop_and_paused_requires_resume(self):
        state = {"status": "RUNNING", "latest_run_id": 10}
        writes = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=state)
            writes.append(request.url.path)
            return httpx.Response(200, json={"run_id": 11})
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        self.assertEqual((await api.start(7))["outcome"], "ALREADY_ACTIVE")
        state["status"] = "PAUSED"
        with self.assertRaises(AIACError):
            await api.start(7)
        self.assertEqual(writes, [])
        resumed = await api.intervene(7, "RESUME")
        self.assertTrue(resumed["same_run"])
        self.assertEqual(writes, ["/api/v1/tasks/7/intervene"])

    async def test_write_timeout_is_not_retried(self):
        posts = 0
        def handler(request):
            nonlocal posts
            if request.method == "GET":
                return httpx.Response(200, json={"status": "RUNNING", "latest_run_id": 10})
            posts += 1
            raise httpx.ReadTimeout("simulated")
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        result = await api.write_task("POST", "/tasks/7/start", task_id=7)
        self.assertEqual(posts, 1)
        self.assertEqual(result["outcome"], "UNCERTAIN")
        self.assertFalse(result["retried"])

    async def test_create_timeout_checks_recent_tasks_without_retry(self):
        posts = 0
        def handler(request):
            nonlocal posts
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 88, "task_name": "paper idea", "status": "PENDING"}])
            posts += 1
            raise httpx.ReadTimeout("simulated")
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        result = await api.write_task("POST", "/tasks", {"name": "paper idea"})
        self.assertEqual(posts, 1)
        self.assertEqual(result["outcome"], "UNCERTAIN")
        self.assertEqual(result["possible_created_tasks"][0]["id"], 88)

    async def test_create_start_pause_resume_stop_each_posts_once(self):
        state = {"status": "PENDING", "latest_run_id": None}
        writes = []
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=state)
            body = json.loads(request.content) if request.content else {}
            writes.append((request.url.path, body))
            if request.url.path.endswith("/start"):
                state.update(status="RUNNING", latest_run_id=21)
                return httpx.Response(200, json={"task_id": 8, "run_id": 21})
            if request.url.path.endswith("/intervene"):
                action = body["action"]
                state["status"] = {"PAUSE": "PAUSED", "RESUME": "RUNNING", "STOP": "STOPPED"}[action]
                return httpx.Response(200, json={"task_id": 8, "action": action, **({"run_id": 21} if action == "RESUME" else {})})
            return httpx.Response(200, json={"id": 8, "task_name": body["name"], "status": "PENDING"})
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        created = await api.write_task("POST", "/tasks", {"name": "paper idea"})
        self.assertEqual(created["id"], 8)
        started = await api.start(8)
        self.assertEqual(started["run_id"], 21)
        await api.intervene(8, "PAUSE")
        resumed = await api.intervene(8, "RESUME")
        self.assertTrue(resumed["same_run"])
        self.assertTrue(resumed["resume_enqueued"])
        await api.intervene(8, "STOP")
        self.assertEqual([path for path, _ in writes], [
            "/api/v1/tasks", "/api/v1/tasks/8/start",
            "/api/v1/tasks/8/intervene", "/api/v1/tasks/8/intervene",
            "/api/v1/tasks/8/intervene",
        ])

    async def test_error_and_trace_do_not_echo_key(self):
        from client import public_trace, public_run
        secret = "sk-" + "x" * 32
        run = public_run({"error_message": f"bad key {secret}", "block_reason": "LLM quota"})
        trace = public_trace({"error_message": f"Bearer {secret}", "output_data": {"prompt": secret, "simulated_count": 2}}, detail=True)
        self.assertNotIn(secret, json.dumps([run, trace]))
        self.assertNotIn("prompt", json.dumps(trace))

    async def test_api_error_body_is_not_exposed(self):
        secret = "sk-" + "x" * 32
        def handler(request):
            return httpx.Response(401, json={"detail": f"bad key {secret}"})
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        with self.assertRaises(AIACError) as raised:
            await api.list_tasks()
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("HTTP 401", str(raised.exception))

    async def test_unreachable_is_error_not_empty_list(self):
        def handler(request):
            raise httpx.ConnectError("offline")
        api = AIACClient("http://localhost/api/v1", httpx.MockTransport(handler))
        with self.assertRaisesRegex(AIACError, "无法连接"):
            await api.list_tasks()
        with self.assertRaisesRegex(ToolError, "AIAC API"):
            await build_server(api).call_tool("aiac_list_tasks", {})


if __name__ == "__main__":
    unittest.main()
