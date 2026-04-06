# Implementation Process

> Status: ACTIVE
> Created: 2026-04-06

---

## Document Roles (strict separation)

| Doc Type | Location | Role | Lifecycle |
|----------|----------|------|-----------|
| **Proposal** | `docs/dev/proposal-{name}.md` | Exploration, options, trade-offs | Exists only while design is unstable. Deleted when plan is created. |
| **Plan** | `docs/governance/plan-{name}.md` | **Single source of truth**: goals, steps, AC, tests | Lives for duration of project. Status: DRAFT → ACTIVE → COMPLETED. |
| **Execution record** | `docs/dev/current-{name}-{date}.md` | **Progress only**: checkboxes referencing plan ACs. No definitions. | Archived when complete. |
| **README Active Plans** | `docs/governance/README.md` | **Index only**: links to plan + execution record. No rules. | Updated when plans change status. |
| **Graph secondary** | graph.json node secondary | Links plan doc to affected nodes. | Only plan docs, never proposals or execution records. |

**Rule**: If you need the definition of an AC, read the plan. If you need whether it's done, read the execution record. Never both in the same file.

---

## Process Flow

```
1. IDENTIFY need
      ↓
2. DRAFT proposal in docs/dev/proposal-{name}.md
      ↓ iterate until stable
3. CREATE plan in docs/governance/plan-{name}.md
   - Move content from proposal (don't copy)
   - Delete proposal file
   - Add plan as graph secondary on affected nodes
      ↓
4. CREATE execution record docs/dev/current-{name}-{date}.md
   - Checkboxes only, referencing plan AC IDs
      ↓
5. IMPLEMENT level by level
   - Each step: code → test → verify AC → check box in execution record
      ↓
6. COMPLETE
   - Archive execution record
   - Update plan status to COMPLETED
   - Archive stale dev docs
   - Update README Active Plans
```

---

## Naming Conventions

| Pattern | Example |
|---------|---------|
| `docs/dev/proposal-{name}.md` | `proposal-graph-driven-doc.md` (temporary) |
| `docs/governance/plan-{name}.md` | `plan-graph-driven-doc.md` (permanent) |
| `docs/dev/current-{name}-{date}.md` | `current-graph-doc-2026-04-06.md` (until complete) |

---

## Document Archival

### Trigger

Archival is checked when:
1. An execution record is completed
2. At session start (observer reviews `docs/dev/`)

### Rules

Archive operates as **candidate + human confirm**, never auto-delete:

1. **Flag candidates**: Identify files matching archive criteria
2. **Log reason**: One-line explanation per file
3. **Require confirmation**: Observer approves before `mv` to archive

### Archive Criteria

| Condition | Action |
|-----------|--------|
| Execution record completed | Archive to `docs/dev/archive/` |
| Proposal superseded by plan | Delete (content already in plan) |
| Session handoff consumed by next session | Archive |
| File has `governance/` canonical copy | Delete dev/ duplicate |
| Filename contains "archived" | Archive (already self-declared) |

### What Should NOT Be Archived

- `docs/dev/current-*.md` while active
- Living documents without dates (`roadmap.md`, `bug-and-fix-backlog.md`) — update, don't archive
- Design discussion docs that inform future decisions — when in doubt, keep

---

## Relationship to Manual Fix SOP

Manual fixes follow `docs/governance/manual-fix-sop.md` with execution records at `docs/dev/manual-fix-current-*.md`. Same lifecycle as above: governed SOP is the plan, current-* is the execution record.
