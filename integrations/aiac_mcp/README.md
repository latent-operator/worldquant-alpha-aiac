# Current local AIAC MCP

Eight read-only tools use the original API on `127.0.0.1:8003`. Run `server.py`
with an interpreter containing `requirements.txt` dependencies. Register its absolute
path under the existing `aiac_local` MCP configuration; restart the client to reload tools.

Task, run, trace and Alpha reads use original routes, not retired fork `/summary`,
`/trace-page` or `/research-report` extensions. Reports preserve explicit request
states and count stored WQ results separately from quality PASS. PPA remains unproven.
No account configuration, WQ calls, task controls or credential endpoints are exposed.

Offline tests: `python -m unittest integrations.aiac_mcp.test_client -q`.
