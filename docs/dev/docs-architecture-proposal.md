# Docs Architecture Proposal — Documentation Governance Framework

> Status: DRAFT v3 | Author: Observer | Date: 2026-04-01
> v2: Codex round 1 (lifecycle, SoT, ownership, acceptance)
> v3: Codex round 2 (canonical granularity, config disambiguation, migration rules, inference fallback)

---

## 0. Governance Boundary Definition

The directory structure encodes **governance domain**, not just content type:

| Path | Governance Domain | Gate Association | Description |
|------|-------------------|------------------|-------------|
| `docs/` | **Governed** | Yes — linked to acceptance graph nodes, subject to gate validation | Canonical documents representing current system truth |
| `docs/dev/` | **Non-governed** | No — explicitly excluded from gate association | Development process artifacts: iterations, proposals, debug logs |
| `docs/dev/archive/` | **Non-governed, historical** | No — intentionally excluded to prevent gate pollution | Superseded documents preserved for design decision history |

**Hard rule**: Any document that is currently valid, affects system behavior, or is used as a reference by agents/humans **must** be in `docs/`. Placing it in `dev/` to avoid gate overhead is a governance violation.

`docs/dev/archive/` is NOT a dumping ground. It stores superseded-but-historically-valuable documents, each with metadata explaining why it was archived and what replaced it.

---

## 1. Document Lifecycle Policy

Every document has exactly one lifecycle state:

| State | Definition | In README? | Gate Associated? | Banner Required? | May Update? |
|-------|-----------|------------|------------------|-----------------|-------------|
| **Draft** | Work in progress, not yet reviewed | No | No (lives in `dev/`) | `> Status: DRAFT` | Yes, freely |
| **Working Note** | Persistent dev artifact (iteration log, debug record) — not expected to become Active | No | No (lives in `dev/`) | None | Yes, freely |
| **Active** | Reviewed, current, maintained. Supports or elaborates on a Canonical doc's topic | Yes | Yes (if in `docs/`) | None | Yes, via chain |
| **Canonical** | Single source of truth for its **topic** — no other doc may contradict it on that topic | Yes (primary link) | Yes | None | Yes, via chain |
| **Deprecated** | Still referenceable but being replaced | Yes (with warning) | Yes (until removed) | `> DEPRECATED: see [replacement]` | Only to add deprecation notice |
| **Archived** | Superseded, historical reference only | No | No (in `dev/archive/`) | Metadata header (see below) | No |

### Canonical Assignment Principle

> **Canonical is assigned per topic, not per directory.**
> A topic may have only one Canonical document. Neighboring overview/reference docs remain Active.

Examples:
- `docs/architecture.md` is **Canonical** for "system architecture"
- `docs/roles/pm.md` is **Canonical** for "PM role behavior"
- `docs/roles/README.md` is **Active** (overview/navigation, defers to individual role docs on specifics)
- `docs/governance/memory.md` is **Canonical** for "memory system design"
- `docs/config/role-permissions.md` is **Active** (documents the YAML schema, but the YAML files themselves are the source of truth for actual values)

When in doubt: the Canonical doc is the one that answers "what is the current truth about X?" Other docs may reference it but must not contradict it.

### Transition Rules

```
Draft → Active       : PR review + gate pass
Draft → Working Note : Explicit decision (this will never become formal)
Active → Canonical   : Designated as single source of truth (explicit decision)
Active → Deprecated  : Replacement doc reaches Active
Deprecated → Archived: Replacement doc reaches Canonical + migration period ends
Any → Archived       : Superseded by newer version
```

### Classification Criteria

| I want to... | It goes in... | State |
|--------------|--------------|-------|
| Write a design proposal before implementation | `docs/dev/` | Draft |
| Document current system architecture | `docs/` | Canonical |
| Document a specific role's behavior | `docs/roles/` | Canonical (for that role's topic) |
| Write an overview linking multiple topics | `docs/roles/README.md` | Active (navigation/overview) |
| Keep a superseded architecture doc for history | `docs/dev/archive/` | Archived |
| Log a debugging session or iteration notes | `docs/dev/` | Working Note |
| Write API reference for current endpoints | `docs/api/` | Canonical |

---

## 2. Source of Truth Priority

When documents, code, and configuration disagree, resolution priority is:

