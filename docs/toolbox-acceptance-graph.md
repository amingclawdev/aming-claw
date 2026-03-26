---
name: acceptance-graph
description: Project Acceptance Graph (Verification Topology) v3 — Dependency layered topology (L0-L4), dual status (impl/verify), gate_mode auto/explicit, test_coverage, critical_file upgrade, failure strategy, propagation strategy
type: reference
version: v3.0
---

# Project Acceptance Graph (Verification Topology) v3

## Maintenance Rules

1. **PM**: PRD must specify new node layer, dependencies, gates, verify, gate_mode, test_coverage and file mapping, format:
   ```
   [TREE:ADD] Lx > node_name | deps:[Lx.y] | gate_mode:auto | verify:Lx | test_coverage:none | primary:[file1] | secondary:[file2] | test:[file3]
   ```
   - When `gate_mode:auto`, no need to write gates (auto-derived by script)
   - When `gate_mode:explicit`, must manually write `gates:[Lx.y]`
2. **Tester**: Generate unit tests + E2E test cases based on new leaf nodes, update `test:[]` field and `test_coverage`
3. **QA**: During acceptance, must verify to each new leaf node's `verify` depth, all leaf nodes green = PASS
4. **Coordinator**: Update node status (build_status + verify_status) + version number after task completion
5. **Regression**: Before each build, pre-dist checks GUARD sentinel nodes' corresponding code guards
6. **Layer rule**: Node layer = max(all deps node layers) + 1; no dependencies = L0
7. **gates rule**: When nodes in gates have verify_status not pass, this node acceptance is SKIP (not executed), overall marked FAIL
8. **verify rule**: Marks the minimum verification depth for this node acceptance (L1-L5), QA cannot downgrade
9. **File mapping three states**:
   - `primary:[]` — Core files directly defining this capability, diff hit requires accepting this node
   - `secondary:[]` — Passively consumed/forwarded files, only included when associated primary nodes are also affected
   - `test:[]` — Test files covering this node
   - `[TBD]` = Mapping to be added, `[]` = Explicitly unrelated

### verify Assignment Rules

| Node Type | verify Minimum | Description | Test Level |
|---------|------------|------|---------|
| Pure config/code existence | L1 | Code exists | T1 unit test verifiable |
| Service layer/API | L2 | API callable | T2 API integration test verifiable |
| UI display | L3 | UI visible | T3 E2E verify |
| Core main flow (search/AI dialog/data isolation) | L4 | End-to-end | T3 E2E verify |
| External systems (Indeed/LinkedIn/JobBank Login) | L5 | Real third-party | T3/T4 E2E verify |

**Per-task acceptance (T1+T2) can reach `verify:T2-pass` nodes**: L1, L2 types
**Pre-release acceptance (T3 E2E) required to reach `verify:pass` nodes**: L3, L4, L5 types

### Gates Auto-Derivation Rules

- Default `gate_mode: auto`: Node gates automatically equal its deps with verify >= L3
- When fine-grained control is needed, switch to `gate_mode: explicit` and manually write gates
- During script validation, auto mode gates are recalculated by script based on deps and compared

## Status Description

### build_status (Implementation Status)

| Value | Meaning |
|----|------|
| impl:done | Implementation complete |
| impl:partial | Partially implemented |
| impl:missing | Not implemented |

### verify_status (Acceptance Status)

| Value | Meaning | Allowed Operations |
|----|------|---------|
| verify:pass | E2E full process acceptance passed | Can release |
| verify:T2-pass | Unit+API tests passed (no E2E) | Can merge, cannot release |
| verify:fail | Acceptance failed (known bug) | Must fix |
| verify:pending | Pending verification | — |
| verify:skipped | Skipped by GATE | Wait for upstream resolution |

**Layered acceptance rules**:
- After each `-coord` task completion: Tester runs T1+T2 (unit+API), nodes marked `verify:T2-pass` after passing
- Before version release (`-coord release`): QA runs T3 E2E (real environment), nodes upgraded to `verify:pass` after passing
- `verify:T2-pass` sufficient for merge and continued development, but **not sufficient for release**
- Release GATE (G5 Strict) requires all nodes `verify:pass`, `T2-pass` does not satisfy

### test_coverage (Test Coverage)

| Value | Meaning |
|----|------|
| none | No test coverage |
| partial | Has unit tests but no E2E |
| strong | Has unit + E2E or L4+ verification evidence |

### Other Markers

| Marker | Meaning |
|------|------|
| GUARD | Critical sentinel (pre-dist auto-check) |

## Layer Definitions

| Layer | Name | Meaning | Dependencies |
|------|------|------|------|
| **L0** | Infrastructure Layer | No external dependencies, lowest layer for system startup and packaging | None |
| **L1** | Service Layer | Depends on L0 runtime environment, provides core service capabilities | L0 |
| **L2** | Capability Layer | Depends on L0+L1, combines services for specific business capabilities | L0, L1 |
| **L3** | Scenario Layer | Depends on L0+L1+L2, complete user scenarios and workflows | L0, L1, L2 |
| **L4** | Presentation Layer | Depends on all lower layers, UI display and user interaction | L0, L1, L2, L3 |
| **L5** | stateService HTTP + SSE | stateService HTTP CRUD + SSE broadcast, cross-layer integration | L0, L1, L4 |

## Node Format Description

```
Lx.y  Node Name  [build_status] [verify_status] version [GUARD]
      deps:[dependency nodes]  — Functional dependency (this node requires these nodes working)
      gate_mode: auto|explicit
      gates:[acceptance prerequisites] — Only needed in explicit mode (auto mode derived by script)
      verify: Lx              — Minimum verification depth
      test_coverage: none|partial|strong
      propagation: smoke_ui   — Connection-type nodes only, suggest adding UI smoke when hit
      primary:[core files]    — diff hit → must verify
      secondary:[aux files]   — diff hit → only included in --full mode
      test:[test files]       — Tests covering this node
```

## Tree Split Rules

Currently a single graph. Split timing:
- When service is independently deployed (e.g., notifyService) → split into independent subgraph
- When adding Agent templates (not just job-seek) → split into Agent subgraph
- Subgraphs mark dependencies on main graph nodes via deps

---

## L0 — Infrastructure Layer (No Dependencies)

```
L0.1  Electron Main Window Load  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js, preload.js]
      secondary:[client/src/index.js]
      test:[TBD]

L0.2  env Passed to server fork  [impl:done] [verify:pass] v1.4.1 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js]
      secondary:[]
      test:[TBD]

L0.3  First Launch npm install (toolService/dbservice)  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[electron.js]
      secondary:[toolService/package.json, dbservice/package.json]
      test:[TBD]

L0.4  Initialization Intercept Page Display  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[electron.js]
      secondary:[client/src/index.js]
      test:[TBD]

L0.5  Express Port Assignment  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[server/server.js, server/services/webSocketService.js]
      secondary:[config.js]
      test:[server/services/webSocketService.test.js]

L0.6  WebSocket Service Start  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[server/server.js, server/services/webSocketService.js]
      secondary:[]
      test:[server/services/webSocketService.test.js]

L0.7  API Route Registration  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      propagation: smoke_ui
      primary:[server/router.js, server/server.js]
      secondary:[]
      test:[TBD]

L0.8  client/build Included in asar  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[package.json, scripts/pre-dist.js]
      secondary:[]
      test:[TBD]

L0.9  pre-dist Check Passed  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[scripts/pre-dist.js]
      secondary:[]
      test:[TBD]

L0.10 Install Directory Has No User Data  [impl:done] [verify:pass] v1.4.2 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[config.js, electron.js]
      secondary:[]
      test:[TBD]

L0.11 toolService/dbservice node_modules Deferred Install  [impl:done] [verify:pass] v1.4.2
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[electron.js]
      secondary:[toolService/package.json, dbservice/package.json]
      test:[TBD]

L0.12 Default savePath Auto-Created  [impl:done] [verify:pass] v1.4.3
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: none
      primary:[config.js]
      secondary:[]
      test:[TBD]

L0.13 NeDB CRUD (wallet/fingerPrint/task)  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[config.js]
      secondary:[server/services/walletService.js, server/services/fingerPrintService.js, server/services/taskService.js]
      test:[server/services/walletService.test.js, server/services/fingerPrintService.test.js, server/services/taskService.test.js]

L0.14 Chromium Installation  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: partial
      primary:[server/services/fingerPrintService.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js]

L0.15 Fingerprint Config Generation  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js]

L0.16 Environment List CRUD  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js, server/router.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js, client/src/pages/ChromeManager/index.test.js]

L0.17 Sidebar Navigation  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/Layout/index.js, client/src/router.js]
      secondary:[]
      test:[client/src/Layout/index.test.js]

L0.18 Responsive Layout (900px Breakpoint)  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/index.scss, client/src/Layout/index.js]
      secondary:[]
      test:[client/src/Layout/index.test.js]

L0.19 Unified Card Width 1400px  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[client/src/index.scss]
      secondary:[]
      test:[TBD]

L0.20 Chinese i18n  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[client/src/i18n.js, client/src/utils/languages/]
      secondary:[]
      test:[TBD]

L0.21 English i18n  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[client/src/i18n.js, client/src/utils/languages/]
      secondary:[]
      test:[TBD]
```

