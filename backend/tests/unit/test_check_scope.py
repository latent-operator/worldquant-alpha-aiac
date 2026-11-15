from scripts.check_scope import docs_only


def test_docs_scope_excludes_execution_and_policy_changes():
    assert docs_only(["docs/goal-loop.md", "openspec/changes/archive/x/tasks.md"])
    assert not docs_only([])
    for path in ["AGENTS.md", "openspec/config.yaml", "scripts/preflight.py",
                 "docs/helper.py", ".github/workflows/ci.yml"]:
        assert not docs_only(["docs/goal-loop.md", path])
