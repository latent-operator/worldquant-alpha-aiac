"""Offline tests for private campaign preparation and catalog selection."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.tests.unit.test_ppa_feedback import base as fixture_base
from scripts.ppa_discovery_campaign import (_evidence, _live_catalog, _load_campaign,
    _new_base, _read_private)
from backend.evals.discovery_campaign import choose_dataset, freeze_campaign_batch
from backend.services.ppa_feedback import digest


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def json(self):
        return self.data


class CatalogBrain:
    def __init__(self):
        self.calls = []
    async def _safe_api_call(self, method, path, **kwargs):
        assert method == "GET" and path == "/data-sets"
        self.calls.append(kwargs["params"])
        return Response({"results": [
            {"id": "sentiment26", "category": "sentiment", "coverage": 0.9},
            {"id": "news76", "category": "news", "coverage": 0.8}]})
    async def get_datafields(self, dataset_id, **kwargs):
        if dataset_id == "sentiment26":
            return [{"id": "vector", "type": "VECTOR", "coverage": 0.9, "dateCoverage": 0.9}]
        return [{"id": name, "type": "MATRIX", "coverage": 0.8, "dateCoverage": 0.7}
                for name in ("a", "b")]


@pytest.mark.asyncio
async def test_live_catalog_skips_unusable_preferred_dataset():
    brain = CatalogBrain()
    datasets, fields = await _live_catalog(brain)
    selected = choose_dataset(datasets, fields, excluded={"pv1", "model110", "predictive_starmine"})
    assert selected["dataset_id"] == "news76"
    assert brain.calls[0]["region"] == "GLB"
    assert brain.calls[0]["universe"] == "TOP3000"
    assert brain.calls[0]["limit"] == 50


def test_new_base_and_evidence_exclude_old_expressions():
    source = fixture_base()
    source["datasets"] = {"pv1": ["returns"]}
    selected = {"dataset_id": "news76", "category": "news", "field_ids": ["a", "b"],
                "field_metadata": {name: {"type": "MATRIX", "account_verified": True}
                                   for name in ("a", "b")}}
    base = _new_base(source, selected, verified_at=datetime.now(timezone.utc).isoformat())
    assert list(base["datasets"]) == ["news76"]
    old = {"candidates": [{"expression": "rank(a)"}]}
    evidence = _evidence({"evidence_hash": "source"}, old, [], base)
    from backend.services.ppa_feedback import case_settings, signature
    assert list(signature("rank(a)", case_settings(base, {}))) in evidence["history_signatures"]
    assert evidence["budget_remaining"] is None


def test_private_campaign_hash_rejects_tampering(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    campaign = {"campaign_id": "one", "code_commit": "a" * 40}
    campaign["campaign_sha256"] = digest(campaign)
    path = root / "campaign.json"
    path.write_text(json.dumps(campaign), encoding="utf-8")
    loaded, batches = _load_campaign(root)
    assert loaded["campaign_id"] == "one" and batches == []
    campaign["code_commit"] = "b" * 40
    path.write_text(json.dumps(campaign), encoding="utf-8")
    with pytest.raises(ValueError, match="CAMPAIGN_HASH_MISMATCH"):
        _load_campaign(root)
