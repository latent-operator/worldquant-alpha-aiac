"""Read current AIAC reports and compare an allowlisted temporary snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import httpx

from backend.evals.snapshot import compare_snapshot, sanitize_snapshot


FIXTURE = Path(__file__).resolve().parents[1] / "backend/tests/evals/fixtures/ppa_runs_v1.json"


def fetch_snapshot(client: httpx.Client, base_url: str, run_id: int, *, page_size: int = 100) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a loopback HTTP AIAC API is allowed")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be between 1 and 100")
    base = base_url.rstrip("/")
    prefix = f"{base}/runs/{run_id}"
    report_response = client.get(f"{prefix}/research-report")
    report_response.raise_for_status()
    report = report_response.json()
    if report.get("run_id") != run_id:
        raise ValueError("Report run ID mismatch")
    candidates = []
    total = None
    for offset in range(0, 100000, page_size):
        response = client.get(f"{prefix}/research-candidates",
                              params={"limit": page_size, "offset": offset})
        response.raise_for_status()
        page = response.json()
        if total is None:
            total = page["total"]
        elif total != page["total"]:
            raise ValueError("Candidate list changed during pagination")
        candidates.extend(page["items"])
        if not page["has_more"]:
            break
    else:
        raise ValueError("Candidate list exceeds snapshot limit")
    if len(candidates) != total or len({row.get("id") for row in candidates}) != total:
        raise ValueError("Candidate list is incomplete or duplicated")
    if report.get("candidate_funnel", {}).get("generated") != total:
        raise ValueError("Research report and candidate page disagree")
    return sanitize_snapshot(report, candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003/api/v1")
    args = parser.parse_args()
    with httpx.Client(timeout=15) as client:
        current = fetch_snapshot(client, args.base_url, args.run_id)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                     prefix=f"aiac-eval-run{args.run_id}-", delete=False) as tmp:
        json.dump({"schema_version": 1, "run": current}, tmp, ensure_ascii=False, indent=2)
        path = tmp.name
    saved = next((run for run in json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]
                  if run["run_id"] == args.run_id), None)
    differences = compare_snapshot(saved, current) if saved else {"run": "no saved fixture"}
    print(json.dumps({"temporary_snapshot": path, "run_id": args.run_id,
                      "differences": differences}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
