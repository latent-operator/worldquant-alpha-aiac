"""A release requires the exact fork CI run and a reviewed public-key baseline."""

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


def test_unconfirmed_historical_key_blocks_release():
    with pytest.raises(ValueError, match="historical"):
        verify_release.validate_secret_attestation({"historical_keys_revoked": False})
    verify_release.validate_secret_attestation({
        "historical_keys_revoked": True, "reviewed_at": "2026-10-22",
    })