## L1 — Service Layer (Depends on L0)

```
L1.1  dbservice Start (memoryService)  [impl:done] [verify:pass] v1.3
      deps:[L0.2, L0.12]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/memoryService.js, dbservice/index.js, dbservice/lib/knowledgeStore.js]
      secondary:[]
      test:[server/services/memoryService.test.js]

L1.2  toolService Start  [impl:done] [verify:pass] v1.3
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/toolServiceManager.js, toolService/index.js]
      secondary:[]
      test:[server/services/toolServiceManager.test.js]

L1.3  toolService Health Check  [impl:done] [verify:pass] v1.3
      deps:[L1.2]
      gate_mode: explicit
      gates:[L1.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/toolServiceManager.js]
      secondary:[]
      test:[server/services/toolServiceManager.test.js]

L1.4  Browser Environment Launch  [impl:done] [verify:pass] v1.0
      deps:[L0.14, L0.15, L0.16]
      gate_mode: explicit
      gates:[L0.14, L0.16]
      verify: L2
      test_coverage: partial
      primary:[server/services/fingerPrintService.js, assets/agents/job-seek/lib/core/browserLauncher.js]
      secondary:[]
      test:[server/services/fingerPrintService.test.js, assets/agents/job-seek/lib/core/browserLauncher.test.js]

L1.5  NeDB Reconnect After savePath Switch  [impl:done] [verify:pass] v1.4.0
      deps:[L0.12, L0.13]
      gate_mode: explicit
      gates:[L0.12]
      verify: L2
      test_coverage: partial
      primary:[config.js, server/services/stateService.js]
      secondary:[]
      test:[server/services/stateService.test.js, server/routes/stateRoutes.test.js]

L1.6  knowledge.db SQLite Read/Write  [impl:done] [verify:pass] v1.3
      deps:[L1.1]
      gate_mode: explicit
      gates:[L1.1]
      verify: L2
      test_coverage: partial
      primary:[dbservice/lib/knowledgeStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/core/knowledgeClient.test.js]

L1.7  knowledge.db Stored in savePath/db/  [impl:done] [verify:pass] v1.3
      deps:[L0.12, L1.1]
      gate_mode: explicit
      gates:[L1.1]
      verify: L2
      test_coverage: none
      primary:[dbservice/lib/knowledgeStore.js, config.js]
      secondary:[]
      test:[TBD]

L1.8  dbservice Restart on savePath Switch  [impl:done] [verify:pass] v1.4.3 GUARD
      deps:[L1.1, L1.5]
      gate_mode: explicit
      gates:[L1.1, L1.5]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js, config.js]
      secondary:[]
      test:[server/services/memoryService.test.js]

L1.9  WebSocket Client (Auto-Reconnect + Heartbeat)  [impl:done] [verify:pass] v1.0
      deps:[L0.6]
      gate_mode: explicit
      gates:[L0.6]
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[client/src/utils/webSocket.js]
      secondary:[]
      test:[client/src/utils/webSocket.test.js]

L1.10 API Client (Axios Wrapper)  [impl:done] [verify:pass] v1.0
      deps:[L0.7]
      gate_mode: explicit
      gates:[L0.7]
      verify: L2
      test_coverage: partial
      propagation: smoke_ui
      primary:[client/src/utils/api.js, client/src/utils/requestBase.js]
      secondary:[]
      test:[client/src/utils/api.test.js, client/src/utils/api.coverage.test.js, client/src/utils/requestBase.test.js]

L1.11 Zustand State Management  [impl:done] [verify:pass] v1.0
      deps:[L0.7]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[client/src/store/walletStore.js, client/src/store/fingerPrintStore.js, client/src/store/pathStore.js, client/src/store/agentStore.js]
      secondary:[]
      test:[client/src/store/walletStore.test.js, client/src/store/fingerPrintStore.test.js, client/src/store/pathStore.test.js, client/src/store/agentStore.test.js]

L1.12 Event Bus  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[client/src/utils/eventEmitter.js]
      secondary:[]
      test:[client/src/utils/eventEmitter.test.js]
```

## L2 — Capability Layer (Depends on L0+L1)

