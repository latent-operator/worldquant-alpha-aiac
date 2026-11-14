"""Evaluate a saved AIAC experiment report without network or database writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.evals.goal_evidence import evaluate_goal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--claim", choices=["EVIDENCE_INSUFFICIENT", "NOT_PROVEN",
                                             "PASS_OBSERVED", "PPA_READY_OBSERVED"])
    args = parser.parse_args()
    result = evaluate_goal(json.loads(args.report.read_text(encoding="utf-8")),
                           source=args.source, claimed_status=args.claim)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["claim_supported"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
