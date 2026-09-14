"""Print a non-secret runtime report and optionally fail when OCR is not ready."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.runtime import build_runtime_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local/container OCR runtime parity.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero unless every readiness check passes.")
    parser.add_argument("--fingerprint-only", action="store_true")
    parser.add_argument("--expect-fingerprint", default="")
    args = parser.parse_args()

    report = build_runtime_report(check_writes=True)
    if args.fingerprint_only:
        print(report["quality_fingerprint"])
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))

    fingerprint_matches = not args.expect_fingerprint or report["quality_fingerprint"] == args.expect_fingerprint
    if not fingerprint_matches:
        return 2
    if args.strict and report["status"] != "ready":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