| Domain | Authoritative Source | Fallback | Never Trust |
|--------|---------------------|----------|-------------|
| **Runtime behavior** | Code (`agent/*.py`) | Config (`.aming-claw.yaml`) | Documentation |
| **Architecture intent** | `docs/architecture.md` (Canonical) | Design spec | Old version docs |
| **API interface** | Route implementation (`server.py`) | `docs/api/governance-api.md` | README examples |
| **Role permissions** | `config/roles/*.yaml` (after migration) / `role_permissions.py` (current) | `docs/roles/*.md` | Hardcoded defaults |
| **Port numbers** | `.mcp.json` env + `start_governance.py` | `docs/deployment.md` | README, old arch docs |
| **Navigation** | `README.md` | N/A | N/A (README is navigation only, never a fact source) |

**Rule**: README is a **directory**, not a **database**. It links to canonical docs but never states facts that should live elsewhere.

**Hard constraint**: README may describe scope and purpose ("what this section covers") but **must not contain operational values, behavior rules, or interface facts** (ports, endpoints, config keys, permission lists). These belong in their Canonical docs. README links to them.

If README says "port 40000" and deployment.md says "port 40006", deployment.md is wrong — fix deployment.md. But README should not have stated the port in the first place; it should just link to deployment.md.

---

## 3. Proposed Structure

### Directory Naming Convention

Two directories share the name "config" at different levels — they serve different purposes:

| Path | Type | Contains |
|------|------|----------|
| `config/` (repo root) | **Executable configuration** | YAML files that the system reads at runtime (role configs, memory config) |
| `docs/config/` | **Reference documentation** | Human-readable schema docs explaining how to fill and validate those config files |

Analogy: `config/roles/pm.yaml` is the data; `docs/config/role-permissions.md` is the manual.

```
README.md                          # Navigation entry point (not a fact source)
.aming-claw.yaml                   # Project config (testing, deploy, governance)

docs/
  architecture.md                  # Canonical: system design & component overview
  deployment.md                    # Canonical: host-based setup, ports, services
  onboarding.md                    # Active: new project setup guide

  roles/                           # Role documentation (1 file per role)
    README.md                      # Canonical: permission matrix + role flow diagram
    coordinator.md                 # Active: decision rules, 2-round flow
    pm.md                          # Active: PRD format, turn cap, context injection
    dev.md                         # Active: worktree isolation, tool access
    tester.md                      # Active: test report format, T2 pass criteria
    qa.md                          # Active: criteria verification, recommendation format
    gatekeeper.md                  # Active: isolated review, merge_pass criteria
    observer.md                    # Active: monitoring, takeover, release flow

  governance/                      # Governance system documentation
    auto-chain.md                  # Canonical: PM->Dev->Test->QA->Gatekeeper->Merge
    gates.md                       # Canonical: what each gate checks and blocks on
    memory.md                      # Active: backends, search, write patterns
    conflict-rules.md              # Active: rule engine, dedup, retry logic
    version-control.md             # Active: version gate, sync, chain_version lifecycle
    acceptance-graph.md            # Active: node definitions (moved from docs/ root)

  config/                          # Config file documentation
    README.md                      # Active: config overview + validation rules
    aming-claw-yaml.md             # Active: .aming-claw.yaml schema reference
    mcp-json.md                    # Active: .mcp.json schema reference
    role-permissions.md            # Active: role config schema + YAML migration plan

  api/                             # API reference
    governance-api.md              # Canonical: all /api/* endpoints
    executor-api.md                # Active: executor-specific API

  dev/                             # Non-governed: development process artifacts
    archive/                       # Superseded docs with metadata headers
    *.md                           # Iteration logs, proposals, session handoffs
```

### Boundary between governance/ and config/

| Subdirectory | Contains | Answers |
|-------------|---------|---------|
| `governance/` | Process documentation | "Why does this work this way?" "What's the flow?" |
| `config/` | Schema reference | "What fields exist?" "How do I fill this?" "What validates it?" |

Example: `governance/conflict-rules.md` explains *why* Rule 5 searches pitfalls. `config/role-permissions.md` documents the YAML schema for *configuring* role permissions.

---

## 4. File Classification & Migration

### Migration Action Rules

When classifying a file, apply these rules in order:

