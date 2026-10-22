"""Preflight must inspect new files before they enter a commit."""

from scripts import check_repo_hygiene


def test_candidate_paths_include_untracked_nonignored_files(monkeypatch):
    observed = {}

    def fake_check_output(args, cwd):
        observed["args"] = args
        return b"tracked.py\0new_file.py\0"

    monkeypatch.setattr(check_repo_hygiene.subprocess, "check_output", fake_check_output)
    assert check_repo_hygiene.candidate_paths() == ["tracked.py", "new_file.py"]
    assert observed["args"] == [
        "git", "ls-files", "--cached", "--others", "--exclude-standard", "-z",
    ]