```
L2.1  knowledge.db Isolation After savePath Switch  [impl:done] [verify:pass] v1.4.3
      deps:[L1.8, L1.6]
      gate_mode: explicit
      gates:[L1.8, L1.6]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js, dbservice/lib/knowledgeStore.js]
      secondary:[config.js]
      test:[server/services/memoryService.test.js]

L2.2  sessions.json Isolation After savePath Switch  [impl:done] [verify:pass] v1.4.2
      deps:[L1.5]
      gate_mode: explicit
      gates:[L1.5]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[config.js]
      test:[assets/agents/job-seek/lib/core/sessionStore.test.js]

L2.3  User Data Retained After Upgrade  [impl:done] [verify:pass] v1.4.2
      deps:[L0.10, L0.12]
      gate_mode: explicit
      gates:[L0.10]
      verify: L4
      test_coverage: none
      primary:[config.js, electron.js]
      secondary:[]
      test:[TBD]

L2.4  Reset All Memory Clears knowledgeStore  [impl:done] [verify:pass] v1.4.1
      deps:[L1.6]
      gate_mode: explicit
      gates:[L1.6]
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js]
      secondary:[dbservice/lib/knowledgeStore.js]
      test:[server/services/memoryService.test.js]

L2.5  Reset All Memory Clears sessions.json  [impl:done] [verify:pass] v1.4.1
      deps:[L1.5]
      gate_mode: explicit
      gates:[L1.5]
      verify: L4
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, server/routes/stateRoutes.test.js]

L2.6  New savePath No Old Memory Leak  [impl:done] [verify:pass] v1.4.3
      deps:[L1.8, L2.1]
      gate_mode: auto
      verify: L4
      test_coverage: partial
      primary:[server/services/memoryService.js]
      secondary:[config.js]
      test:[server/services/memoryService.test.js]

L2.7  ComSpec env Passing (Windows spawn)  [impl:done] [verify:pass] v1.4.1 GUARD
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L1
      test_coverage: partial
      primary:[server/services/taskService.js, electron.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.8  workspace Directory Auto-Created + git init  [impl:done] [verify:pass] v1.4.1
      deps:[L0.2]
      gate_mode: explicit
      gates:[L0.2]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.9  Claude CLI Callable  [impl:done] [verify:pass] v1.4.1
      deps:[L2.7]
      gate_mode: explicit
      gates:[L2.7]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js, assets/agents/job-seek/agent.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.10 Codex CLI Callable  [impl:done] [verify:pass] v1.4.1
      deps:[L2.7]
      gate_mode: explicit
      gates:[L2.7]
      verify: L2
      test_coverage: partial
      primary:[server/services/taskService.js]
      secondary:[]
      test:[server/services/taskService.test.js]

L2.11 Create Session  [impl:done] [verify:pass] v1.0
      deps:[L1.5, L0.13]
      gate_mode: explicit
      gates:[L1.5]
      verify: L2
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, assets/agents/job-seek/lib/stateApi.test.js]

L2.12 Delete Session  [impl:done] [verify:pass] v1.0
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L2
      test_coverage: partial
      primary:[server/services/stateService.js, assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[server/services/stateService.test.js, assets/agents/job-seek/lib/core/sessionStore.test.js, assets/agents/job-seek/lib/stateApi.test.js]

L2.13 Session List Persistence  [impl:done] [verify:pass] v1.0
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/sessionStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/core/sessionStore.test.js]

L2.14 Resume Upload Parsing  [impl:done] [verify:pass] v1.2
      deps:[L1.2]
      gate_mode: explicit
      gates:[L1.2]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/fileParser.js]
      secondary:[assets/agents/job-seek/agent.js]
      test:[assets/agents/job-seek/lib/core/fileParser.test.js]

L2.15 Profile Collection（5 sections）  [impl:done] [verify:pass] v1.2
      deps:[L2.11]
      gate_mode: explicit
      gates:[L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/prompts.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/prompts.test.js]

L2.16 masterProfile Cross-Session Reuse  [impl:done] [verify:pass] v1.3
      deps:[L2.15, L1.6]
      gate_mode: explicit
      gates:[L2.15, L1.6]
      verify: L4
      test_coverage: none
      primary:[assets/agents/job-seek/lib/core/masterProfileClient.js]
      secondary:[assets/agents/job-seek/agent.js]
      test:[TBD]

L2.17 Profile seed from knowledgeStore  [impl:done] [verify:pass] v1.3
      deps:[L1.6, L1.1]
      gate_mode: explicit
      gates:[L1.6]
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/core/knowledgeClient.js]
      secondary:[dbservice/lib/knowledgeStore.js]
      test:[assets/agents/job-seek/lib/core/knowledgeClient.test.js]

L2.18 Login Confirmation Flow  [impl:done] [verify:pass] v1.2
      deps:[L1.4]
      gate_mode: explicit
      gates:[L1.4]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/workflow/platformService.test.js]

L2.19 Single Environment Runs Only One Task at a Time  [impl:done] [verify:pass] v1.3
      deps:[L1.4, L0.16]
      gate_mode: explicit
      gates:[L1.4]
      verify: L4
      test_coverage: partial
      primary:[server/services/taskService.js, server/services/stateService.js]
      secondary:[]
      test:[server/services/taskService.test.js, server/services/stateService.test.js]

L2.20 Onboarding Subtask Completion  [impl:done] [verify:pass] v1.2
      deps:[L2.11, L2.15]
      gate_mode: explicit
      gates:[L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js]
      secondary:[]
      test:[assets/agents/job-seek/agent.memory.test.js]

L2.21 Single Agent Entry (No Duplicates)  [impl:done] [verify:pass] v1.4.1
      deps:[L0.7, L1.10]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[client/src/pages/aiAgents/index.js, server/router.js]
      secondary:[]
      test:[TBD]
```

## L3 — Scenario Layer (Depends on L0+L1+L2)

```
L3.1  3 Platform Initialization (Indeed/LinkedIn/JobBank)  [impl:done] [verify:pass] v1.2
      deps:[L2.11, L1.4]
      gate_mode: explicit
      gates:[L2.11, L1.4]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js, assets/agents/job-seek/lib/workflow/platformStore.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/workflow/platformService.test.js, assets/agents/job-seek/lib/workflow/platformStore.test.js]

L3.2  Search Tool Construction  [impl:done] [verify:pass] v1.2
      deps:[L2.15, L1.4]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/tools/jobSearch.js, assets/agents/job-seek/lib/toolRouter.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/jobSearch.test.js, assets/agents/job-seek/lib/toolRouter.test.js]

L3.3  Indeed Login  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/indeed.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/indeed.test.js]

L3.4  LinkedIn Login  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/linkedin.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/linkedin.test.js]

L3.5  JobBank Login  [impl:done] [verify:pass] v1.2
      deps:[L1.4, L2.18]
      gate_mode: explicit
      gates:[L1.4, L2.18]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/sources/jobbank.js]
      secondary:[assets/agents/job-seek/lib/workflow/platformService.js]
      test:[assets/agents/job-seek/lib/sources/jobbank.test.js]

L3.6  Re-login Button Function  [impl:done] [verify:pass] v1.4.3(2026-03-21 real acceptance: Indeed+LinkedIn Re-login both trigger launchLogin → auto-verified; when cookie valid, status auto-returns to Logged in, no manual Confirm needed)
      deps:[L3.4, L2.18]
      gate_mode: explicit
      gates:[L3.4]
      verify: L5
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/workflow/platformService.js, assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
      verify_notes:
        - Re-login click → platformLogin() → launchLogin() call chain normal
        - When cookie alive, platformService.auto-verify automatically sets ready state
        - After closing browser, login status keeps Logged in (by design, not a bug)
        - wf-cell-action-login testid triggers Re-login in error state (not platform-relogin-{pid})

L3.7  Search Execution  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E: self-heal fix effective, screenshot+Cloudflare detection+healScript 3 bugs fixed, LinkedIn search 2→10/11 results, 5+ QUALIFIED jobs)
      deps:[L1.4, L3.3, L3.4, L3.5, L3.2]
      gate_mode: explicit
      gates:[L1.4, L3.2]
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/searchPipeline.js, assets/agents/job-seek/lib/workflow/workflowEngine.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/searchPipeline.test.js, assets/agents/job-seek/lib/searchPipeline.e2e.test.js, assets/agents/job-seek/lib/workflow/workflowEngine.test.js]
      verify_notes:
        - BUG: analyzeFailure healScript receives Object instead of string → "first argument must be of type string or Buffer"
        - Indeed search tool v8 timeout 180s (search script failed to return results before timeout)
        - LinkedIn: Cloudflare block → page title empty → "No job card selector found"
        - Pipeline correctly identifies 0 results and skips generate/apply steps

L3.8  JD Parsing & Matching  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E Phase 8 verified: job listing contains title/company/location, JD parsing normal)
      deps:[L3.7, L2.15]
      gate_mode: explicit
      gates:[L3.7]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/tools/parseListing.js, assets/agents/job-seek/lib/tools/matchProfile.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/parseListing.test.js, assets/agents/job-seek/lib/tools/matchProfile.test.js]

L3.9  Resume Generation  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E: pipeline QUALIFIED jobs trigger generate step, step completed (status=idle); user confirmed based on log evidence)
      deps:[L3.8, L2.16]
      gate_mode: auto
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/tools/resumeGen.js, assets/agents/job-seek/lib/tools/docxBuilder.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/tools/resumeGen.test.js, assets/agents/job-seek/lib/tools/docxBuilder.test.js]
      verify_notes:
        - E2E acceptance pending next search with results: verify resume.docx exists in savePath/documents/{jobId}/
        - Unit tests cover docxBuilder template rendering, section mapping, file writing

L3.10 Stuck Timeout Detection  [impl:done] [verify:pass] v1.4.0
      deps:[L3.7]
      gate_mode: auto
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/searchPipeline.js, assets/agents/job-seek/lib/workflow/workflowEngine.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/searchPipeline.test.js, assets/agents/job-seek/lib/workflow/workflowEngine.test.js]

L3.11 Pipeline Abort After Stuck  [impl:done] [verify:pass] v1.5(alert-service.e2e.test.js 13/13 pass; pipeline abort on consecutive errors + alertService.dispatch implemented)
      deps:[L3.10]
      gate_mode: auto
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/searchPipeline.js]
      secondary:[assets/agents/job-seek/lib/workflow/alertService.js]
      test:[assets/agents/job-seek/lib/workflow/alert-service.e2e.test.js]
```

## L4 — Presentation Layer (Depends on All Lower Layers)

