"""Evaluate a saved AIAC experiment report without network or database writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.evals.goal_evidence import evaluate_goal, require_goal_ready


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--require-ready", action="store_true",
                        help="Exit nonzero unless the research goal has complete acceptance evidence")
    parser.add_argument("--claim", choices=["EVIDENCE_INSUFFICIENT", "NOT_PROVEN",
                                             "PASS_OBSERVED", "PPA_READY_OBSERVED"])
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        result = evaluate_goal(report, source=args.source, claimed_status=args.claim)
        if args.require_ready:
            result = require_goal_ready(report, source=args.source)
            result["claim_supported"] = args.claim is None or args.claim == result["status"]
    except (ValueError, OSError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["claim_supported"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