| Condition | Action |
|-----------|--------|
| Content spans multiple Canonical topics (e.g. design-spec covers memory + chain + context) | **Split** into per-topic Canonical docs |
| Same topic is explained in 2+ files with overlapping content | **Merge** into one Canonical doc, archive the others |
| Content is at the right topic granularity, just wrong location | **Move** (rename/relocate, leave redirect stub) |
| Content contradicts current system behavior and cannot be patched locally | **Rewrite** (new doc from current code + architecture reality) |
| Content has no current behavioral value but records design decisions | **Archive** with metadata header |
| Content has no current or historical value | **Delete** (rare — prefer archive) |

### Keep at docs/ (current, update content)

| File | Action | New Location | New State |
|------|--------|-------------|-----------|
| design-spec-memory-coordinator-executor.md | **Split** key sections | governance/memory.md + auto-chain.md | Canonical |
| prd-memory-coordinator-executor.md | **Split** into governance/ sections | governance/ | Active |
| aming-claw-acceptance-graph.md | **Move** | governance/acceptance-graph.md | Active |
| coordinator-rules.md | **Move + merge** with guide-coordinator.md | roles/coordinator.md | Active |
| pm-rules.md | **Move** | roles/pm.md | Active |
| observer-rules.md | **Move + merge** with observer guides | roles/observer.md | Active |
| guide-coordinator.md | **Merge into** roles/coordinator.md | (deleted after merge) | — |
| guide-dev-agent.md | **Merge into** roles/dev.md | (deleted after merge) | — |
| guide-tester-qa.md | **Split into** roles/tester.md + qa.md | (deleted after split) | — |
| human-intervention-guide.md | **Merge into** roles/observer.md | (deleted after merge) | — |
| observer-feature-guide.md | **Merge into** roles/observer.md | (deleted after merge) | — |
| ai-agent-integration-guide.md | **Rewrite as** api/governance-api.md | api/governance-api.md | Canonical |
| executor-api-guide.md | **Rewrite as** api/executor-api.md | api/executor-api.md | Active |
| deployment-guide.md | **Rewrite** for host-based | deployment.md | Canonical |

### Archive to docs/dev/archive/ (superseded)

Each archived file gets a metadata header:

```yaml
---
status: archived
superseded_by: docs/architecture.md
archived_date: 2026-04-01
historical_value: "Records the v5 session runtime design decisions and why coordinator.py was replaced"
do_not_use_for: "current deployment, current API, current port assumptions"
---
```

| File | Historical Value | Superseded By |
|------|-----------------|---------------|
| architecture-v3-complete.md | Early system design, plugin model | architecture.md |
| architecture-v4-complete.md | Message delivery reliability design | architecture.md |
| architecture-v5-revised.md | Toolbox feedback integration | architecture.md |
| architecture-v5-runtime.md | Session runtime state service design | architecture.md |
| architecture-v6-executor-driven.md | Executor-driven architecture pivot | architecture.md |
| architecture-v7-context-service.md | Context service + observer SOP | architecture.md |
| workflow-governance-design.md | Original governance concept | governance/auto-chain.md |
| workflow-governance-architecture-v2.md | Governance v2 expansion | governance/auto-chain.md |
| session-runtime-design.md | Old coordinator.py session model | architecture.md |
| scheduled-task-design.md | Unimplemented scheduled task design | (no replacement) |
| telegram-project-binding-design.md | Gateway binding design | deployment.md |
| toolbox-acceptance-graph.md | Different project's graph | (no replacement) |
| p0-3-design.md | Dev->Gatekeeper chain design history | governance/auto-chain.md |
| production-guard.md | Original production deploy guard | deployment.md |

---

## 5. README Redesign — Two-Layer Entry

```markdown
# Aming Claw

> AI Workflow Governance Platform — Observer-driven task execution

## Get Started
- [Architecture](docs/architecture.md) — How the system works
- [Deployment](docs/deployment.md) — Set up and run the platform
- [Onboarding](docs/onboarding.md) — Add a new project

## Deep Dive

### Roles
[Overview & Permissions](docs/roles/README.md) |
[Coordinator](docs/roles/coordinator.md) |
[PM](docs/roles/pm.md) |
[Dev](docs/roles/dev.md) |
[Tester](docs/roles/tester.md) |
[QA](docs/roles/qa.md) |
[Gatekeeper](docs/roles/gatekeeper.md) |
[Observer](docs/roles/observer.md)

### Governance
[Auto-Chain](docs/governance/auto-chain.md) |
[Gates](docs/governance/gates.md) |
[Memory](docs/governance/memory.md) |
[Conflict Rules](docs/governance/conflict-rules.md) |
[Version Control](docs/governance/version-control.md)

### Config & API
[Config Overview](docs/config/README.md) |
[Governance API](docs/api/governance-api.md) |
[Executor API](docs/api/executor-api.md)

### Development
[Dev Notes](docs/dev/) |
[Historical Docs](docs/dev/archive/)
```