```
L4.1  AI Chat Panel  [impl:done] [verify:pass] v1.2
      deps:[L2.9, L1.9, L1.10]
      gate_mode: explicit
      gates:[L2.9, L1.9]
      verify: L4
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.2  Runtime Settings（provider/model）  [impl:done] [verify:pass] v1.4.0
      deps:[L4.1, L1.10]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, server/services/providerModelService.js]
      secondary:[client/src/config/providerModels.js]
      test:[client/src/pages/agentWorkspace/index.test.js, server/services/providerModelService.test.js, client/src/config/providerModels.test.js]

L4.3  Subtask Panel  [impl:done] [verify:pass] v1.2
      deps:[L4.1, L2.20]
      gate_mode: explicit
      gates:[L4.1]
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.4  Preset Questions  [impl:done] [verify:pass] v1.2
      deps:[L4.1]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.5  File Upload  [impl:done] [verify:pass] v1.2
      deps:[L4.1, L2.14]
      gate_mode: explicit
      gates:[L4.1, L2.14]
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L4.6  Job Listing Display (title/company/location/salary)  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E Phase 8 verified: dashboard job listing displays normally)
      deps:[L3.7, L1.9]
      gate_mode: explicit
      gates:[L3.7, L1.9]
      verify: L4
      test_coverage: strong
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[client/src/pages/agentWorkspace/index.js]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js, assets/agents/job-seek/lib/workflow/dashboard-features.e2e.test.js]

L4.7  dashboardServer HTTP Service (port 30003)  [impl:done] [verify:pass] v1.4.3(2026-03-21 real acceptance: service startup, dashboard page rendering, platform cards, workflow control bar all normal)
      deps:[L3.1, L2.11]
      gate_mode: explicit
      gates:[L3.1, L2.11]
      verify: L4
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
      verify_notes:
        - dashboardServer binds localhost:30003 (changed from 127.0.0.1 to localhost)
        - After Build Dashboard subtask triggers, auto-seeds 3 platforms (Indeed/LinkedIn/JobBank)
        - BUG-001: After agent restart, seed has no idempotency check, causing duplicate platform cards (to be fixed)
        - /api/debug/browsers endpoint added for E2E to get browserId
        - data-testid covers all control bar buttons, platform cards, workflow cell action buttons

L4.8  Workflow Editor + Launch  [impl:done] [verify:pass] v1.5(2026-03-21 real acceptance: editor popup, parameter configuration, enters RUNNING state after confirmation)
      deps:[L4.7, L3.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: none
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[TBD]
      verify_notes:
        - wfStart() checks AI provider → opens workflowEditorModal
        - Editor loads search config, platform list, job list (3 parallel fetches)
        - Search config: minScore/targetCount/maxResults/searchPreference/platform selection
        - Generation config: custom resume/cover letter/interview prep toggle
        - Apply: next version launch (locked)
        - After confirmation POST /api/workflow/:sid/start, dashboard status changes to RUNNING

L4.9  Workflow Progress Panel  [impl:done] [verify:pass] v1.5(2026-03-21 real acceptance: panel shows customizeProfile/search/generate/apply 4 steps, real-time log scrolling)
      deps:[L4.8]
      gate_mode: auto
      verify: L3
      test_coverage: none
      primary:[assets/agents/job-seek/lib/dashboardServer.js]
      secondary:[]
      test:[TBD]
      verify_notes:
        - workflow-progress-btn click opens progress offcanvas
        - Shows step status: done(Xs)/running(Xs)/idle/skipped
        - Log area displays pipeline output in real-time (with timestamps)
        - Steps/status two tabs switchable

L4.10  E2E Main Flow Test  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E 14/14 pass, Phase 0-8 all passed, self-heal + QUALIFIED detection + generate completed)
      deps:[L4.1, L4.3, L4.5, L4.7, L4.8]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: strong
      primary:[test/main-flow.spec.js, test/helpers/e2e-helpers.js]
      secondary:[]
      test:[test/main-flow.spec.js]
      last_full_pass: 2026-03-21
      verify_notes:
        - Phase 0-8 serial GATE mechanism
        - Does not use page.goto() to skip steps, follows complete UI click flow
        - Dashboard uses second browser context (port 30003)
        - Covers session creation, preset filling, resume upload, dashboard verification, login, search build, workflow, result verification

L4.11  E2E Rebuild/Self-heal Test  [impl:done] [verify:pass] v1.4.5(2026-03-21 E2E 4/4 pass, GATE + Scenario 1-3 passed)
      deps:[L4.7, L3.7]
      gate_mode: explicit
      gates:[L4.7]
      verify: L4
      test_coverage: partial
      primary:[test/rebuild-flow.spec.js]
      secondary:[]
      test:[test/rebuild-flow.spec.js]
      verify_notes:
        - Scenario 1: search error → Rebuild → building → ready
        - Scenario 2: zero results → self-heal log verification
        - Scenario 3: re-login → launching → verifying → verified

L4.12  E2E Skip-Step Mode Verification  [impl:done] [verify:pass] v1.4.6
      deps:[L4.10]
      gate_mode: conditional
      gate_condition: L4.10 verify:pass + change scope excludes Phase 1-3 files + within 7 days of last full pass
      verify: verify:pass
      test_coverage: partial
      primary:[test/main-flow.spec.js, test/helpers/e2e-helpers.js]
      secondary:[]
      test:[test/main-flow.spec.js]
```

## L5 — stateService HTTP + SSE (Depends on L0+L1)

```
L5.1  State HTTP Read (Phase A1)  [impl:done] [verify:pass] v1.5
      deps:[L0.7]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.2  State HTTP Write (Phase A2)  [impl:done] [verify:pass] v1.5
      deps:[L0.7, L5.1]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.3  Session CRUD HTTP (Phase A3)  [impl:done] [verify:pass] v1.5
      deps:[L5.1, L5.2, L1.5]
      gate_mode: explicit
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js, server/services/stateService.test.js]

L5.4  Language Preference (Phase A4)  [impl:done] [verify:pass] v1.5
      deps:[L5.1, L5.2]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js, server/services/stateService.test.js]

L5.5  SSE Subscribe Endpoint (Phase B1)  [impl:done] [verify:pass] v1.5
      deps:[L5.1]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/routes/stateRoutes.js, server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.6  SSE Broadcast Wiring (Phase B2)  [impl:done] [verify:pass] v1.5
      deps:[L5.5]
      gate_mode: auto
      verify: L2
      test_coverage: strong
      primary:[server/services/stateService.js]
      secondary:[]
      test:[server/routes/stateRoutes.test.js]

L5.7  Agent HTTP Read on Startup (Phase C1)  [impl:done] [verify:pass] v1.5
      deps:[L5.3]
      gate_mode: explicit
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.8  Agent HTTP Write-through (Phase C2)  [impl:done] [verify:pass] v1.5
      deps:[L5.7, L5.3]
      gate_mode: explicit
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.9  Agent SSE Subscription (Phase C3)  [impl:done] [verify:pass] v1.5
      deps:[L5.6, L5.7]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[assets/agents/job-seek/agent.js, assets/agents/job-seek/lib/stateApi.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/stateApi.test.js]

L5.10 Frontend Session HTTP (Phase D1)  [impl:done] [verify:pass] v1.5
      deps:[L5.3, L4.1]
      gate_mode: explicit
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, client/src/utils/api.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L5.11 savePath via stateService (Phase D2)  [impl:done] [verify:pass] v1.5
      deps:[L5.3, L5.9]
      gate_mode: explicit
      verify: L3
      test_coverage: partial
      primary:[client/src/pages/agentWorkspace/index.js, client/src/utils/api.js]
      secondary:[]
      test:[client/src/pages/agentWorkspace/index.test.js]

L5.12 Dashboard Language Sync (Phase D3)  [impl:done] [verify:pass] v1.5
      deps:[L5.4]
      gate_mode: auto
      verify: L3
      test_coverage: partial
      primary:[assets/agents/job-seek/lib/dashboardServer.js, client/src/Layout/index.js, client/src/utils/api.js]
      secondary:[]
      test:[assets/agents/job-seek/lib/dashboardServer.test.js]
```

---

## Reverse Dependency Index (with max_verify Recommendation)

Changed a node → lookup table → find all affected nodes → determine verification depth limit by max_verify.

