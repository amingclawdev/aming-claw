#!/usr/bin/env python3
"""ETL: Parse docs/dev/bug-and-fix-backlog.md and upsert into governance DB.

Usage:
    python scripts/etl-backlog-md-to-db.py                  # dry-run (default)
    python scripts/etl-backlog-md-to-db.py --dry-run        # explicit dry-run
    python scripts/etl-backlog-md-to-db.py --apply          # upsert all bugs
    python scripts/etl-backlog-md-to-db.py --apply --pid X  # custom project id

Idempotent: re-running --apply produces 0 net-new rows.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

# Resolve project root (scripts/ is one level below)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_BACKLOG_PATH = os.path.join(_PROJECT_ROOT, "docs", "dev", "bug-and-fix-backlog.md")


def parse_backlog(md_path: str) -> list:
    """Parse bug-and-fix-backlog.md and extract bug entries.

    Looks for:
    - Fixed bugs table: rows like | B1 | description | commit | date |
    - ### headings with bug ID pattern (B##, D##, G##, O##)
    - chain-trigger HTML comment blocks

    Returns list of dicts with bug_id, title, status, priority, etc.
    """
    if not os.path.exists(md_path):
        print(f"ERROR: Backlog file not found: {md_path}", file=sys.stderr)
        return []

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    bugs = {}

    # --- Phase 1: Parse the Fixed Bugs table ---
    table_pattern = re.compile(
        r'^\|\s*((?:B|D|G|O)\d+(?:/\w+)?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|',
        re.MULTILINE
    )
    for m in table_pattern.finditer(content):
        bug_id = m.group(1).strip()
        title = m.group(2).strip()
        commit = m.group(3).strip()
        date = m.group(4).strip()
        if bug_id in ("ID",):
            continue  # skip header row
        # Determine status from commit field
        status = "FIXED"
        if "(OPEN)" in commit or not commit or commit == "—":
            status = "OPEN"
        bugs[bug_id] = {
            "bug_id": bug_id,
            "title": title,
            "status": status,
            "priority": "P1",
            "commit": commit.strip("~` ") if status == "FIXED" else "",
            "discovered_at": date.strip() if date.strip() != "—" else "",
            "target_files": [],
            "test_files": [],
            "acceptance_criteria": [],
            "details_md": "",
            "chain_trigger_json": {},
        }

    # --- Phase 2: Parse ### heading sections ---
    heading_pattern = re.compile(
        r'^###\s+((?:B|D|G|O)\d+(?:[a-z]?))\s*[:/]\s*(.+?)(?:\s*\[(OPEN|FIXED|WONTFIX)\])?(?:\s*\[(P\d(?:\.\d)?)\])?\s*$',
        re.MULTILINE
    )
    # Split content by ### headings to get section bodies
    sections = re.split(r'^(###\s+.+)$', content, flags=re.MULTILINE)
    for i, section in enumerate(sections):
        hm = heading_pattern.match(section.strip())
        if hm:
            bug_id = hm.group(1).strip()
            title = hm.group(2).strip()
            status = hm.group(3) or "OPEN"
            priority = hm.group(4) or "P3"
            # Get the body (next section)
            body = sections[i + 1] if i + 1 < len(sections) else ""

            if bug_id not in bugs:
                bugs[bug_id] = {
                    "bug_id": bug_id,
                    "title": title,
                    "status": status,
                    "priority": priority,
                    "commit": "",
                    "discovered_at": "",
                    "target_files": [],
                    "test_files": [],
                    "acceptance_criteria": [],
                    "details_md": body.strip()[:2000],
                    "chain_trigger_json": {},
                }
            else:
                # Update existing entry
                if status:
                    bugs[bug_id]["status"] = status
                if priority:
                    bugs[bug_id]["priority"] = priority
                bugs[bug_id]["details_md"] = body.strip()[:2000]

            # Parse chain-trigger block if present
            ct_match = re.search(
                r'<!--\s*chain-trigger:\s*(.*?)-->',
                body, re.DOTALL
            )
            if ct_match:
                ct_text = ct_match.group(1).strip()
                ct_data = {}
                for line in ct_text.split("\n"):
                    line = line.strip()
                    if ":" in line:
                        key, val = line.split(":", 1)
                        key = key.strip()
                        val = val.strip()
                        # Parse YAML-like values
                        if val.startswith("[") and val.endswith("]"):
                            try:
                                val = json.loads(val.replace("'", '"'))
                            except json.JSONDecodeError:
                                pass
                        elif val.startswith('"') and val.endswith('"'):
                            val = val.strip('"')
                        elif val in ("true", "True"):
                            val = True
                        elif val in ("false", "False"):
                            val = False
                        ct_data[key] = val
                bugs[bug_id]["chain_trigger_json"] = ct_data

                # Extract fields from chain-trigger
                if ct_data.get("target_files"):
                    tf = ct_data["target_files"]
                    if isinstance(tf, str):
                        try:
                            tf = json.loads(tf.replace("'", '"'))
                        except Exception:
                            tf = [tf]
                    bugs[bug_id]["target_files"] = tf
                if ct_data.get("test_files"):
                    tef = ct_data["test_files"]
                    if isinstance(tef, str):
                        try:
                            tef = json.loads(tef.replace("'", '"'))
                        except Exception:
                            tef = [tef]
                    bugs[bug_id]["test_files"] = tef
                if ct_data.get("acceptance_criteria"):
                    ac = ct_data["acceptance_criteria"]
                    if isinstance(ac, list):
                        bugs[bug_id]["acceptance_criteria"] = ac
                if ct_data.get("priority"):
                    bugs[bug_id]["priority"] = str(ct_data["priority"])
                if ct_data.get("status"):
                    bugs[bug_id]["status"] = str(ct_data["status"])
                if ct_data.get("bug_id"):
                    # Ensure bug_id from chain-trigger matches
                    pass

    return list(bugs.values())


def upsert_bug(gov_url: str, pid: str, bug: dict) -> dict:
    """POST a single bug to the governance backlog upsert endpoint."""
    url = f"{gov_url}/api/backlog/{pid}/{bug['bug_id']}"
    data = json.dumps(bug, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        return {"error": str(exc), "body": raw}
    except Exception as exc:
        return {"error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="ETL: backlog md → governance DB")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Parse and print without writing to DB (default)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually upsert bugs to governance DB")
    parser.add_argument("--pid", default="aming-claw",
                        help="Project ID (default: aming-claw)")
    parser.add_argument("--gov-url", default=None,
                        help="Governance URL (default: $GOVERNANCE_URL or http://localhost:40000)")
    parser.add_argument("--backlog", default=_BACKLOG_PATH,
                        help="Path to backlog md file")
    args = parser.parse_args()

    gov_url = args.gov_url or os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")

    bugs = parse_backlog(args.backlog)
    if not bugs:
        print("No bugs found in backlog.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(bugs)} bugs from {args.backlog}")

    if args.apply:
        print(f"Upserting {len(bugs)} bugs to {gov_url}/api/backlog/{args.pid}/...")
        ok_count = 0
        err_count = 0
        for bug in bugs:
            result = upsert_bug(gov_url, args.pid, bug)
            if result.get("ok"):
                ok_count += 1
            else:
                err_count += 1
                print(f"  ERROR {bug['bug_id']}: {result}", file=sys.stderr)
        print(f"Done: {ok_count} upserted, {err_count} errors")
    else:
        # Dry-run: print summary
        print("\n--- Dry-run summary ---")
        for bug in bugs[:10]:
            print(f"  {bug['bug_id']:8s} [{bug['status']:7s}] {bug['priority']} {bug['title'][:60]}")
        if len(bugs) > 10:
            print(f"  ... and {len(bugs) - 10} more")
        print(f"\nTotal: {len(bugs)} bugs")
        print("Run with --apply to upsert to governance DB.")


if __name__ == "__main__":
    main()
