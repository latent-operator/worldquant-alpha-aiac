"""Serve a built local AIAC UI and proxy its API/SSE requests to the host API."""

from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web


HOP_HEADERS = {"connection", "host", "content-length", "transfer-encoding", "keep-alive", "upgrade"}


def make_app(dist: Path, commit: str, api_base: str = "http://127.0.0.1:8003") -> web.Application:
    root = dist.resolve()
    if not (root / "index.html").is_file():
        raise ValueError("Built UI has no index.html")

    app = web.Application(client_max_size=10 * 1024 * 1024)

    async def session_context(application):
        application["upstream_session"] = ClientSession(
            timeout=ClientTimeout(total=None, sock_read=None), auto_decompress=False,
        )
        yield
        await application["upstream_session"].close()

    app.cleanup_ctx.append(session_context)

    async def health(_request):
        return web.json_response({"status": "ok", "commit": commit})

    async def proxy(request):
        url = f"{api_base.rstrip('/')}{request.rel_url}"
        request_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        body = await request.read() if request.can_read_body else None
        async with request.app["upstream_session"].request(
            request.method, url, data=body, headers=request_headers,
        ) as upstream:
            response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response

    async def static(request):
        suffix = request.match_info.get("path", "")
        target = (root / suffix).resolve()
        if not target.is_relative_to(root):
            raise web.HTTPForbidden()
        if target.is_file():
            return web.FileResponse(target)
        return web.FileResponse(root / "index.html")

    app.router.add_get("/__ui_health", health)
    app.router.add_route("*", "/api/{path:.*}", proxy)
    app.router.add_get("/{path:.*}", static)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--port", type=int, default=5174)
    args = parser.parse_args()
    web.run_app(make_app(args.dist, args.commit), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