| Node | Directly Depended By | Transitive Impact (Indirect) | max_verify |
|------|-------------------|-----------------|------------|
| L0.2 | L1.1, L1.2, L2.7, L2.8 | L1.3, L1.6-L1.8, L2.1-L2.6, L2.9-L2.10, L2.17, L3.*, L4.* | L5 |
| L0.6 | L1.9 | L4.1-L4.6 | L4 |
| L0.7 | L1.10, L1.11, L2.21 | L4.1-L4.6 | L4 |
| L0.10 | L2.3 | — | L4 |
| L0.12 | L1.1, L1.5, L1.7, L2.3 | L1.6, L1.8, L2.1-L2.6, L2.11-L2.13, L3.*, L4.* | L5 |
| L0.13 | L1.5, L2.11 | L2.2, L2.5, L2.12-L2.13, L2.15, L2.20, L3.1-L3.11, L4.* | L5 |
| L0.14 | L1.4 | L2.18-L2.19, L3.1-L3.11, L4.6 | L5 |
| L0.15 | L1.4 | Same as L0.14 | L5 |
| L0.16 | L1.4, L2.19 | Same as L0.14 | L5 |
| L1.1 | L1.6, L1.7, L1.8, L2.17 | L2.1, L2.4, L2.6, L2.16, L3.8-L3.9 | L4 |
| L1.2 | L1.3, L2.14 | L4.5 | L3 |
| L1.4 | L2.18, L2.19, L3.1-L3.5, L3.7 | L3.6-L3.11, L4.6 | L5 |
| L1.5 | L1.8, L2.2, L2.5, L2.11 | L2.1, L2.6, L2.12-L2.20, L3.*, L4.* | L5 |
| L1.6 | L2.1, L2.4, L2.16, L2.17 | L2.6, L3.8-L3.9 | L4 |
| L1.8 | L2.1, L2.6 | — | L4 |
| L1.9 | L4.1, L4.6 | L4.2-L4.5 | L4 |
| L1.10 | L4.1, L4.2, L2.21 | L4.3-L4.5 | L4 |
| L1.12 | — | — | L2 |
| L2.7 | L2.9, L2.10 | L4.1, L4.2 | L4 |
| L2.9 | L4.1 | L4.2-L4.5 | L4 |
| L2.11 | L2.12, L2.13, L2.15, L2.20, L3.1 | L2.16, L3.2, L3.7-L3.11, L4.* | L5 |
| L2.14 | L4.5 | — | L3 |
| L2.15 | L2.16, L2.20, L3.2, L3.8 | L2.16, L3.7, L3.9, L4.* | L4 |
| L2.16 | L3.9 | — | L4 |
| L2.18 | L3.3, L3.4, L3.5, L3.6 | L3.7, L4.6 | L5 |
| L2.20 | L4.3 | — | L3 |
| L3.2 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.3 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.4 | L3.6, L3.7 | L3.8-L3.11, L4.6 | L5 |
| L3.5 | L3.7 | L3.8-L3.11, L4.6 | L4 |
| L3.7 | L3.8, L3.10, L4.6 | L3.9, L3.11 | L4 |
| L3.8 | L3.9 | — | L4 |
| L3.10 | L3.11 | — | L4 |
| L4.1 | L4.2, L4.3, L4.4, L4.5 | — | L4 |

**max_verify note**: When this node changes, the recommended highest verification level. Based on the deepest verify level in its transitive impact. E.g., L0.2 affects L3.3-L3.5 (verify:L5), so max_verify=L5.

---

## File → Node Index Table

