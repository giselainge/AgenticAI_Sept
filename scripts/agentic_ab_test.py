"""Run the current extraction (Plan A) beside the experimental coded-agent Plan B."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.paths import DEFAULT_AGENTIC_AB_DIR
from llm.agent.workflow import judge_ab_artifact, run_agentic_ab_test
from rag.adaptive_rag import DEFAULT_KB


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A/B test deterministic Plan A against coded-agent Plan B.")
    parser.add_argument("--text-file", required=True, help="Selected OCR text file.")
    parser.add_argument("--pdf-file", help="Canonical invoice PDF. Required for Gemini extraction in Plan B.")
    parser.add_argument("--kb", default=str(DEFAULT_KB), help="Provider-memory JSON path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_AGENTIC_AB_DIR))
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL") or "gemini-3.5-flash")
    parser.add_argument("--judge-base-url", default=os.getenv("JUDGE_BASE_URL", ""))
    parser.add_argument("--judge-model", default=os.getenv("JUDGE_MODEL", ""))
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    try:
        result = run_agentic_ab_test(
            args.text_file,
            pdf_file=args.pdf_file,
            kb_path=args.kb,
            output_dir=args.output_dir,
            api_key=os.getenv("GEMINI_API_KEY", ""),
            model=args.model,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"A/B test could not start: {exc}", file=sys.stderr)
        return 2
    print(f"A/B artifact: {result.artifact_path}")
    print(f"Plan A: {len(result.plan_a.validation_errors)} validation error(s), route={result.plan_a.route}")
    print(
        f"Plan B: {len(result.plan_b.validation_errors)} validation error(s), "
        f"route={result.plan_b.route}, llm_used={str(result.plan_b.llm_used).lower()}"
    )
    if not result.plan_b.llm_used:
        print("Plan B retained deterministic extraction because a PDF/API key was unavailable or the LLM call failed.")
    if args.judge_base_url and args.judge_model:
        judged = judge_ab_artifact(
            result.artifact_path,
            base_url=args.judge_base_url,
            api_key=os.getenv("JUDGE_API_KEY", ""),
            model=args.judge_model,
        )
        assert judged.llm_judge is not None
        print(
            f"LLM judge: status={judged.llm_judge.status}, "
            f"preferred={judged.llm_judge.preferred_plan}, confidence={judged.llm_judge.confidence}"
        )
    print("A reviewer verdict is required to compare accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