Layer 1 ("Get Started") = 3 links for first-time readers.
Layer 2 ("Deep Dive") = full topic index for experienced users.

---

## 6. Source of Truth Priority & Config Governance

### Role Config YAML Migration

**Current**: Hardcoded in Python dicts.
**Target**: YAML files with schema validation.

```yaml
# config/roles/pm.yaml
version: 1                    # Config version (bump on breaking changes)
role: pm
max_turns: 5
tools: [Read, Grep, Glob]
permissions:
  can: [generate_prd, design_nodes, analyze_requirements, query_governance]
  cannot: [modify_code, run_tests, create_dev_task, verify_update]
prompt: |
  You are the project PM (Product Manager / Architect).
  ...
```

### YAML Governance Rules

| Risk | Mitigation |
|------|-----------|
| Missing/invalid fields | **JSON Schema or Pydantic validation** at startup — refuse to run if invalid |
| Prompt drift without tracking | **`version` field** — bump required for prompt changes, enables rollback |
| Runtime reload accidents | **No hot reload** — config loaded at startup only, restart required |
| Project-specific overrides | **Default + override pattern**: `config/roles/default/*.yaml` + `config/roles/{project_id}/*.yaml` |
| Validation gap | **Startup check**: compare YAML values against expected types/ranges, log warnings for unusual values |

### Memory Config Similarly

```yaml
# config/memory.yaml
version: 1
backend: docker
fts5_fallback: true
conflict_rules:
  search_kinds: [failure_pattern, pitfall, decision]
  max_retry_count: 2
  dedup_window_statuses: [queued, claimed, observer_hold]
```

---

## 7. Ownership & Update Triggers

### Document Ownership

| Document Area | Owner | Update Trigger |
|--------------|-------|----------------|
| `docs/architecture.md` | Observer / Architect | Component added/removed, major flow change |
| `docs/deployment.md` | Observer / DevOps | Port change, service migration, env var change |
| `docs/roles/*.md` | Role governance owner | Permission change, prompt change, tool access change |
| `docs/governance/*.md` | Core governance owner | Gate logic change, chain flow change, memory schema change |
| `docs/config/*.md` | Core governance owner | Config schema change, new config field |
| `docs/api/*.md` | API implementor | Route added/removed, request/response schema change |
| `docs/onboarding.md` | Observer | Onboarding workflow change |
| `README.md` | Observer | Only when doc structure changes (new sections, moved files) |

### Mandatory Update Triggers (Code-Doc Sync)

These changes **must** include corresponding doc updates in the same chain:

| Code Change | Required Doc Update |
|-------------|-------------------|
| New/removed API route in `server.py` | `docs/api/governance-api.md` |
| Role permission change in `role_permissions.py` | `docs/roles/{role}.md` + `docs/config/role-permissions.md` |
| Gate logic change in `auto_chain.py` | `docs/governance/gates.md` |
| Port/env var change | `docs/deployment.md` |
| Chain flow change (new stage, removed stage) | `docs/governance/auto-chain.md` |
| Memory schema change | `docs/governance/memory.md` |
| New config field in `.aming-claw.yaml` | `docs/config/aming-claw-yaml.md` |

**Enforcement**: checkpoint gate (`_gate_checkpoint`) already checks `doc_impact` via `get_related_docs()`. Ensure CODE_DOC_MAP in `impact_analyzer.py` maps code files to their canonical docs.

---

## 8. Migration Compatibility Plan

### Redirect Stubs

For high-traffic old file paths, leave a stub:

```markdown
<!-- docs/architecture-v7-context-service.md -->
> This document has been archived.
> Current architecture: [docs/architecture.md](architecture.md)
> Archived version: [docs/dev/archive/architecture-v7-context-service.md](dev/archive/architecture-v7-context-service.md)
```

### Stub Retention Policy
- Keep stubs for **one release cycle** (or 30 days)
- After that, delete stubs (git history preserves the redirect)
- Maintain `docs/dev/migration-map.md` listing all old→new path mappings

### Code/Prompt Reference Scan

