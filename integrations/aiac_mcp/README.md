# Local AIAC MCP for Codex

This stdio MCP server connects only to `http://127.0.0.1:8003/api/v1`.
It delegates all task actions to AIAC's existing API. It does not call WQ,
read credentials, submit alphas, or operate the old loop.

## Install

```powershell
py -m venv integrations/aiac_mcp/.venv
integrations/aiac_mcp/.venv/Scripts/python.exe -m pip install -r integrations/aiac_mcp/requirements.txt
```

Register the absolute interpreter and `server.py` paths as a global Codex
`mcp_servers.aiac_local` stdio server. The service must run with AIAC API on
port 8003. Set `default_tools_approval_mode = "auto"` under the server's
`config.toml` table if user instruction should authorize writes without a
second popup. Restart Codex after registering the server. There are no
credential environment variables.

Read tools return `source` and `observed_at`. `aiac_get_run_report` separates
trace-derived generation counts, trace-derived simulation attempts, stored WQ
Alpha IDs, and quality `PASS` rows. Counts can change while a run is active.

Writes are single attempts. If a POST times out, the bridge checks task state
once, returns `UNCERTAIN`, and does not retry. An uncertain create request
requires checking the task list before making another create request.

`tests/smoke_live.py` makes only read requests against existing task 7/run 10.
