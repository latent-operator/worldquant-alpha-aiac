"""One offline preflight contract for local work and CI."""

from scripts import preflight


def test_preflight_runs_backend_and_frontend_checks():
    commands = preflight.command_plan()
    labels = [item.label for item in commands]
    assert labels == [
        "git diff", "staged git diff", "repository hygiene", "backend unit tests",
        "AIAC MCP tests",
        "frontend tests", "frontend lint", "frontend build",
    ]
    assert all("brain" not in " ".join(item.argv).lower() for item in commands)
    if preflight.os.name == "nt":
        assert all(item.argv[0] == "npm.cmd" for item in commands[5:])


def test_preflight_clears_live_credentials_and_network_targets():
    env = preflight.offline_environment({
        "BRAIN_EMAIL": "private", "BRAIN_PASSWORD": "private",
        "OPENAI_API_KEY": "private", "CREDENTIALS_SECRET": "private",
        "POSTGRES_PORT": "55433", "REDIS_URL": "redis://127.0.0.1:56579/0",
    })
    for name in ("BRAIN_EMAIL", "BRAIN_PASSWORD", "OPENAI_API_KEY", "CREDENTIALS_SECRET"):
        assert env[name] == ""
    assert env["POSTGRES_PORT"] == "1"
    assert env["REDIS_URL"] == "redis://127.0.0.1:1/0"
