"""Browser access must not turn local services into a cross-origin control API."""
from pathlib import Path

import httpx
import pytest
import yaml

from backend.main import app


@pytest.mark.asyncio
async def test_untrusted_origin_cannot_preflight_or_send_simple_post():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8003") as client:
        preflight = await client.options("/api/v1/tasks/14/start", headers={
            "Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"})
        assert preflight.status_code in (400, 403)
        simple = await client.post("/api/v1/nonexistent", headers={"Origin": "https://untrusted.example"})
        assert simple.status_code == 403
        assert "access-control-allow-origin" not in simple.headers


@pytest.mark.asyncio
async def test_trusted_ui_and_no_origin_local_clients_work():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8003") as client:
        assert (await client.get("/health")).status_code == 200
        response = await client.options("/api/v1/tasks/14/start", headers={
            "Origin": "http://127.0.0.1:5174", "Access-Control-Request-Method": "POST"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"


@pytest.mark.asyncio
async def test_rebound_api_host_is_rejected():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://attacker.example:8003") as client:
        assert (await client.get("/health")).status_code == 400


def test_default_compose_exposes_ports_only_on_loopback():
    config = yaml.safe_load((Path(__file__).parents[3] / "docker-compose.yml").read_text())
    for service in config["services"].values():
        for binding in service.get("ports", []):
            assert str(binding).startswith("127.0.0.1:")