| File Path | Primary Nodes | Secondary Nodes | Test Coverage Nodes |
|---------|-------------|---------------|--------------|
| `electron.js` | L0.1, L0.2, L0.3, L0.4, L0.10, L0.11, L2.3, L2.7 | — | — |
| `preload.js` | L0.1 | — | — |
| `config.js` | L0.5, L0.10, L0.12, L0.13, L1.5, L1.7, L1.8 | L2.1, L2.2, L2.3, L2.6 | — |
| `package.json` | L0.8 | — | — |
| `server/server.js` | L0.5, L0.6, L0.7 | — | — |
| `server/router.js` | L0.7, L0.16, L2.21 | — | — |
| `server/services/webSocketService.js` | L0.6 | — | — |
| `server/services/taskService.js` | L2.7, L2.8, L2.9, L2.10, L2.19 | L0.13 | — |
| `server/services/stateService.js` | L1.5, L2.5, L2.11, L2.12, L2.19, L5.1-L5.6 | — | — |
| `server/routes/stateRoutes.js` | L5.1, L5.2, L5.3, L5.4, L5.5 | — | — |
| `server/services/memoryService.js` | L1.1, L1.8, L2.1, L2.4, L2.6 | — | — |
| `server/services/fingerPrintService.js` | L0.14, L0.15, L0.16, L1.4 | L0.13 | — |
| `server/services/walletService.js` | — | L0.13 | — |
| `server/services/providerModelService.js` | L4.2 | — | — |
| `server/services/toolServiceManager.js` | L1.2, L1.3 | — | — |
| `server/services/proxyService.js` | — | — | — |
| `dbservice/index.js` | L1.1 | — | — |
| `dbservice/lib/knowledgeStore.js` | L1.1, L1.6, L1.7 | L2.1, L2.4, L2.17 | — |
| `toolService/index.js` | L1.2 | — | — |
| `toolService/package.json` | — | L0.3, L0.11 | — |
| `dbservice/package.json` | — | L0.3, L0.11 | — |
| `scripts/pre-dist.js` | L0.8, L0.9 | — | — |
| `client/src/index.js` | — | L0.1, L0.4 | — |
| `client/src/index.scss` | L0.18, L0.19 | — | — |
| `client/src/router.js` | L0.17 | — | — |
| `client/src/i18n.js` | L0.20, L0.21 | — | — |
| `client/src/utils/languages/` | L0.20, L0.21 | — | — |
| `client/src/utils/webSocket.js` | L1.9 | — | — |
| `client/src/utils/api.js` | L1.10, L5.10, L5.11, L5.12 | — | — |
| `client/src/utils/requestBase.js` | L1.10 | — | — |
| `client/src/utils/eventEmitter.js` | L1.12 | — | — |
| `client/src/store/walletStore.js` | L1.11 | — | — |
| `client/src/store/fingerPrintStore.js` | L1.11 | — | — |
| `client/src/store/pathStore.js` | L1.11 | — | — |
| `client/src/store/agentStore.js` | L1.11 | — | — |
| `client/src/Layout/index.js` | L0.17, L0.18, L5.12 | — | — |
| `client/src/pages/agentWorkspace/index.js` | L4.1, L4.2, L4.3, L4.4, L4.5, L5.10, L5.11 | L4.6 | — |
| `client/src/pages/aiAgents/index.js` | L2.21 | — | — |
| `client/src/pages/ChromeManager/index.js` | — | — | L0.16 |
| `client/src/config/providerModels.js` | — | L4.2 | — |
| `assets/agents/job-seek/agent.js` | L2.9, L2.15, L2.20, L5.7, L5.8, L5.9 | L2.14, L2.16 | — |
| `assets/agents/job-seek/lib/stateApi.js` | L5.7, L5.8, L5.9 | — | — |
| `assets/agents/job-seek/lib/core/sessionStore.js` | L2.2, L2.5, L2.11, L2.12, L2.13 | — | — |
| `assets/agents/job-seek/lib/core/browserLauncher.js` | L1.4 | — | — |
| `assets/agents/job-seek/lib/core/fileParser.js` | L2.14 | — | — |
| `assets/agents/job-seek/lib/core/knowledgeClient.js` | L2.17 | — | — |
| `assets/agents/job-seek/lib/core/masterProfileClient.js` | L2.16 | — | — |
| `assets/agents/job-seek/lib/prompts.js` | L2.15 | — | — |
| `assets/agents/job-seek/lib/toolRouter.js` | L3.2 | — | — |
| `assets/agents/job-seek/lib/searchPipeline.js` | L3.7, L3.10, L3.11 | — | — |
| `assets/agents/job-seek/lib/dashboardServer.js` | L3.6, L4.6, L5.12 | — | — |
| `assets/agents/job-seek/lib/tools/jobSearch.js` | L3.2 | — | — |
| `assets/agents/job-seek/lib/tools/parseListing.js` | L3.8 | — | — |
| `assets/agents/job-seek/lib/tools/matchProfile.js` | L3.8 | — | — |
| `assets/agents/job-seek/lib/tools/resumeGen.js` | L3.9 | — | — |
| `assets/agents/job-seek/lib/tools/docxBuilder.js` | L3.9 | — | — |
| `assets/agents/job-seek/lib/sources/indeed.js` | L3.3 | — | — |
| `assets/agents/job-seek/lib/sources/linkedin.js` | L3.4 | — | — |
| `assets/agents/job-seek/lib/sources/jobbank.js` | L3.5 | — | — |
| `assets/agents/job-seek/lib/workflow/platformService.js` | L2.18, L3.1 | L3.3, L3.4, L3.5, L3.6 | — |
| `assets/agents/job-seek/lib/workflow/platformStore.js` | L3.1 | — | — |
| `assets/agents/job-seek/lib/workflow/workflowEngine.js` | L3.7, L3.10 | — | — |
| `assets/agents/job-seek/lib/workflow/alertService.js` | — | L3.11 | — |
| **Test Files** | | | |
| `server/services/memoryService.test.js` | — | — | L1.1, L1.8, L2.1, L2.4, L2.6 |
| `server/services/stateService.test.js` | — | — | L1.5, L2.5, L2.11, L2.12, L2.19, L5.3, L5.4 |
| `server/routes/stateRoutes.test.js` | — | — | L1.5, L2.5, L5.1, L5.2, L5.3, L5.4, L5.5, L5.6 |
| `server/services/taskService.test.js` | — | — | L0.13, L2.7, L2.8, L2.9, L2.10, L2.19 |
| `server/services/fingerPrintService.test.js` | — | — | L0.14, L0.15, L0.16, L1.4 |
| `server/services/walletService.test.js` | — | — | L0.13 |
| `server/services/webSocketService.test.js` | — | — | L0.6 |
| `server/services/toolServiceManager.test.js` | — | — | L1.2, L1.3 |
| `server/services/providerModelService.test.js` | — | — | L4.2 |
| `server/services/proxyService.test.js` | — | — | UNMAPPED |
| `client/src/utils/webSocket.test.js` | — | — | L1.9 |
| `client/src/utils/api.test.js` | — | — | L1.10 |
| `client/src/utils/api.coverage.test.js` | — | — | L1.10 |
| `client/src/utils/requestBase.test.js` | — | — | L1.10 |
| `client/src/utils/eventEmitter.test.js` | — | — | L1.12 |
| `client/src/store/walletStore.test.js` | — | — | L1.11 |
| `client/src/store/fingerPrintStore.test.js` | — | — | L1.11 |
| `client/src/store/pathStore.test.js` | — | — | L1.11 |
| `client/src/store/agentStore.test.js` | — | — | L1.11 |
| `client/src/Layout/index.test.js` | — | — | L0.17, L0.18 |
| `client/src/pages/agentWorkspace/index.test.js` | — | — | L4.1, L4.2, L4.3, L4.4, L4.5, L5.10, L5.11 |
| `client/src/pages/ChromeManager/index.test.js` | — | — | L0.16 |
| `client/src/config/providerModels.test.js` | — | — | L4.2 |
| `client/src/components/taskOffcanvas/AITaskPanel.test.js` | — | — | UNMAPPED |
| `assets/agents/job-seek/lib/core/sessionStore.test.js` | — | — | L2.2, L2.5, L2.11, L2.12, L2.13 |
| `assets/agents/job-seek/lib/core/fileParser.test.js` | — | — | L2.14 |
| `assets/agents/job-seek/lib/core/knowledgeClient.test.js` | — | — | L1.6, L2.17 |
| `assets/agents/job-seek/lib/core/browserLauncher.test.js` | — | — | L1.4 |
| `assets/agents/job-seek/lib/prompts.test.js` | — | — | L2.15 |
| `assets/agents/job-seek/lib/toolRouter.test.js` | — | — | L3.2 |
| `assets/agents/job-seek/lib/searchPipeline.test.js` | — | — | L3.7, L3.10 |
| `assets/agents/job-seek/lib/searchPipeline.e2e.test.js` | — | — | L3.7 |
| `assets/agents/job-seek/lib/dashboardServer.test.js` | — | — | L3.6, L4.6, L5.12 |
| `assets/agents/job-seek/lib/tools/jobSearch.test.js` | — | — | L3.2 |
| `assets/agents/job-seek/lib/tools/parseListing.test.js` | — | — | L3.8 |
| `assets/agents/job-seek/lib/tools/matchProfile.test.js` | — | — | L3.8 |
| `assets/agents/job-seek/lib/tools/resumeGen.test.js` | — | — | L3.9 |
| `assets/agents/job-seek/lib/tools/docxBuilder.test.js` | — | — | L3.9 |
| `assets/agents/job-seek/lib/sources/indeed.test.js` | — | — | L3.3 |
| `assets/agents/job-seek/lib/sources/linkedin.test.js` | — | — | L3.4 |
| `assets/agents/job-seek/lib/sources/jobbank.test.js` | — | — | L3.5 |
| `assets/agents/job-seek/lib/workflow/platformService.test.js` | — | — | L2.18, L3.1 |
| `assets/agents/job-seek/lib/workflow/platformStore.test.js` | — | — | L3.1 |
| `assets/agents/job-seek/lib/workflow/workflowEngine.test.js` | — | — | L3.7, L3.10 |
| `assets/agents/job-seek/lib/workflow/alert-service.e2e.test.js` | — | — | L3.11 |
| `assets/agents/job-seek/lib/workflow/dashboard-features.e2e.test.js` | — | — | L4.6 |
| `test/main-flow.spec.js` | — | — | L4.1, L4.3, L4.5, L4.7, L4.8, L4.10 |
| `test/rebuild-flow.spec.js` | — | — | L3.6, L3.7, L4.7, L4.11 |
| `test/helpers/e2e-helpers.js` | — | — | L4.10, L4.11 |
| `assets/agents/job-seek/lib/stateApi.test.js` | — | — | L2.11, L2.12, L5.7, L5.8, L5.9 |
| `assets/agents/job-seek/agent.memory.test.js` | — | — | L2.20 |
| `assets/agents/shared/stateClient.test.js` | — | — | UNMAPPED |

---

## Minimal Acceptance Path Derivation Rules v3

### Core Rules

1. **Input**: git diff file list or specified changed nodes
2. **Lookup file index (primary-first)**: file → primary associated node set `S_primary`
3. **Default mode**: Only spread from `S_primary`. `--full` mode appends secondary associated nodes to `S`
4. **GATE check**: For each node in `S`, check if all nodes in its `gates:[]` are verify:pass. GATE FAIL → node SKIP
5. **Reverse spread**: For each node in `S`, lookup reverse index for all direct + transitive dependent nodes → impact set `A`
6. **Merge**: `R = S ∪ A` (excluding GATE FAIL SKIP nodes)
7. **Sort by layer ascending**: L0 → L1 → L2 → L3 → L4
8. **verify depth trim**: Each node accepted to its marked `verify` depth, but not exceeding `max_verify` (from reverse index table)
9. **Generate acceptance steps**: In sorted order, verify one by one from bottom layer to top

### Critical Base File Auto-Upgrade

The following files are marked as `critical_file`, auto-upgrading from primary-first to full-lite mode when hit:

| File | Reason | Upgrade Behavior |
|------|------|---------|
| electron.js | System startup entry, 8 nodes attached | All primary included + high-risk secondary included |
| config.js | Global config, 10 nodes attached | All primary included + high-risk secondary included |
| server/router.js | API Route Registration | All primary included |
| server/services/taskService.js | Core task engine | All primary included + secondary included |
| client/src/pages/agentWorkspace/index.js | Agent UI shell file | All primary included |
| assets/agents/job-seek/agent.js | Agent orchestration file | All primary included |

