"""The released UI serves built files and proxies the existing API path."""

import pytest
import gzip
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from scripts import serve_ui


@pytest.mark.asyncio
async def test_static_ui_and_spa_fallback(tmp_path):
    (tmp_path / "index.html").write_text("<title>AIAC release</title>")
    app = serve_ui.make_app(tmp_path, "a" * 40, "http://127.0.0.1:1")
    async with TestClient(TestServer(app)) as client:
        assert (await (await client.get("/tasks")).text()) == "<title>AIAC release</title>"
        health = await (await client.get("/__ui_health")).json()
        assert health == {"status": "ok", "commit": "a" * 40}


@pytest.mark.asyncio
async def test_api_path_is_proxied_without_using_live_wq(tmp_path):
    (tmp_path / "index.html").write_text("ready")
    upstream = web.Application()
    upstream.router.add_get("/api/v1/ping", lambda _: web.json_response({"api": "ok"}))
    async with TestServer(upstream) as api:
        app = serve_ui.make_app(tmp_path, "a" * 40, str(api.make_url("/"))[:-1])
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/v1/ping")
            assert response.status == 200
            assert await response.json() == {"api": "ok"}


@pytest.mark.asyncio
async def test_proxy_preserves_compressed_api_response(tmp_path):
    (tmp_path / "index.html").write_text("ready")
    upstream = web.Application()
    upstream.router.add_get("/api/v1/ping", lambda _: web.Response(
        body=gzip.compress(b'{"api":"ok"}'),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    ))
    async with TestServer(upstream) as api:
        app = serve_ui.make_app(tmp_path, "a" * 40, str(api.make_url("/"))[:-1])
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/v1/ping")
            assert response.status == 200
            assert await response.json() == {"api": "ok"}
