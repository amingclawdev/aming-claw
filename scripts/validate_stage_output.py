"""CLI helper for stage-output preflight validation (PR1).

Usage:
    python scripts/validate_stage_output.py \\
        --stage=dev \\
        --input=/path/to/dev-output.json \\
        --context=/path/to/chain-context-snapshot.json

Exit codes:
    0 — payload valid
    1 — validation errors present
    2 — argparse / IO failure
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_json(path: str) -> dict:
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_stage_output",
        description="Preflight-validate a stage output payload.",
    )
    parser.add_argument("--stage", required=True, choices=("dev",),
                        help="stage name (PR1 supports 'dev' only)")
    parser.add_argument("--input", required=True,
                        help="path to JSON file with the stage's result payload")
    parser.add_argument("--context", required=False, default=None,
                        help="path to JSON file with chain-context snapshot")
    parser.add_argument("--mode", default="warn",
                        choices=("strict", "warn", "disabled"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits with code 2 on bad args; honour that contract.
        return int(e.code) if isinstance(e.code, int) else 2

    try:
        payload = _load_json(args.input)
        chain_context = _load_json(args.context) if args.context else None
    except (OSError, json.JSONDecodeError) as e:
        print(f"validate_stage_output: cannot read input: {e}", file=sys.stderr)
        return 2

    # Lazy import to keep --help fast.
    from agent.governance.output_schemas import validate_dev_output

    if args.stage != "dev":
        print(f"unsupported stage: {args.stage}", file=sys.stderr)
        return 2

    result = validate_dev_output(payload, chain_context, mode=args.mode)
    print(result.to_human_readable())
    print(json.dumps(result.to_machine_json(), ensure_ascii=False), file=sys.stderr)
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
