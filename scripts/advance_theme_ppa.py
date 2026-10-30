"""Host-side stage preparation. Import only; resuming a task is a separate command."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.ppa_feedback import prepare_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--cockpit-script", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8003/api/v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.api_base not in {"http://127.0.0.1:8003/api/v1", "http://localhost:8003/api/v1"}:
        raise ValueError("Only the local AIAC API is allowed")
    with urlopen(f"{args.api_base}/runs/{args.run_id}/ppa-feedback", timeout=20) as response:
        evidence = json.load(response)
    batch = prepare_batch(evidence["base"], evidence, args.cockpit_script)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    payload = json.dumps(batch, ensure_ascii=False).encode("utf-8")
    request = Request(f"{args.api_base}/runs/{args.run_id}/ppa-batch", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        print(json.load(response))


if __name__ == "__main__":
    main()