Before deleting any doc, grep for its filename in:
- `agent/**/*.py` (code references)
- `agent/role_permissions.py` (prompt references)
- `.claude/` config files
- Other `docs/**/*.md` (cross-references)

---

## 9. Acceptance Criteria

Migration is **complete** when all of the following are true:

| # | Criterion | Verification |
|---|-----------|-------------|
| 1 | README all primary links resolve (no 404) | `scripts/check-doc-links.sh` or manual |
| 2 | No duplicate "current architecture" docs in `docs/` | Only `architecture.md` exists at root level |
| 3 | All archived files have metadata header | `grep -L "^status: archived" docs/dev/archive/*.md` returns empty |
| 4 | Each role has exactly one canonical doc | 7 files in `docs/roles/` (excl README) |
| 5 | All `/api/*` endpoints documented in one place | `docs/api/governance-api.md` covers all routes |
| 6 | Port 40000 is the only canonical port value | `grep -r "40006" docs/` returns only archive/ hits |
| 7 | Search for "PM role" reaches canonical doc in 2 clicks | README → roles/README.md → roles/pm.md |
| 8 | No governed doc references old file paths | `grep -r "architecture-v[3-7]" docs/*.md docs/roles/ docs/governance/` returns empty |
| 9 | `docs/dev/` contains no documents that should be governed | Manual review: no active system docs in dev/ |
| 10 | CODE_DOC_MAP updated for new paths | `impact_analyzer.py` maps to new doc locations |

---

## 10. Implementation Phases

### Phase 1: Archive & Restructure (mechanical, low risk)
- Create `docs/dev/archive/`, `docs/roles/`, `docs/governance/`, `docs/config/`, `docs/api/`
- Move 14 files to `docs/dev/archive/` with metadata headers
- Move role docs to `docs/roles/`
- Leave redirect stubs at old locations
- Update `docs/dev/migration-map.md`

### Phase 2: Content Consolidation (requires reading + rewriting)
- Write `docs/architecture.md` from v7 + current code
- Rewrite `docs/deployment.md` for host-based (40000)
- Merge guide-*.md into role docs
- Write `docs/governance/auto-chain.md` + `gates.md` from design-spec + code
- Verify acceptance criteria 1-6

### Phase 3: README + Config + Linkage
- Rewrite `README.md` (two-layer entry)
- Write `docs/config/` schema references
- Update CODE_DOC_MAP in `impact_analyzer.py`
- Delete redirect stubs (after 30 days or next release)
- Verify acceptance criteria 7-10

### Phase 4: YAML Migration + Onboarding (future)
- Write `docs/onboarding.md`
- Implement YAML role config loader with Pydantic validation
- Migrate `role_permissions.py` → `config/roles/*.yaml`
- Startup validation: refuse to run on invalid YAML
- Acceptance test: YAML values match current Python behavior

---

## 11. Multi-Project Documentation Architecture

### Problem

