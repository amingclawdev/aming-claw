"""Tests for the minimal JavaScript/TypeScript graph adapter."""
from __future__ import annotations

from agent.governance.language_adapters import JavaScriptTypescriptAdapter


def test_js_ts_adapter_extracts_imports_symbols_and_api_relations():
    adapter = JavaScriptTypescriptAdapter()
    source = """
import React from 'react';
import { fetchNodes as loadNodes, saveNode } from './api/client';
import * as utils from "../lib/utils";
import './styles.css';
export { Dashboard } from './Dashboard';
const { createThing } = require('./legacy');

export class Dashboard {}
export default function App() {
  return null;
}
export function renderDashboard() {
  return fetch('/api/graph-governance/aming-claw/status');
}
export const submit = async () => axios.post("/api/graph-governance/aming-claw/query", {});
export const api = {
  health(signal?: AbortSignal) {
    return getJSON<HealthResponse>("/api/health", signal);
  },
  nodes(snapshotId: string) {
    return getJSON(`/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/nodes`);
  },
};
await http("GET", `/api/graph-governance/${PROJECT_ID}/operations/queue`);
await http("GET", p.path);
const path =
  `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}` +
  `/edges?limit=${limit}`;
return getJSON(path, signal);
function scopedPaths() {
  const path = `/api/first`;
  getJSON(path);
  {
    const path = `/api/second`;
    getJSON(path);
  }
}
"""

    assert adapter.supports("web/src/App.tsx")
    assert adapter.supports("web/scripts/e2e-semantic.mjs")
    assert not adapter.supports("web/src/style.css")
    assert not adapter.supports("web/src/vite-env.d.ts")
    assert adapter.classify_file("web/src/App.tsx") == {
        "file_kind": "source",
        "language": "typescript",
        "adapter": "javascript_typescript",
    }
    assert adapter.find_test_pairing("web/src/App.tsx") == "web/src/App.test.tsx"

    imports = adapter.parse_imports("web/src/App.tsx", source)
    pairs = {(row["local"], row["specifier"], row["kind"]) for row in imports}
    assert ("React", "react", "import") in pairs
    assert ("loadNodes", "./api/client", "import") in pairs
    assert ("saveNode", "./api/client", "import") in pairs
    assert ("utils", "../lib/utils", "import") in pairs
    assert ("./styles.css", "./styles.css", "side_effect_import") in pairs
    assert ("./Dashboard", "./Dashboard", "export_from") in pairs
    assert ("createThing", "./legacy", "require") in pairs

    symbols = adapter.parse_symbols("web/src/App.tsx", source)
    assert any(row["name"] == "Dashboard" and row["kind"] == "class" for row in symbols)
    assert any(row["name"] == "App" and row["kind"] == "function" for row in symbols)
    assert any(row["name"] == "renderDashboard" and row["kind"] == "function" for row in symbols)
    assert any(row["name"] == "submit" and row["kind"] == "function" for row in symbols)
    assert any(row["name"] == "api.health" and row["kind"] == "function" for row in symbols)
    assert any(row["name"] == "api.nodes" and row["kind"] == "function" for row in symbols)

    relations = adapter.extract_relations("web/src/App.tsx", source)
    triples = {(row["relation_type"], row["target"], row["target_kind"]) for row in relations}
    assert ("calls_api", "/api/graph-governance/aming-claw/status", "interface") in triples
    assert ("calls_api", "/api/graph-governance/aming-claw/query", "interface") in triples
    assert ("calls_api", "/api/health", "interface") in triples
    assert (
        "calls_api",
        "/api/graph-governance/{expr}/snapshots/{expr}/nodes",
        "interface",
    ) in triples
    assert (
        "calls_api",
        "/api/graph-governance/{expr}/operations/queue",
        "interface",
    ) in triples
    assert (
        "calls_api",
        "/api/graph-governance/{expr}/snapshots/{expr}/edges?limit={expr}",
        "interface",
    ) in triples
    assert ("calls_api", "/api/first", "interface") in triples
    assert ("calls_api", "/api/second", "interface") in triples
    assert all("p.path" not in row["target"] for row in relations)
