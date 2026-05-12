from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _run_mcp_probe(messages: list[dict]) -> tuple[list[dict], str, int]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent.mcp.server",
            "--project",
            "aming-claw",
            "--workers",
            "0",
            "--governance-url",
            "http://127.0.0.1:9",
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    for message in messages:
        proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.close()
    stdout = proc.stdout.read()
    stderr = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait(timeout=10)
    responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return responses, stderr, returncode


def test_mcp_stdio_initialize_and_health_survive_missing_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        },
    ])

    assert returncode == 0
    assert stderr == ""
    assert responses[0]["result"]["serverInfo"]["name"] == "aming-claw"
    text = responses[1]["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert "error" in payload


def test_mcp_stdio_tools_list_does_not_require_redis_or_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    names = {tool["name"] for tool in responses[0]["result"]["tools"]}
    assert {"health", "manager_health", "graph_query", "backlog_upsert"}.issubset(names)
