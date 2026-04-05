# Role Documentation

This directory contains the canonical role documentation for the Aming Claw project.

## Role Documents

| File | Role | Description |
|------|------|-------------|
| [coordinator.md](coordinator.md) | Coordinator | Coordinator rules and behavior |
| [pm.md](pm.md) | PM | Project Manager rules and output format |
| [observer.md](observer.md) | Observer | Observer rules and monitoring behavior |
| [dev.md](dev.md) | Dev | Developer agent guide and workflow |
| [tester.md](tester.md) | Tester | Tester agent guide |
| [qa.md](qa.md) | QA | QA agent guide |
| [gatekeeper.md](gatekeeper.md) | Gatekeeper | Gatekeeper role and gate override rules |

## Permission Matrix

| Permission | Observer | Coordinator | PM | Dev | Tester | QA | Gatekeeper |
|-----------|----------|-------------|-----|-----|--------|-----|------------|
| task.create | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| task.claim | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| task.complete | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| node.verify | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| node.baseline | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| gate.override | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

For the full permission schema and YAML migration plan, see [docs/config/role-permissions.md](../config/role-permissions.md).

## Migration Note

These files were moved from `docs/` root on 2026-04-05 as part of Phase 1 documentation restructuring.
Original locations contain redirect stubs pointing here.