Rules:
- `--query file.js` hits critical_file → auto-switch to full-lite
- full-lite = all primary + secondary nodes with verify >= L3
- User can use `--primary-only` to force primary only

### GATE Blocking Logic

```
Node X gates:[L1.8, L2.1]
  ├─ L1.8 verify:pass and L2.1 verify:pass → Normal acceptance of X
  ├─ L1.8 verify:fail → X directly SKIP, acceptance not executed
  └─ L2.1 verify:pending → X directly SKIP, wait for L2.1 to complete then verify
```

Difference between GATE and deps:
- **deps** = Functional dependency (X needs these nodes working to run)
- **gates** = Acceptance blocking (when these nodes not passed, X acceptance not executed)
- When deps not passed, X may also fail but will attempt acceptance
- When gates not passed, X directly SKIP to not waste acceptance time

### primary-first Strategy

```
Default mode (no --full):
  git diff → only lookup primary mapping → precise acceptance

--full mode:
  git diff → lookup primary + secondary mapping → complete acceptance
```

Purpose: Reduce acceptance noise during daily development. Changing `config.js` does not need to verify all secondary nodes referencing it, only verify primary core nodes.

### Base Connection Node Propagation

When diff hits nodes with `propagation: smoke_ui`:
- Standard path derivation proceeds normally
- Additional suggestion: Select 1-2 core UI nodes in the highest layer (L4) for smoke check
- smoke node selection: Prefer L4.1 (AI Chat Panel) and L4.6 (Job listing)

Nodes with `propagation: smoke_ui`:
- L0.5（Express Port Assignment）
- L0.6（WebSocket Service Start）
- L0.7（API Route Registration）
- L1.9 (WebSocket Client)
- L1.10 (API Client)

### Execution Failure Strategy

| Situation | Strategy |
|------|------|
| GATE node fails | Block all upper layer nodes with it as gate, mark verify:skipped |
| Non-GATE but verify < L3 node fails | Record failure, continue other nodes in same layer |
| 2 consecutive critical nodes (verify >= L4) fail | Auto-suggest switching to --full mode |
| L0 layer node fails | All transitive dependents of this node SKIP |

Output after failure:
- Passed nodes list
- Failed nodes + reasons
- SKIP nodes list
- Suggested next steps

### Derivation Example 1: Modifying `server/services/memoryService.js`

```
Step 1 — Lookup file index (primary-first):
  memoryService.js primary → {L1.1, L1.8, L2.1, L2.4, L2.6}

Step 2 — GATE check:
  L1.1 gates:[L0.2] → L0.2 verify:pass → Passed
  L1.8 gates:[L1.1, L1.5] → all verify:pass → Passed
  L2.1 gates:[L1.8, L1.6] → all verify:pass → Passed
  L2.4 gates:[L1.6] → verify:pass → Passed
  L2.6 gates:[L1.8, L2.1] → all verify:pass → Passed
  All passed, no SKIP

Step 3 — Reverse spread:
  L1.1 depended by: L1.6, L1.7, L1.8, L2.17
  L1.8 depended by: L2.1, L2.6
  L2.1 depended by: (none)
  L2.4 depended by: (none)
  L2.6 depended by: (none)

Step 4 — Merge and deduplicate:
  {L1.1, L1.6, L1.7, L1.8, L2.1, L2.4, L2.6, L2.16, L2.17, L3.8, L3.9}

Step 5 — Sort by layer + verify depth:
  L1: L1.1(L2) → L1.6(L2) → L1.7(L2) → L1.8(L4)
  L2: L2.1(L4) → L2.4(L4) → L2.6(L4) → L2.16(L4) → L2.17(L2)
  L3: L3.8(L4) → L3.9(L4,verify:pending skip)
```

### Derivation Example 2: Modifying `assets/agents/job-seek/lib/sources/linkedin.js`

```
Step 1 — Lookup file index (primary-first):
  linkedin.js primary → {L3.4}

Step 2 — GATE check:
  L3.4 gates:[L1.4, L2.18]
  ├─ L1.4 verify:pass → Passed
  └─ L2.18 verify:pass → Passed
  Passed

Step 3 — Reverse spread:
  L3.4 depended by: L3.6, L3.7 → transitive: L3.8-L3.11, L4.6

Step 4 — Merge:
  {L3.4, L3.6, L3.7, L3.8, L3.9, L3.10, L3.11, L4.6}

Step 5 — GATE second check:
  L3.6 gates:[L3.4] → L3.4 just modified needs verification first → decide after L3.4 acceptance
  L3.7 gates:[L1.4, L3.2] → all verify:pass

Step 6 — Acceptance path + verify depth:
  L3: L3.4(L5) → L3.6(L5,verify:fail known failure) → L3.7(L4) → L3.8(L4) → L3.10(L4) → L3.11(L4,verify:pending)
  L4: L4.6(L4)
```

---

## ID Migration Mapping Table

| Old ID | Old Name | New ID |
|--------|--------|--------|
| 1.1.1 | Main window load index.html | L0.1 |
| 1.1.2 | env Passed to server fork | L0.2 |
| 1.1.3 | First launch npm install | L0.3 |
| 1.1.4 | Initialization Intercept Page Display | L0.4 |
| 1.2.1 | Dynamic port assignment | L0.5 |
| 1.2.2 | WebSocket Service Start | L0.6 |
| 1.2.3 | API Route Registration | L0.7 |
| 1.3.1 | dbservice startup | L1.1 |
| 1.3.2 | dbservice Restart on savePath Switch | L1.8 |
| 1.3.3 | toolService Start | L1.2 |
| 1.3.4 | toolService Health Check | L1.3 |
| 1.4.1 | client/build Included in asar | L0.8 |
| 1.4.2 | pre-dist Check Passed | L0.9 |
| 1.4.3 | Install Directory Has No User Data | L0.10 |
| 1.4.4 | toolService/dbservice node_modules Deferred Install | L0.11 |
| 2.1.1 | Default savePath Auto-Created | L0.12 |
| 2.1.2 | NeDB Reconnect After savePath Switch | L1.5 |
| 2.1.3 | knowledge.db Isolation After savePath Switch | L2.1 |
| 2.1.4 | sessions.json Isolation After savePath Switch | L2.2 |
| 2.1.5 | User Data Retained After Upgrade | L2.3 |
| 2.2.1 | NeDB CRUD | L0.13 |
| 2.2.2 | knowledge.db SQLite Read/Write | L1.6 |
| 2.2.3 | knowledge.db Stored in savePath/db/ | L1.7 |
| 2.3.1 | Reset All Memory Clears knowledgeStore | L2.4 |
| 2.3.2 | Reset All Memory Clears sessions.json | L2.5 |
| 2.3.3 | New savePath No Old Memory Leak | L2.6 |
| 3.1.1 | ComSpec env passing | L2.7 |
| 3.1.2 | workspace Directory Auto-Created + git init | L2.8 |
| 3.1.3 | Claude CLI Callable | L2.9 |
| 3.1.4 | Codex CLI Callable | L2.10 |
| 3.2.1 | Create Session | L2.11 |
| 3.2.2 | Delete Session | L2.12 |
| 3.2.3 | Session List Persistence | L2.13 |
| 3.2.4 | Onboarding Subtask Completion | L2.20 |
| 3.3.1 | Resume Upload Parsing | L2.14 |
| 3.3.2 | Profile Collection | L2.15 |
| 3.3.3 | masterProfile Cross-Session Reuse | L2.16 |
| 3.3.4 | Profile seed from knowledgeStore | L2.17 |
| 3.4.1 | 3 platform initialization | L3.1 |
| 3.4.2 | Search Tool Construction | L3.2 |
| 3.4.3 | Job listing display | L4.6 |
| 3.5.1 | Search Execution | L3.7 |
| 3.5.2 | JD Parsing & Matching | L3.8 |
| 3.5.3 | Resume Generation | L3.9 |
| 3.5.4 | Stuck Timeout Detection | L3.10 |
| 3.5.5 | Pipeline Abort After Stuck | L3.11 |
| 4.1.1 | Chromium Installation | L0.14 |
| 4.1.2 | Fingerprint Config Generation | L0.15 |
| 4.1.3 | Browser Environment Launch | L1.4 |
| 4.1.4 | Login Confirmation Flow | L2.18 |
| 4.2.1 | Indeed Login | L3.3 |
| 4.2.2 | LinkedIn Login | L3.4 |
| 4.2.3 | JobBank Login | L3.5 |
| 4.2.4 | Re-login Button Function | L3.6 |
| 4.3.1 | Environment List CRUD | L0.16 |
| 4.3.2 | Single Environment Runs Only One Task at a Time | L2.19 |
| 5.1.1 | Sidebar Navigation | L0.17 |
| 5.1.2 | Responsive layout | L0.18 |
| 5.1.3 | Unified card width | L0.19 |
| 5.2.1 | Chinese | L0.20 |
| 5.2.2 | English | L0.21 |
| 5.3.1 | AI Chat Panel | L4.1 |
| 5.3.2 | Runtime Settings | L4.2 |
| 5.3.3 | Subtask Panel | L4.3 |
| 5.3.4 | Preset Questions | L4.4 |
| 5.3.5 | File Upload | L4.5 |
| 5.4.1 | Single Agent entry | L2.21 |