When aming-claw governs an external project (e.g. "toolbox"), that project has its own `docs/` directory with its own content. We cannot:
- Pollute the external project's `docs/` with aming-claw governance files
- Keep all governance docs centralized in aming-claw (gate needs to validate within each project's repo)
- Assume external projects follow our directory conventions

### Design: `.aming-claw/` + Optional Doc Governance

每个接入项目在 repo 根目录放 `.aming-claw/`（最小集）:

```
external-project/
  docs/                          # Project's own docs (project team maintains)
  src/                           # Project's own code
  .aming-claw/                   # Governance entry point (required)
    project.yaml                 # Project config — includes doc_governance flag
    acceptance-graph.md          # Acceptance graph (required)
    config/                      # Optional: role/memory overrides
      roles/                     # Override default role configs
```

### Doc Governance is User's Choice

在 `project.yaml` 中声明：

```yaml
# .aming-claw/project.yaml
project_id: "toolbox"
doc_governance:
  enabled: true                  # false = gate skips all doc checks
  doc_root: "docs/"              # Which directory to associate with nodes
  code_doc_map:                  # Code file → doc file mapping (optional)
    "src/api/":  "docs/api.md"
    "src/auth/": "docs/auth.md"
```

| `doc_governance.enabled` | Gate 行为 |
|--------------------------|----------|
| `false` | `_gate_checkpoint` 完全跳过文档检查，等同于全局 `skip_doc_check=true` |
| `true` | Gate 根据 `code_doc_map` 或自动推断检查关联文档是否更新 |

**核心原则：文档关联的是项目自己的 `docs/`，不是另建目录。** `.aming-claw/` 只放治理配置（project.yaml、graph、role override），不放文档内容。

### Separation of Concerns

| Scope | Location | Owned By |
|-------|----------|----------|
| 治理平台通用文档 | `aming-claw/docs/` | 平台团队 |
| 治理规则/流程（通用） | `aming-claw/docs/governance/` | 平台团队 |
| 默认角色配置 | `aming-claw/config/roles/default/` | 平台团队 |
| 项目验收图 | `{project}/.aming-claw/acceptance-graph.md` | 项目团队 + Observer |
| 项目角色覆盖 | `{project}/.aming-claw/config/roles/` | 项目团队 |
| 项目自身文档 | `{project}/docs/` | 项目团队（doc_governance=true 时 gate 关联） |

### Config Inheritance

```
aming-claw/config/roles/default/pm.yaml     ← 平台默认值
  ↓ (inherits)
toolbox/.aming-claw/config/roles/pm.yaml     ← 项目覆盖（可选）
  ↓ (merged at runtime)
Final PM config for "toolbox" project
```

Override rules:
- 项目 YAML 可覆盖: `max_turns`, `tools`, `permissions`
- 项目 YAML **不可**覆盖: `role` name, core safety constraints
- 无项目 YAML = 完全使用平台默认值
- 两层都需要 schema validation

### Gate Doc Check Flow

```
_gate_checkpoint(changed_files):
  1. Read project.yaml → doc_governance.enabled?
     ├─ false → skip doc check entirely, return True
     └─ true →
  2. code_doc_map provided?
     ├─ yes → use explicit mapping to find expected docs
     └─ no  → use auto-inference (impact_analyzer.get_related_docs)
  3. Check if expected docs were updated in changed_files
     ├─ all updated → pass
     └─ missing → block with "Related docs not updated: [...]"
```

### Auto-Inference Fallback Strategy

When `code_doc_map` is not provided and `doc_governance.enabled=true`, the gate uses `impact_analyzer.get_related_docs()` to auto-infer which docs should be updated. This inference can be inaccurate:

| Situation | Fallback |
|-----------|----------|
| Code maps to 0 docs (no inference match) | **Pass with warning** — log "no doc mapping found for {files}" |
| Code maps to 2+ docs (ambiguous) | **Pass with warning** — log ambiguous mapping, suggest adding `code_doc_map` |
| Inferred doc doesn't exist | **Pass with warning** — stale mapping, don't block on missing file |
| Confidence below threshold | **Pass with warning** — only block when mapping is unambiguous |

**Principle**: auto-inference **warns**, explicit `code_doc_map` **blocks**. Projects that want hard doc enforcement must provide explicit mappings. Projects without mappings get soft reminders only.

This prevents false blocks during multi-project onboarding while still nudging teams toward doc maintenance.

### Onboarding a New External Project

```
1. Create {project}/.aming-claw/project.yaml
   - project_id, test commands
   - doc_governance: enabled true/false
   - Optional: code_doc_map
2. Create {project}/.aming-claw/acceptance-graph.md
3. Optional: role overrides in {project}/.aming-claw/config/roles/
4. Import graph: POST /api/wf/{project_id}/import-graph
5. First PM task to bootstrap the chain
6. Verify: version-check returns ok=true
```

### aming-claw 自治理（Self-Governing）

aming-claw 既是平台又是受治理项目，结构特殊：

```
aming-claw/
  .aming-claw.yaml              # 自治理配置（existing, doc_governance=true）
  docs/                          # 平台文档 + 自治理文档（合并，gate 关联）
    governance/                  # 通用治理规则
    roles/                       # 通用角色文档
    config/                      # 通用配置文档
  config/                        # 平台默认配置
    roles/default/               # 默认角色 YAML
```

外接项目不需要完整的 `docs/governance/` 结构——只需 `.aming-claw/` 治理入口 + 项目自己的 `docs/`（如果开启 doc_governance）。

---

## 12. Port/Architecture Updates (bundled with Phase 2-3)

All docs created/rewritten will use:
- Port 40000 (governance on host, not Docker 40006)
- Host-based architecture (not Docker-centric)
- Current component list (no executor-gateway, no old coordinator.py)
- Correct `.mcp.json` env vars (GOVERNANCE_URL, MEMORY_BACKEND=docker)
