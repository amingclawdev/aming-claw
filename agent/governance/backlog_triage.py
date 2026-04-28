"""AI triage gate for backlog inserts. Actions: admit, reject_dup, supersede, merge_into."""
from __future__ import annotations
import json, logging
log = logging.getLogger(__name__)

def _parse_tf(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return []
    return v or []

def triage_backlog_insert(payload: dict, open_rows: list[dict]) -> dict:
    """Classify a new backlog filing against existing OPEN rows.
    Returns dict with keys: action, reason, related_bug_ids, confidence."""
    title = payload.get("title", "")
    tf = _parse_tf(payload.get("target_files", []))
    if not open_rows:
        return {"action": "admit", "reason": "no open rows", "related_bug_ids": [], "confidence": 1.0}
    for row in open_rows:
        rid, rt, rtf = row.get("bug_id", ""), row.get("title", ""), _parse_tf(row.get("target_files", []))
        if title and rt and title.strip().lower() == rt.strip().lower():
            return {"action": "reject_dup", "reason": "duplicate of %s" % rid, "related_bug_ids": [rid], "confidence": 0.9}
        if tf and rtf:
            ov = set(tf) & set(rtf)
            if len(ov) >= len(tf) and len(ov) >= len(rtf):
                return {"action": "supersede", "reason": "supersedes %s" % rid, "related_bug_ids": [rid], "confidence": 0.8}
            if ov and len(ov) >= max(1, len(tf) // 2):
                return {"action": "merge_into", "reason": "merge into %s" % rid, "related_bug_ids": [rid], "confidence": 0.7}
    return {"action": "admit", "reason": "no significant overlap", "related_bug_ids": [], "confidence": 0.8}