---

## Script Requirements v3: build-acceptance-graph.js

### 6 Validations (All Must Pass)

#### Validation 1: deps/gates References Exist and No Cycles

- Parse all nodes' `deps:[]` and `gates:[]`
- Each referenced `Lx.y` must exist in defined nodes
- Build DAG, detect cycles (DFS topological sort)
- Violation → error with invalid references or cycle listed

#### Validation 2: Layer Satisfies max(dep layer)+1

- For each node `Lx.y`, check `x == max(all deps node layers) + 1`
- Nodes with no deps must be L0
- Violation → error: `L2.7 layer should be L1 (deps max layer is L0)` etc.
- **Exception allowlist**: Allow same-layer dependencies (e.g., L2.12 deps:[L2.11]), layer = max(deps layer)

#### Validation 3: File Mapping References Existing Nodes

- All node IDs referenced in `primary:[]`, `secondary:[]`, `test:[]` must exist
- Node IDs in file index table must exist
- Violation → error

#### Validation 4: gate_mode auto Consistency

- For all `gate_mode: auto` nodes, recalculate auto gates (deps nodes with verify >= L3)
- If node explicitly wrote gates and inconsistent with auto calculation → warning
- For all `gate_mode: explicit` nodes, check if gates field exists

#### Validation 5: Generate reverse index / file index / minimal path

- **reverse index**: Each node → nodes directly depending on it + transitive impact
- **file index**: Each file → primary/secondary/test node mapping
- **minimal path**: Given file list → output affected nodes (sorted by layer) + each node's verify depth
- **critical_file detection**: Hit critical_file → auto full-lite

#### Validation 6: TBD / UNMAPPED / orphan Report

- `[TBD]` count: Which nodes' test field is still TBD
- `UNMAPPED` count: Which test files not associated with any node
- `orphan` detection: Which source files appear in project but not in any node's primary/secondary
- `test_coverage: none` count

### CLI Usage

```bash
# Parse + validate + generate JSON
node scripts/build-acceptance-graph.js

# Query impact (primary-first, critical_file auto-upgrades to full-lite)
node scripts/build-acceptance-graph.js --query server/services/memoryService.js

# Query impact (including secondary)
node scripts/build-acceptance-graph.js --query server/services/memoryService.js --full

# Query impact (force primary only, even when hitting critical_file)
node scripts/build-acceptance-graph.js --query config.js --primary-only

# pre-dist verify sentinel nodes
node scripts/build-acceptance-graph.js --verify-guards

# Output TBD/UNMAPPED/orphan report
node scripts/build-acceptance-graph.js --audit
```

### Output Format

```json
{
  "nodes": {
    "L0.1": {
      "name": "Electron Main Window Load",
      "layer": 0,
      "build_status": "impl:done",
      "verify_status": "verify:pass",
      "version": "v1.4.3",
      "guard": true,
      "deps": [],
      "gate_mode": "auto",
      "gates": [],
      "verify": "L1",
      "test_coverage": "none",
      "primary": ["electron.js", "preload.js"],
      "secondary": ["client/src/index.js"],
      "test": [],
      "dependedBy": []
    }
  },
  "fileIndex": {
    "electron.js": {
      "primary": ["L0.1", "L0.2", "L0.3", "L0.4", "L0.10", "L0.11", "L2.3", "L2.7"],
      "secondary": [],
      "test": [],
      "critical_file": true
    }
  },
  "reverseIndex": {
    "L0.2": {
      "directDeps": ["L1.1", "L1.2", "L2.7", "L2.8"],
      "transitive": ["L1.3", "L1.6", "..."],
      "maxVerify": "L5"
    }
  },
  "stats": {
    "impl_done": 89,
    "impl_partial": 0,
    "impl_missing": 0,
    "verify_pass": 89,
    "verify_fail": 0,
    "verify_pending": 0,
    "verify_skipped": 0,
    "guard": 5,
    "gate_mode_auto": 38,
    "gate_mode_explicit": 45,
    "test_coverage_none": 14,
    "test_coverage_partial": 61,
    "test_coverage_strong": 8,
    "tbd_test": 14,
    "unmapped_test": 3,
    "orphan_files": 0
  }
}
```

---

## Statistics

### Node Statistics

| Layer | Node Count | verify:pass | verify:fail | verify:pending | verify:skipped |
|------|--------|-------------|-------------|----------------|----------------|
| L0   | 21     | 21          | 0           | 0              | 0              |
| L1   | 12     | 12          | 0           | 0              | 0              |
| L2   | 21     | 21          | 0           | 0              | 0              |
| L3   | 11     | 11          | 0           | 0              | 0              |
| L4   | 12     | 12          | 0           | 0              | 0              |
| L5   | 12     | 12          | 0           | 0              | 0              |
| Total | 89     | 89          | 0           | 0              | 0              |

### Status Statistics

| build_status | Count |
|-------------|------|
| impl:done | 89 |
| impl:partial | 0 |
| impl:missing | 0 |

| verify_status | Count |
|--------------|------|
| verify:pass | **89** |
| verify:T2-pass | 0 |
| verify:fail | 0 |
| verify:pending | 0 |
| verify:skipped | 0 |

| GUARD sentinels | 5 (L0.1, L0.2, L0.8, L0.10, L2.7) |
|-----------|------|

### gate_mode Statistics

| gate_mode | Count |
|-----------|------|
| auto | 39 |
| explicit | 49 |
| conditional | 1（L4.12） |

### verify Distribution

| verify Level | Node Count |
|------------|--------|
| L1 | 10 |
| L2 | 37 |
| L3 | 13 |
| L4 | 24 |
| L5 | 4 |
| verify:pass（L4.12） | 1 |

### test_coverage Distribution

| test_coverage | Count |
|--------------|------|
| none | 14 |
| partial | 61 |
| strong | 8（L3.7, L4.6, L5.1-L5.6） |

### File Mapping Quality

| Category | Count |
|------|------|
| test:[TBD] to be added | 14 |
| UNMAPPED test files | 3 (proxyService.test.js, AITaskPanel.test.js, stateClient.test.js) |
| Total primary mapped files | 55 |
| Total secondary mapped files | 18 |
| Total test mapped files | 49 |
| critical_file | 6 |
| propagation: smoke_ui | 5 |

### gates Statistics

| Category | Count |
|------|------|
| gate_mode: explicit nodes | 49 |
| gate_mode: auto nodes | 39 (of which 21 L0 have no deps) |
| gate_mode: conditional nodes | 1 (L4.12) |
| gates total references (explicit) | 59 |

## Statistics Table Maintenance Rules

- After Dev/Coordinator adds/removes acceptance graph nodes, must synchronize summary table
- Gatekeeper G5 check item: Statistics table numbers match actual node counts
- Future implementation of scripts/acceptance-graph-check.js for auto-verification
