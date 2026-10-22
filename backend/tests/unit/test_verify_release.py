"""A release requires the exact fork CI run and a reviewed legacy file."""

import json

import pytest

from scripts import verify_release


def test_successful_ci_must_match_commit_and_writable_fork():
    record = {
        "repository": {"full_name": "latent-operator/worldquant-alpha-aiac"},
        "name": "AIAC offline checks", "status": "completed", "conclusion": "success",
        "head_sha": "a" * 40, "head_branch": "master", "event": "push",
    }
    verify_release.validate_ci_run(record, "a" * 40)
    with pytest.raises(ValueError, match="commit"):
        verify_release.validate_ci_run(record, "b" * 40)
    with pytest.raises(ValueError, match="fork"):
        verify_release.validate_ci_run({**record, "repository": {"full_name": "someone/else"}}, "a" * 40)


def test_failed_or_unmerged_ci_cannot_release():
    record = {
        "repository": {"full_name": "latent-operator/worldquant-alpha-aiac"},
        "name": "AIAC offline checks", "status": "completed", "conclusion": "failure",
        "head_sha": "a" * 40, "head_branch": "master", "event": "push",
    }
    with pytest.raises(ValueError, match="successful"):
        verify_release.validate_ci_run(record, "a" * 40)
    with pytest.raises(ValueError, match="master"):
        verify_release.validate_ci_run({**record, "conclusion": "success", "event": "pull_request"}, "a" * 40)


def test_reviewed_legacy_dataset_list_is_not_treated_as_credentials():
    verify_release.validate_legacy_dataset_keys_blob(
        "9a3ce503dd74e397ed75f652e132ed97347a0b25"
    )
    with pytest.raises(ValueError, match="legacy"):
        verify_release.validate_legacy_dataset_keys_blob("0" * 40)


def test_release_gate_accepts_reviewed_dataset_list_without_rotation_attestation(monkeypatch):
    commit = "a" * 40
    ci = {
        "repository": {"full_name": "latent-operator/worldquant-alpha-aiac"},
        "name": "AIAC offline checks", "status": "completed", "conclusion": "success",
        "head_sha": commit, "head_branch": "master", "event": "push",
    }
    def fake_output(args):
        if args[:2] == ["git", "status"]:
            return ""
        if args[:2] == ["git", "rev-parse"]:
            return commit if args[2] == "HEAD" else verify_release.LEGACY_DATASET_KEYS_BLOB
        return json.dumps(ci)
    monkeypatch.setattr(verify_release, "_output", fake_output)
    assert verify_release.verify(commit, 123)["commit"] == commit
