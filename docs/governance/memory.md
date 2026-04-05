# Memory System

> **Canonical governance topic document** — Memory backend architecture, schema, and search patterns.
> Last updated: 2026-04-05 | Phase 2 Documentation Consolidation

## Overview

The Aming Claw memory system provides persistent, searchable storage for project knowledge, decisions, failure patterns, and task history. It uses SQLite as the source of truth with FTS5 full-text search, and supports pluggable backends for semantic search.

## Schema v8

### Tables

#### `memories` (Primary Table)

| Column | Type | Description |
|--------|------|-------------|
| memory_id | TEXT PK | Unique identifier (never reused) |
| ref_id | TEXT | Stable semantic anchor for entity mapping |
| entity_id | TEXT | Business object PK (task_id, node_id, etc.) |
| kind | TEXT | Memory type (see Kind Enumeration) |
| scope | TEXT | Project isolation scope |
| module | TEXT | Module/component tag |
| content | TEXT | Memory content (searchable) |
| version | INTEGER | Version chain counter |
| status | TEXT | active / archived / deleted |
| confidence | REAL | Confidence score (0.0–1.0) |
| ttl | INTEGER | Time-to-live in seconds (0 = permanent) |
| durability | TEXT | permanent / durable / ephemeral |
| conflict_policy | TEXT | replace / append / reject |
| write_class | TEXT | Write classification |
| index_status | TEXT | FTS5 index sync status |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

#### `memories_fts` (FTS5 Virtual Table)

Full-text search index over `memories.content`:
```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, kind, module, scope,
    content='memories', content_rowid='rowid'
);
```

#### `memory_relations` (Graph Edges)

| Column | Type | Description |
|--------|------|-------------|
| source_ref_id | TEXT | Source ref_id |
| target_ref_id | TEXT | Target ref_id |
| relation_type | TEXT | PRODUCED, CAUSED_BY, DEPENDS_ON, etc. |
| created_at | TEXT | ISO timestamp |

#### `memory_events` (Event Log)

Append-only event log for all memory operations:
| Column | Type | Description |
|--------|------|-------------|
| event_id | TEXT PK | Unique event identifier |
| memory_id | TEXT | Associated memory |
| event_type | TEXT | write, update, delete, promote, relate |
| payload | TEXT | JSON event payload |
| created_at | TEXT | ISO timestamp |

## Kind Enumeration

| Kind | Durability | Conflict Policy | Description |
|------|-----------|-----------------|-------------|
| fact | permanent | replace | Verified facts |
| summary | durable | replace | Session/topic summaries |
| decision | permanent | append | Design decisions and rationale |
| failure_pattern | permanent | append | Known failure patterns |
| task_result | durable | replace | Task execution results |
| task_snapshot | ephemeral | replace | Task state snapshots |
| module_note | durable | append | Module-specific notes |
| rule | permanent | replace | Governance rules |
| audit_event | permanent | append | Audit trail events |
| architecture | permanent | replace | Architecture decisions |
| pattern | permanent | replace | Code/workflow patterns |

## Core Concepts

### ref_id (Semantic Anchor)

The `ref_id` is the most important concept in the memory system. It provides a stable identifier that maps recall results back to SQLite entities:

- **entity_id ↔ ref_id**: 1:1 mapping (existing business object to semantic anchor)
- **ref_id ↔ memory_id**: 1:N mapping (one anchor, many version-chained memories)

### Recall vs Read Separation

The memory system enforces a strict flow:
```
Query → Semantic Recall → ref_id list → SQLite fetch full object → AI decides
```

Never: `Query → Vector snippets → AI guesses`

This ensures AI decisions are based on authoritative SQLite data, not fuzzy vector approximations.

### Scope System

Multi-project isolation via the `scope` field:
- **Project-specific**: Memories scoped to one project
- **Global**: Shared across all projects
- **Promote API**: Copies a memory from project scope to global scope

Query resolution merges project-scope + global results, with project-scope winning on conflict.

## Backends

The `MEMORY_BACKEND` environment variable selects the backend:

### Local Backend (default)

```bash
export MEMORY_BACKEND="local"
```

- SQLite + FTS5 on host
- No external dependencies
- Full-text search via FTS5 `MATCH` queries
- Suitable for single-machine development

### Docker Backend

```bash
export MEMORY_BACKEND="docker"
```

- Uses dbservice (port 40002) for semantic search with embeddings
- Falls back to local FTS5 if dbservice is unavailable
- Requires: `docker compose up -d dbservice`
- Domain pack registration required after each restart

### Cloud Backend (stub)

```bash
export MEMORY_BACKEND="cloud"
```

- Cloud-hosted memory service
- Not yet implemented (stub interface)

## API Endpoints

All memory APIs are served by the governance service at `http://localhost:40000`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/mem/{pid}/write` | POST | Write a memory record |
| `/api/mem/{pid}/query` | GET | Query memories (filter by module, kind) |
| `/api/mem/{pid}/search` | GET | Full-text search (FTS5 or semantic) |
| `/api/mem/{pid}/relate` | POST | Create relation between ref_ids |
| `/api/mem/{pid}/expand` | GET | Expand ref_id graph (find related memories) |
| `/api/mem/{pid}/promote` | POST | Promote memory from project to global scope |
| `/api/mem/{pid}/register-pack` | POST | Register domain knowledge pack |

## Search Patterns

### FTS5 Search

```bash
# Basic search
curl "http://localhost:40000/api/mem/aming-claw/search?q=version+gate&top_k=5"

# Search with module filter
curl "http://localhost:40000/api/mem/aming-claw/query?module=governance&kind=failure_pattern"
```

### Coordinator Memory Flow

1. **Round 1**: Coordinator receives user message (no memories yet)
2. **query_memory**: Coordinator specifies search queries (up to 3)
3. **Executor searches**: dbservice semantic (primary) → FTS5 (fallback), top_k=3, deduped
4. **Round 2**: Coordinator sees results, makes final decision

### Memory Write Patterns

```json
POST /api/mem/aming-claw/write
{
  "kind": "failure_pattern",
  "module": "auto_chain",
  "content": "Gate blocks when .claude/settings.local.json is dirty",
  "scope": "aming-claw"
}
```

## Implementation

Key file: `agent/governance/memory_backend.py`

Classes:
- `MemoryBackend` — Abstract base class
- `LocalBackend` — SQLite + FTS5 implementation
- `DockerBackend` — dbservice semantic search with FTS5 fallback
- `CloudBackend` — Cloud-hosted stub

## Invariants

1. **ref_id stability**: Once assigned, a ref_id never changes for a given entity
2. **SQLite is truth**: Semantic layers help find objects; they never serve as final state
3. **Compensable index failures**: FTS5 index failure preserves the primary record with async retry
4. **Scope isolation**: Project memories never leak to other projects without explicit promote
