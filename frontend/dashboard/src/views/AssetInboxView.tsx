import { useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../lib/api";
import type {
  AssetInboxBatchAction,
  AssetInboxItem,
  AssetInboxMountRelation,
  AssetInboxResponse,
  AssetInboxStatus,
  NodeRecord,
} from "../types";

interface Props {
  assetInbox: AssetInboxResponse;
  projectId: string;
  snapshotId: string;
  nodes: NodeRecord[];
}

type AssetGroupId = "ALL" | "doc" | "test" | "config" | "source" | "generated" | "other";
type AttachRole = "doc" | "test" | "config";
type AttachState = "idle" | "writing" | "written_uncommitted" | "error";
type DriftStateName = "not_drifted" | "suspected" | "confirmed" | "resolved" | "waived";
type ActionState = "idle" | "writing" | "written" | "blocked" | "error";

interface AttachDraft {
  targetNodeId: string;
  role: AttachRole;
}

interface AttachResult {
  state: AttachState;
  message: string;
}

interface ActionResult {
  state: ActionState;
  message: string;
}

interface RelationView extends AssetInboxMountRelation {
  relation_id: string;
  status: "accepted" | "candidate" | "unbound" | "stale_drift" | "impact_pending" | string;
  target_node_id: string;
}

interface GroupView {
  id: AssetGroupId;
  label: string;
  count: number;
  itemIds: Set<string>;
  statuses: Record<string, number>;
}

const GROUP_ORDER: AssetGroupId[] = ["ALL", "doc", "test", "config", "source", "generated", "other"];

const GROUP_LABELS: Record<AssetGroupId, string> = {
  ALL: "All assets",
  doc: "Docs",
  test: "Tests",
  config: "Config",
  source: "Source",
  generated: "Generated / Ignored",
  other: "Other",
};

const STATUS_LABELS: Record<string, string> = {
  source_orphan: "Source orphan",
  doc_unbound: "Doc unbound",
  doc_candidate: "Doc candidate",
  accepted: "Accepted",
  test_candidate: "Test candidate",
  config_pending_decision: "Config pending",
  ignored: "Ignored",
  archive: "Archive",
  stale: "Stale",
  impact_pending: "Impact pending",
  drift_suspected: "Drift suspected",
  drift_confirmed: "Drift confirmed",
  drift_resolved: "Drift resolved",
  drift_waived: "Drift waived",
};

const RELATION_LABELS: Record<string, string> = {
  accepted: "Accepted",
  candidate: "Candidate",
  unbound: "Unbound",
  stale_drift: "Stale / drift",
  impact_pending: "Impact pending",
};

const DRIFT_LABELS: Record<DriftStateName, string> = {
  not_drifted: "Not drifted",
  suspected: "Suspected",
  confirmed: "Confirmed",
  resolved: "Resolved",
  waived: "Waived",
};

export default function AssetInboxView({ assetInbox, projectId, snapshotId, nodes }: Props) {
  const [groupFilter, setGroupFilter] = useState<AssetGroupId>("ALL");
  const [query, setQuery] = useState("");
  const [selectedAssetId, setSelectedAssetId] = useState("");
  const [selectedRelationId, setSelectedRelationId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, AttachDraft>>({});
  const [attachResults, setAttachResults] = useState<Record<string, AttachResult>>({});
  const [actionResults, setActionResults] = useState<Record<string, ActionResult>>({});

  const items = useMemo(() => (assetInbox.items ?? []).slice().sort(compareAssets), [assetInbox.items]);
  const groups = useMemo(() => buildGroups(assetInbox, items), [assetInbox, items]);
  const visibleItems = useMemo(() => filterAssets(items, groups, groupFilter, query), [items, groups, groupFilter, query]);
  const selectedItem = useMemo(() => {
    if (selectedAssetId) {
      const selected = visibleItems.find((item) => item.asset_id === selectedAssetId);
      if (selected) return selected;
    }
    return visibleItems[0] ?? items.find((item) => item.asset_id === selectedAssetId) ?? null;
  }, [items, selectedAssetId, visibleItems]);
  const selectedRelations = useMemo(() => (selectedItem ? deriveRelations(selectedItem) : []), [selectedItem]);
  const selectedRelation = useMemo(() => {
    if (!selectedRelations.length) return null;
    if (selectedRelationId) {
      const match = selectedRelations.find((relation) => relation.relation_id === selectedRelationId);
      if (match) return match;
    }
    return selectedRelations[0];
  }, [selectedRelationId, selectedRelations]);
  const selectedSummary = useMemo(
    () => (selectedItem ? summarizeRelations(selectedItem, selectedRelations) : null),
    [selectedItem, selectedRelations],
  );
  const nodeOptions = useMemo(
    () =>
      nodes
        .filter((node) => (node.layer || "").toUpperCase() === "L7")
        .slice()
        .sort((a, b) => (a.title || a.node_id).localeCompare(b.title || b.node_id)),
    [nodes],
  );

  const reviewCount = assetInbox.summary?.operator_review_count ?? 0;
  const backlogEligible = assetInbox.summary?.backlog_eligible_count ?? 0;
  const total = assetInbox.summary?.total ?? items.length;
  const acceptedCount = assetInbox.summary?.accepted_count ?? countStatus(assetInbox, "accepted");
  const candidateCount =
    assetInbox.summary?.candidate_count ??
    items.reduce((sum, item) => sum + (item.binding_candidates ?? []).length, 0);

  const updateDraft = (path: string, patch: Partial<AttachDraft>) => {
    setDrafts((current) => {
      const item = items.find((candidate) => candidate.path === path);
      const existing = current[path] ?? {
        targetNodeId: suggestedTargetNodeId(item, nodeOptions),
        role: roleForAsset(item),
      };
      return { ...current, [path]: { ...existing, ...patch } };
    });
  };

  const writeHint = async (item: AssetInboxItem) => {
    const draft = drafts[item.path] ?? {
      targetNodeId: suggestedTargetNodeId(item, nodeOptions),
      role: roleForAsset(item),
    };
    if (!draft.targetNodeId) {
      setAttachResults((current) => ({
        ...current,
        [item.path]: { state: "error", message: "Select a target node first." },
      }));
      return;
    }
    setAttachResults((current) => ({
      ...current,
      [item.path]: { state: "writing", message: "Writing governance hint..." },
    }));
    try {
      const result = await api.attachFileGovernanceHintFor(projectId, snapshotId, {
        path: item.path,
        target_node_id: draft.targetNodeId,
        role: draft.role,
        actor: "dashboard_user",
      });
      setAttachResults((current) => ({
        ...current,
        [item.path]: {
          state: "written_uncommitted",
          message: result.message || "Hint written. Commit this file, then run Update graph.",
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setAttachResults((current) => ({
        ...current,
        [item.path]: { state: "error", message: msg },
      }));
    }
  };

  const recordDriftState = async (item: AssetInboxItem, driftState: DriftStateName) => {
    const key = `${item.asset_id}:drift`;
    setActionResults((current) => ({ ...current, [key]: { state: "writing", message: "Recording drift state..." } }));
    try {
      const result = await api.recordAssetDriftStateFor(projectId, {
        asset_kind: normalizeAssetKind(item.asset_kind),
        asset_path: item.path,
        drift_state: driftState,
        snapshot_id: snapshotId,
        actor: "dashboard_user",
        evidence: { source: "asset_inbox", previous_state: item.drift?.state || "not_drifted" },
      });
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "written",
          message: `Recorded ${DRIFT_LABELS[driftState]} for ${item.path}.`,
        },
      }));
      if (!result.ok) throw new Error("drift state write was not accepted");
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({ ...current, [key]: { state: "error", message: msg } }));
    }
  };

  const queueResolveDrift = async (item: AssetInboxItem, relation: RelationView | null) => {
    const key = `${item.asset_id}:resolve-drift`;
    setActionResults((current) => ({ ...current, [key]: { state: "writing", message: "Queueing drift proposal..." } }));
    try {
      const result = await api.queueAssetDriftProposalFor(projectId, {
        asset_kind: normalizeAssetKind(item.asset_kind),
        asset_path: item.path,
        snapshot_id: snapshotId,
        node_id: relation?.target_node_id || "",
        mode: "ai_assisted_proposal",
        note: "Queued from Asset Inbox drift controls.",
        actor: "dashboard_user",
      });
      const blocked = !result.ai_available || result.proposal?.status === "blocked";
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: blocked ? "blocked" : "written",
          message: blocked
            ? `Proposal recorded but AI is blocked: ${result.ai_reason}`
            : `Proposal queued with local precheck evidence: ${result.proposal?.proposal_id || "pending"}`,
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({ ...current, [key]: { state: "error", message: msg } }));
    }
  };

  const proposeRelationAction = async (item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding") => {
    const key = `${relation.relation_id}:${action}`;
    setActionResults((current) => ({ ...current, [key]: { state: "writing", message: "Recording proposal..." } }));
    try {
      const result = await api.fileHygieneActionFor(projectId, snapshotId, {
        action,
        path: item.path,
        target_node_id: relation.target_node_id,
        role: roleForAsset(item),
        reason: action === "remove_binding" ? "Proposal-safe binding removal from Asset Inbox." : "Proposal-safe binding add from Asset Inbox.",
        actor: "dashboard_user",
      });
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "written",
          message: `Proposal event recorded: ${String(result.event?.event_id || result.action)}`,
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({ ...current, [key]: { state: "error", message: msg } }));
    }
  };

  return (
    <div className="view asset-browser-view">
      <div className="view-head">
        <h2 className="view-title">Asset Inbox</h2>
        <span className="view-subtitle">
          Relation browser - <span className="mono">/api/graph-governance/{projectId}/snapshots/{snapshotId}/asset-inbox</span> -{" "}
          {visibleItems.length} shown - {total} total
        </span>
      </div>

      <div className="asset-browser-policy">
        <div>
          <strong>Asset review surface.</strong> Files stay separate from backlog rows; backlog work is created only from selected
          assets.
        </div>
        <span className="mono">{assetInbox.impact_scope_policy || "accepted_bindings_only"}</span>
      </div>

      <div className="score-grid asset-browser-score-grid">
        <Kpi label="Review" value={reviewCount} tone={reviewCount > 0 ? "amber" : "green"} />
        <Kpi label="Backlog eligible" value={backlogEligible} tone={backlogEligible > 0 ? "red" : "neutral"} />
        <Kpi label="Candidates" value={candidateCount} tone={candidateCount > 0 ? "amber" : "neutral"} />
        <Kpi label="Accepted" value={acceptedCount} tone="green" />
      </div>

      <section className="asset-relation-browser">
        <aside className="asset-selector-panel">
          <div className="asset-selector-head">
            <div>
              <div className="asset-panel-title">Assets</div>
              <div className="asset-panel-meta">{total} files in snapshot</div>
            </div>
            <span className="asset-chip-count">{visibleItems.length}</span>
          </div>
          <input
            className="asset-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search paths, nodes, evidence"
          />
          <div className="asset-group-list">
            {GROUP_ORDER.map((groupId) => {
              const group = groups[groupId] ?? emptyGroup(groupId);
              return (
                <button
                  key={groupId}
                  type="button"
                  className={`asset-group-row${groupFilter === groupId ? " active" : ""}`}
                  onClick={() => setGroupFilter(groupId)}
                >
                  <span className="asset-group-main">
                    <span>{group.label}</span>
                    <span className="asset-group-count">{group.count}</span>
                  </span>
                  <StatusChipStrip statuses={group.statuses} />
                </button>
              );
            })}
          </div>
          <div className="asset-selector-list scrollbar-thin">
            {visibleItems.length === 0 ? (
              <div className="asset-browser-empty">No assets match this group or search.</div>
            ) : (
              visibleItems.map((item) => {
                const relations = deriveRelations(item);
                const active = selectedItem?.asset_id === item.asset_id;
                return (
                  <button
                    key={item.asset_id}
                    type="button"
                    className={`asset-selector-row${active ? " active" : ""}`}
                    onClick={() => setSelectedAssetId(item.asset_id)}
                  >
                    <span className="asset-selector-path mono">{item.path}</span>
                    <span className="asset-selector-meta">
                      {labelForKind(item.asset_kind)} - {STATUS_LABELS[item.asset_status] ?? item.asset_status}
                    </span>
                    <span className="asset-selector-relations">
                      {relations.filter((relation) => relation.status === "accepted").length} accepted /{" "}
                      {relations.filter((relation) => relation.status !== "accepted").length} candidate
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </aside>

        <main className="asset-detail-panel">
          {selectedItem ? (
            <>
              <div className="asset-detail-head">
                <div>
                  <div className="asset-detail-kicker">
                    {labelForKind(selectedItem.asset_kind)} - {selectedItem.language || "language n/a"}
                  </div>
                  <h3 className="asset-detail-title mono">{selectedItem.path}</h3>
                </div>
                <span className={`status-badge ${assetStatusClass(selectedItem.asset_status)}`}>
                  {STATUS_LABELS[selectedItem.asset_status] ?? selectedItem.asset_status}
                </span>
              </div>

              <div className="asset-meta-grid">
                <Meta label="Hash" value={selectedItem.file_hash || selectedItem.sha256 || "n/a"} mono />
                <Meta label="Scan" value={selectedItem.scan_status || "n/a"} />
                <Meta label="Graph" value={selectedItem.graph_status || "n/a"} />
                <Meta label="Risk" value={selectedItem.risk || "unknown"} />
                <Meta label="Size" value={formatBytes(selectedItem.size_bytes)} />
                <Meta label="Binding" value={selectedItem.binding_status || relationSummaryLabel(selectedSummary)} />
                <Meta label="Drift" value={driftStateLabel(selectedItem)} />
              </div>

              <AssetRelationGraph
                item={selectedItem}
                relations={selectedRelations}
                selectedRelationId={selectedRelation?.relation_id || ""}
                onSelect={setSelectedRelationId}
              />

              <div className="asset-detail-grid">
                <DetailBlock title="Evidence">
                  {(selectedItem.evidence ?? []).length === 0 ? (
                    <div className="asset-browser-muted">No evidence recorded.</div>
                  ) : (
                    <div className="asset-evidence-list">
                      {(selectedItem.evidence ?? []).slice(0, 4).map((evidence, index) => (
                        <span key={`${selectedItem.asset_id}-e-${index}`}>
                          <strong>{evidence.kind}</strong>: {evidence.message}
                        </span>
                      ))}
                    </div>
                  )}
                </DetailBlock>

                <DetailBlock title="Asset actions">
                  <div className="asset-action-list">
                    {(selectedItem.recommended_actions ?? []).slice(0, 5).map((action) => (
                      <span key={action} className="mono">
                        {action}
                      </span>
                    ))}
                    {(selectedItem.recommended_actions ?? []).length === 0 ? (
                      <span className="asset-browser-muted">No recommended action.</span>
                    ) : null}
                  </div>
                </DetailBlock>

                <DriftControls
                  item={selectedItem}
                  selectedRelation={selectedRelation}
                  result={actionResults[`${selectedItem.asset_id}:drift`] ?? { state: "idle", message: "No manual state change recorded." }}
                  proposalResult={
                    actionResults[`${selectedItem.asset_id}:resolve-drift`] ?? { state: "idle", message: proposalStateLabel(selectedItem) }
                  }
                  onStateChange={(driftState) => recordDriftState(selectedItem, driftState)}
                  onResolve={() => queueResolveDrift(selectedItem, selectedRelation)}
                />

                <DetailBlock title="Backlog policy">
                  <div className="asset-policy-lines">
                    <span>{assetInbox.backlog_policy?.reason || "Create backlog rows only from selected assets."}</span>
                    <span className={selectedItem.backlog?.eligible ? "asset-policy-eligible" : "asset-browser-muted"}>
                      {selectedItem.backlog?.eligible ? "Eligible for backlog creation" : selectedItem.backlog?.reason || "Not eligible"}
                    </span>
                  </div>
                </DetailBlock>

                <HintBindingPanel
                  item={selectedItem}
                  nodeOptions={nodeOptions}
                  draft={
                    drafts[selectedItem.path] ?? {
                      targetNodeId: suggestedTargetNodeId(selectedItem, nodeOptions),
                      role: roleForAsset(selectedItem),
                    }
                  }
                  result={attachResults[selectedItem.path] ?? { state: "idle", message: "Not written." }}
                  snapshotId={snapshotId}
                  onUpdate={(patch) => updateDraft(selectedItem.path, patch)}
                  onWrite={() => writeHint(selectedItem)}
                />
              </div>
            </>
          ) : (
            <div className="asset-browser-empty asset-browser-empty-large">
              No assets are available in this snapshot.
            </div>
          )}
        </main>

        <aside className="asset-relations-panel">
          <div className="asset-selector-head">
            <div>
              <div className="asset-panel-title">Mount relations</div>
              <div className="asset-panel-meta">
                {selectedSummary ? relationSummaryLabel(selectedSummary) : "Select an asset"}
              </div>
            </div>
          </div>
          {selectedItem ? (
            <RelationPanel
              item={selectedItem}
              relations={selectedRelations}
              selectedRelationId={selectedRelation?.relation_id || ""}
              actionResults={actionResults}
              onSelect={setSelectedRelationId}
              onPropose={proposeRelationAction}
            />
          ) : (
            <div className="asset-browser-empty">Select an asset to inspect graph bindings.</div>
          )}
        </aside>
      </section>

      <section className="section">
        <div className="section-head">
          Batch actions <span className="head-hint">read-only preview in this slice</span>
        </div>
        {(assetInbox.batch_actions ?? []).length === 0 ? (
          <div className="empty empty-compact">No batch actions are advertised for this payload.</div>
        ) : (
          <div className="asset-action-grid">
            {(assetInbox.batch_actions ?? []).map((action) => (
              <ActionCard key={action.action} action={action} />
            ))}
          </div>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          Matching assets <span className="head-hint">secondary list, sorted by state and path</span>
        </div>
        {visibleItems.length === 0 ? (
          <div className="empty">No assets match the current filters.</div>
        ) : (
          <div className="asset-compact-list">
            {visibleItems.slice(0, 40).map((item) => (
              <button
                key={`compact-${item.asset_id}`}
                type="button"
                className="asset-compact-row"
                onClick={() => setSelectedAssetId(item.asset_id)}
              >
                <span className="mono">{item.path}</span>
                <span>{labelForKind(item.asset_kind)}</span>
                <span>{STATUS_LABELS[item.asset_status] ?? item.asset_status}</span>
                <span>{relationSummaryLabel(summarizeRelations(item, deriveRelations(item)))}</span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function AssetRelationGraph(props: {
  item: AssetInboxItem;
  relations: RelationView[];
  selectedRelationId: string;
  onSelect(relationId: string): void;
}) {
  const relationSlots = props.relations.slice(0, 8);
  const centerX = 180;
  const centerY = 128;
  const radius = 92;
  return (
    <section className="asset-one-hop-graph" aria-label="Asset relation graph">
      <div className="asset-graph-head">
        <div>
          <div className="asset-detail-block-title">One-hop relation graph</div>
          <div className="asset-panel-meta">Selected asset root with related graph nodes</div>
        </div>
        <RelationLegend />
      </div>
      <div className="asset-graph-canvas">
        <svg viewBox="0 0 360 256" role="img" aria-label={`One-hop graph for ${props.item.path}`}>
          {relationSlots.map((relation, index) => {
            const angle = (Math.PI * 2 * index) / Math.max(relationSlots.length, 1) - Math.PI / 2;
            const x = centerX + Math.cos(angle) * radius;
            const y = centerY + Math.sin(angle) * radius;
            return (
              <g key={`${relation.relation_id}-edge`}>
                <line
                  x1={centerX}
                  y1={centerY}
                  x2={x}
                  y2={y}
                  className={`asset-graph-edge ${relationStatusClass(relation.status)}`}
                />
              </g>
            );
          })}
          <g>
            <circle cx={centerX} cy={centerY} r="38" className="asset-graph-root" />
            <text x={centerX} y={centerY - 4} textAnchor="middle" className="asset-graph-root-label">
              Asset
            </text>
            <text x={centerX} y={centerY + 12} textAnchor="middle" className="asset-graph-root-kind">
              {labelForKind(props.item.asset_kind)}
            </text>
          </g>
          {relationSlots.map((relation, index) => {
            const angle = (Math.PI * 2 * index) / Math.max(relationSlots.length, 1) - Math.PI / 2;
            const x = centerX + Math.cos(angle) * radius;
            const y = centerY + Math.sin(angle) * radius;
            const active = props.selectedRelationId === relation.relation_id;
            return (
              <g
                key={relation.relation_id}
                className={`asset-graph-node-wrap${active ? " active" : ""}`}
                onClick={() => props.onSelect(relation.relation_id)}
              >
                <circle cx={x} cy={y} r="28" className={`asset-graph-node ${relationStatusClass(relation.status)}`} />
                <text x={x} y={y + 4} textAnchor="middle" className="asset-graph-node-label">
                  {graphNodeLabel(relation)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </section>
  );
}

function RelationLegend() {
  return (
    <div className="asset-relation-legend">
      {(["accepted", "candidate", "unbound", "stale_drift", "impact_pending"] as const).map((status) => (
        <span key={status} className={`asset-legend-item ${relationStatusClass(status)}`}>
          {RELATION_LABELS[status]}
        </span>
      ))}
    </div>
  );
}

function DriftControls(props: {
  item: AssetInboxItem;
  selectedRelation: RelationView | null;
  result: ActionResult;
  proposalResult: ActionResult;
  onStateChange(state: DriftStateName): void;
  onResolve(): void;
}) {
  const currentState = normalizeDriftState(props.item.drift?.state);
  return (
    <DetailBlock title="Drift controls">
      <div className="asset-drift-control-grid">
        {(["not_drifted", "suspected", "confirmed", "resolved", "waived"] as DriftStateName[]).map((state) => (
          <button
            key={state}
            type="button"
            className={`asset-drift-state-btn${currentState === state ? " active" : ""}`}
            disabled={props.result.state === "writing"}
            onClick={() => props.onStateChange(state)}
          >
            {DRIFT_LABELS[state]}
          </button>
        ))}
      </div>
      <button
        type="button"
        className="action-btn action-btn-primary asset-resolve-drift-btn"
        disabled={props.proposalResult.state === "writing"}
        onClick={props.onResolve}
      >
        {props.proposalResult.state === "writing" ? "Queueing..." : "Resolve Drift"}
      </button>
      <div className={`attach-state attach-state-${props.result.state}`}>{props.result.message}</div>
      <div className={`attach-state attach-state-${props.proposalResult.state}`}>{props.proposalResult.message}</div>
      {props.selectedRelation ? (
        <div className="asset-browser-muted">
          Target relation: {props.selectedRelation.target_node_id || RELATION_LABELS[props.selectedRelation.status] || "unbound"}
        </div>
      ) : null}
    </DetailBlock>
  );
}

function RelationPanel(props: {
  item: AssetInboxItem;
  relations: RelationView[];
  selectedRelationId: string;
  actionResults: Record<string, ActionResult>;
  onSelect(relationId: string): void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
}) {
  const accepted = props.relations.filter((relation) =>
    ["accepted", "impact_pending", "stale_drift"].includes(relation.status),
  );
  const candidates = props.relations.filter((relation) => relation.status === "candidate");
  const unbound = props.relations.filter((relation) => relation.status === "unbound");
  if (props.relations.length === 0) {
    return (
      <div className="asset-browser-empty">
        No accepted or candidate graph relations for <span className="mono">{props.item.path}</span>.
      </div>
    );
  }
  return (
    <div className="asset-relation-stack scrollbar-thin">
      <RelationGroup
        title="Accepted / drift"
        item={props.item}
        relations={accepted}
        empty="No accepted binding."
        selectedRelationId={props.selectedRelationId}
        actionResults={props.actionResults}
        onSelect={props.onSelect}
        onPropose={props.onPropose}
      />
      <RelationGroup
        title="Candidates"
        item={props.item}
        relations={candidates}
        empty="No candidate proposal."
        selectedRelationId={props.selectedRelationId}
        actionResults={props.actionResults}
        onSelect={props.onSelect}
        onPropose={props.onPropose}
      />
      <RelationGroup
        title="Unbound"
        item={props.item}
        relations={unbound}
        empty="No unbound relation."
        selectedRelationId={props.selectedRelationId}
        actionResults={props.actionResults}
        onSelect={props.onSelect}
        onPropose={props.onPropose}
      />
    </div>
  );
}

function RelationGroup(props: {
  title: string;
  item: AssetInboxItem;
  relations: RelationView[];
  empty: string;
  selectedRelationId: string;
  actionResults: Record<string, ActionResult>;
  onSelect(relationId: string): void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
}) {
  return (
    <section className="asset-relation-group">
      <div className="asset-relation-group-title">
        <span>{props.title}</span>
        <span className="asset-chip-count">{props.relations.length}</span>
      </div>
      {props.relations.length === 0 ? (
        <div className="asset-browser-muted">{props.empty}</div>
      ) : (
        props.relations.map((relation) => {
          const active = props.selectedRelationId === relation.relation_id;
          const addKey = `${relation.relation_id}:attach_to_node`;
          const removeKey = `${relation.relation_id}:remove_binding`;
          return (
          <div
            key={relation.relation_id}
            className={`asset-relation-card ${relationStatusClass(relation.status)}${active ? " active" : ""}`}
            onClick={() => props.onSelect(relation.relation_id)}
          >
            <div className="asset-relation-card-head">
              <span className="mono">{relation.target_node_id || "unbound"}</span>
              <span className={`asset-relation-state ${relationStatusClass(relation.status)}`}>
                {RELATION_LABELS[relation.status] ?? relation.status}
              </span>
            </div>
            {relation.target_title ? <div className="asset-relation-title">{relation.target_title}</div> : null}
            <div className="asset-relation-meta">
              <MetaPill label="Role" value={relation.role || "n/a"} />
              <MetaPill label="Source" value={relation.source || "n/a"} />
              <MetaPill label="Evidence" value={relation.evidence_kind || "n/a"} />
              <MetaPill label="Strength" value={relation.binding_strength || "n/a"} />
              <MetaPill label="Scope" value={formatImpactScope(relation.impact_scope)} />
              <MetaPill label="Review" value={relation.review_required ? "required" : "not required"} />
              <MetaPill label="Drift" value={relation.drift_state || "not_drifted"} />
            </div>
            {relation.proposal_hash ? <div className="asset-relation-hash mono">{relation.proposal_hash}</div> : null}
            <div className="asset-relation-actions">
              {relation.status === "candidate" && relation.target_node_id ? (
                <button
                  type="button"
                  className="action-btn"
                  disabled={props.actionResults[addKey]?.state === "writing"}
                  onClick={(event) => {
                    event.stopPropagation();
                    props.onPropose(props.item, relation, "attach_to_node");
                  }}
                >
                  Add relation
                </button>
              ) : null}
              {["accepted", "impact_pending", "stale_drift"].includes(relation.status) ? (
                <button
                  type="button"
                  className="action-btn"
                  disabled={props.actionResults[removeKey]?.state === "writing"}
                  onClick={(event) => {
                    event.stopPropagation();
                    props.onPropose(props.item, relation, "remove_binding");
                  }}
                >
                  Propose remove
                </button>
              ) : null}
              {props.actionResults[addKey]?.message ? (
                <span className={`attach-state attach-state-${props.actionResults[addKey].state}`}>
                  {props.actionResults[addKey].message}
                </span>
              ) : null}
              {props.actionResults[removeKey]?.message ? (
                <span className={`attach-state attach-state-${props.actionResults[removeKey].state}`}>
                  {props.actionResults[removeKey].message}
                </span>
              ) : null}
            </div>
          </div>
          );
        })
      )}
    </section>
  );
}

function HintBindingPanel(props: {
  item: AssetInboxItem;
  nodeOptions: NodeRecord[];
  draft: AttachDraft;
  result: AttachResult;
  snapshotId: string;
  onUpdate(patch: Partial<AttachDraft>): void;
  onWrite(): void;
}) {
  const supported = canDirectWriteHint(props.item.path);
  const hintable = isHintable(props.item);
  const disabledReason = governanceHintDisabledReason(props.item, supported, props.nodeOptions.length, props.snapshotId);
  const disabled =
    !props.snapshotId ||
    !supported ||
    !hintable ||
    props.nodeOptions.length === 0 ||
    props.result.state === "writing" ||
    props.result.state === "written_uncommitted";
  return (
    <DetailBlock title="Governance hint">
      <div className="asset-hint-controls">
        <select
          value={props.draft.role}
          onChange={(event) => props.onUpdate({ role: event.target.value as AttachRole })}
          disabled={props.result.state === "writing" || props.result.state === "written_uncommitted"}
        >
          <option value="doc">doc</option>
          <option value="test">test</option>
          <option value="config">config</option>
        </select>
        <select
          value={props.draft.targetNodeId}
          onChange={(event) => props.onUpdate({ targetNodeId: event.target.value })}
          disabled={props.result.state === "writing" || props.result.state === "written_uncommitted"}
        >
          {props.nodeOptions.map((node) => (
            <option key={node.node_id} value={node.node_id}>
              {node.title || node.node_id} - {node.node_id}
            </option>
          ))}
        </select>
        <button
          className="action-btn action-btn-primary"
          disabled={disabled}
          onClick={props.onWrite}
          title={disabledReason || "Write governance hint into the file"}
        >
          {props.result.state === "writing" ? "Writing..." : "Write hint"}
        </button>
      </div>
      <div className={`attach-state attach-state-${props.result.state}`}>
        {disabledReason || props.result.message}
      </div>
    </DetailBlock>
  );
}

function DetailBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="asset-detail-block">
      <div className="asset-detail-block-title">{title}</div>
      {children}
    </section>
  );
}

function Meta({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="asset-meta-cell">
      <span>{label}</span>
      <strong className={mono ? "mono" : undefined}>{value}</strong>
    </div>
  );
}

function MetaPill({ label, value }: { label: string; value: string }) {
  return (
    <span className="asset-meta-pill">
      <strong>{label}</strong>
      {value}
    </span>
  );
}

function StatusChipStrip({ statuses }: { statuses: Record<string, number> }) {
  const entries = Object.entries(statuses)
    .filter(([, count]) => count > 0)
    .sort((a, b) => statusWeight(a[0]) - statusWeight(b[0]))
    .slice(0, 3);
  if (entries.length === 0) return <span className="asset-group-statuses muted">No files</span>;
  return (
    <span className="asset-group-statuses">
      {entries.map(([status, count]) => (
        <span key={status}>
          {STATUS_LABELS[status] ?? status} {count}
        </span>
      ))}
    </span>
  );
}

function ActionCard({ action }: { action: AssetInboxBatchAction }) {
  return (
    <div className="asset-action-card">
      <div className="asset-action-head">
        <span>{action.label || action.action}</span>
        <span className={action.mutates_source ? "asset-action-danger" : "asset-action-safe"}>
          {action.mutates_source ? "source write" : "read/queue"}
        </span>
      </div>
      <div className="asset-action-meta">
        {(action.allowed_statuses ?? []).map((status) => (
          <span key={status}>{STATUS_LABELS[status] ?? status}</span>
        ))}
      </div>
      <button className="action-btn" disabled title="Mutation actions are not enabled in this slice">
        Disabled
      </button>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`score-card tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function buildGroups(assetInbox: AssetInboxResponse, items: AssetInboxItem[]): Record<AssetGroupId, GroupView> {
  const groups = Object.fromEntries(GROUP_ORDER.map((id) => [id, emptyGroup(id)])) as Record<AssetGroupId, GroupView>;
  const backendGroups = assetInbox.asset_groups ?? [];
  if (backendGroups.length > 0) {
    for (const backendGroup of backendGroups) {
      const id = normalizeGroupId(backendGroup.group_id ?? backendGroup.group);
      const target = groups[id];
      const itemIds = new Set([
        ...(backendGroup.item_ids ?? []),
        ...(backendGroup.items ?? []).map((item) => item.asset_id),
      ].filter(Boolean));
      const paths = new Set([
        ...(backendGroup.paths ?? []),
        ...(backendGroup.items ?? []).map((item) => item.path),
      ].filter(Boolean));
      for (const item of items) {
        if (itemIds.has(item.asset_id) || paths.has(item.path)) target.itemIds.add(item.asset_id);
      }
      target.count = Math.max(target.count, backendGroup.count ?? target.itemIds.size);
      target.statuses = mergeStatusCounts(target.statuses, backendGroup.status_counts ?? backendGroup.statuses ?? {});
    }
  }

  for (const item of items) {
    const id = normalizeGroupId(item.asset_kind, item.asset_status, item.path);
    groups.ALL.itemIds.add(item.asset_id);
    groups.ALL.statuses[item.asset_status] = (groups.ALL.statuses[item.asset_status] ?? 0) + 1;
    if (backendGroups.length === 0 || !groups[id].itemIds.has(item.asset_id)) {
      groups[id].itemIds.add(item.asset_id);
      groups[id].statuses[item.asset_status] = (groups[id].statuses[item.asset_status] ?? 0) + 1;
    }
  }

  for (const id of GROUP_ORDER) {
    if (id === "ALL") {
      groups[id].count = items.length;
    } else if (groups[id].count === 0 || groups[id].itemIds.size > groups[id].count) {
      groups[id].count = groups[id].itemIds.size;
    }
  }
  return groups;
}

function emptyGroup(id: AssetGroupId): GroupView {
  return { id, label: GROUP_LABELS[id], count: 0, itemIds: new Set(), statuses: {} };
}

function mergeStatusCounts(left: Record<string, number>, right: Record<string, number>): Record<string, number> {
  const merged = { ...left };
  for (const [status, count] of Object.entries(right)) {
    merged[status] = (merged[status] ?? 0) + count;
  }
  return merged;
}

function filterAssets(
  items: AssetInboxItem[],
  groups: Record<AssetGroupId, GroupView>,
  groupFilter: AssetGroupId,
  query: string,
): AssetInboxItem[] {
  const q = query.trim().toLowerCase();
  const group = groups[groupFilter];
  return items.filter((item) => {
    if (groupFilter !== "ALL" && group && !group.itemIds.has(item.asset_id)) return false;
    if (!q) return true;
    const relations = deriveRelations(item);
    const hay = [
      item.path,
      item.asset_kind,
      item.asset_status,
      item.graph_status,
      item.scan_status,
      ...(item.evidence ?? []).map((evidence) => `${evidence.kind} ${evidence.message}`),
      ...relations.map((relation) =>
        [
          relation.status,
          relation.role,
          relation.target_node_id,
          relation.target_title,
          relation.source,
          relation.evidence_kind,
          relation.proposal_hash,
        ].join(" "),
      ),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

function deriveRelations(item: AssetInboxItem): RelationView[] {
  if ((item.mount_relations ?? []).length > 0) {
    return (item.mount_relations ?? []).map((relation, index) => ({
      ...relation,
      relation_id: relation.relation_id || `${item.asset_id}:relation:${index}`,
      status: relation.status || "candidate",
      target_node_id: relation.target_node_id,
    }));
  }
  const accepted = (item.accepted_bindings ?? []).map((binding): RelationView => ({
    relation_id: `${item.asset_id}:accepted:${binding.node_id}:${binding.role}`,
    status: "accepted",
    role: binding.role,
    target_node_id: binding.node_id,
    target_title: binding.title,
    source: binding.source,
    evidence_kind: "accepted_binding",
    binding_strength: "strong",
    impact_scope: "accepted_binding",
    review_required: false,
  }));
  const candidates = (item.binding_candidates ?? []).map((candidate, index): RelationView => ({
    relation_id: `${item.asset_id}:candidate:${candidate.proposal_hash || index}`,
    status: "candidate",
    role: normalizeAssetKind(candidate.asset_kind),
    target_node_id: candidate.target_node_id,
    target_title: candidate.target_title,
    source: candidate.source,
    evidence_kind: candidate.evidence_kind,
    proposal_hash: candidate.proposal_hash,
    binding_strength: candidate.precheck?.binding_strength,
    impact_scope: candidate.precheck?.mode || "proposal",
    review_required: candidate.precheck?.decision !== "accepted",
  }));
  return [...accepted, ...candidates];
}

function summarizeRelations(item: AssetInboxItem, relations: RelationView[]) {
  const accepted = relations.filter((relation) => relation.status === "accepted").length;
  const candidate = relations.length - accepted;
  return {
    accepted_count: item.relation_summary?.accepted_count ?? accepted,
    candidate_count: item.relation_summary?.candidate_count ?? candidate,
    relation_count: item.relation_summary?.relation_count ?? relations.length,
    impact_scope_count:
      item.relation_summary?.impact_scope_count ??
      relations.filter((relation) => relation.status === "accepted" || relation.impact_scope).length,
    review_required_count:
      item.relation_summary?.review_required_count ?? relations.filter((relation) => relation.review_required).length,
  };
}

function relationSummaryLabel(summary: ReturnType<typeof summarizeRelations> | null): string {
  if (!summary) return "0 relations";
  return `${summary.accepted_count ?? 0} accepted / ${summary.candidate_count ?? 0} candidate`;
}

function driftStateLabel(item: AssetInboxItem): string {
  const state = normalizeDriftState(item.drift?.state);
  const source = item.drift?.source ? ` (${item.drift.source})` : "";
  return `${DRIFT_LABELS[state]}${source}`;
}

function proposalStateLabel(item: AssetInboxItem): string {
  if (item.drift_proposal?.proposal_id) {
    return `Latest proposal ${item.drift_proposal.status || "recorded"} / ${item.drift_proposal.ai_status || "ai n/a"}`;
  }
  if (item.drift?.impact_pending) return "Impact reminder present; Resolve Drift will queue or record a proposal.";
  return "No drift proposal queued.";
}

function normalizeDriftState(state?: string): DriftStateName {
  if (state === "suspected" || state === "confirmed" || state === "resolved" || state === "waived") return state;
  return "not_drifted";
}

function relationStatusClass(status?: string): string {
  if (status === "accepted") return "accepted";
  if (status === "candidate") return "candidate";
  if (status === "impact_pending") return "impact-pending";
  if (status === "stale_drift") return "stale-drift";
  if (status === "unbound") return "unbound";
  return "unbound";
}

function graphNodeLabel(relation: RelationView): string {
  if (!relation.target_node_id) return "Unbound";
  const value = relation.target_node_id.replace(/^L\d+\./, "L");
  return value.length > 9 ? `${value.slice(0, 8)}...` : value;
}

function normalizeGroupId(kind?: string, status?: string, path?: string): AssetGroupId {
  const normalized = normalizeAssetKind(kind);
  if (normalized === "doc") return "doc";
  if (normalized === "test") return "test";
  if (normalized === "config") return "config";
  if (normalized === "source") return "source";
  if (normalized === "generated" || normalized === "ignored" || status === "ignored" || status === "archive") {
    return "generated";
  }
  const lowerPath = (path || "").toLowerCase();
  if (lowerPath.includes("/test") || lowerPath.endsWith(".test.ts") || lowerPath.endsWith(".spec.ts")) return "test";
  if (lowerPath.endsWith(".md") || lowerPath.endsWith(".mdx") || lowerPath.includes("/docs/")) return "doc";
  if (/\.(ya?ml|toml|ini|cfg|json)$/.test(lowerPath)) return "config";
  return "other";
}

function normalizeAssetKind(kind?: string): string {
  const value = (kind || "").trim().toLowerCase();
  if (value === "index_doc") return "doc";
  if (value === "unknown") return "other";
  return value;
}

function labelForKind(kind?: string): string {
  const normalized = normalizeAssetKind(kind);
  if (normalized === "doc") return "Doc";
  if (normalized === "test") return "Test";
  if (normalized === "config") return "Config";
  if (normalized === "source") return "Source";
  if (normalized === "generated") return "Generated";
  if (normalized === "ignored") return "Ignored";
  return "Other";
}

function roleForAsset(item?: AssetInboxItem): AttachRole {
  const kind = normalizeAssetKind(item?.asset_kind);
  if (kind === "test") return "test";
  if (kind === "config") return "config";
  return "doc";
}

function isHintable(item: AssetInboxItem): boolean {
  const kind = normalizeAssetKind(item.asset_kind);
  return (
    ["doc", "test", "config"].includes(kind) &&
    (item.accepted_bindings ?? []).length === 0 &&
    item.scan_status === "orphan" &&
    ["doc_unbound"].includes(item.asset_status)
  );
}

function governanceHintDisabledReason(
  item: AssetInboxItem,
  supported: boolean,
  nodeCount: number,
  snapshotId: string,
): string {
  if (!snapshotId) return "Select a snapshot before writing governance hints.";
  if (!supported) return "Direct write unsupported for this file type.";
  const kind = normalizeAssetKind(item.asset_kind);
  if (!["doc", "test", "config"].includes(kind)) return "Governance hint is only available for doc/test/config assets.";
  if ((item.accepted_bindings ?? []).length > 0) return "Governance hint is disabled because this asset is already attached.";
  if (item.scan_status !== "orphan") {
    return `Governance hint is disabled: backend requires scan_status=orphan, current scan_status=${item.scan_status || "unknown"}.`;
  }
  if (item.asset_status !== "doc_unbound") {
    return `Governance hint is disabled for ${STATUS_LABELS[item.asset_status] ?? item.asset_status}; use proposal/review actions instead.`;
  }
  if (nodeCount === 0) return "No target nodes are available in this snapshot.";
  return "";
}

function suggestedTargetNodeId(item: AssetInboxItem | undefined, nodes: NodeRecord[]): string {
  const candidateTarget = item?.binding_candidates?.find((candidate) => candidate.target_node_id)?.target_node_id;
  if (candidateTarget && nodes.some((node) => node.node_id === candidateTarget)) return candidateTarget;
  return nodes[0]?.node_id ?? "";
}

function canDirectWriteHint(path: string): boolean {
  const lower = path.toLowerCase();
  const name = lower.split(/[\\/]/).pop() || "";
  if (name === "dockerfile" || name === "makefile") return true;
  return [
    ".md",
    ".mdx",
    ".html",
    ".htm",
    ".py",
    ".pyw",
    ".sh",
    ".bash",
    ".ps1",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".txt",
    ".rst",
    ".adoc",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
  ].some((suffix) => lower.endsWith(suffix));
}

function compareAssets(a: AssetInboxItem, b: AssetInboxItem): number {
  const byGroup = GROUP_ORDER.indexOf(normalizeGroupId(a.asset_kind, a.asset_status, a.path)) -
    GROUP_ORDER.indexOf(normalizeGroupId(b.asset_kind, b.asset_status, b.path));
  if (byGroup !== 0) return byGroup;
  const byState = statusWeight(a.asset_status) - statusWeight(b.asset_status);
  if (byState !== 0) return byState;
  return a.path.localeCompare(b.path);
}

function statusWeight(status: string): number {
  const order = [
    "source_orphan",
    "doc_unbound",
    "doc_candidate",
    "test_candidate",
    "config_pending_decision",
    "impact_pending",
    "drift_suspected",
    "drift_confirmed",
    "stale",
    "accepted",
    "drift_resolved",
    "drift_waived",
    "ignored",
    "archive",
  ];
  const index = order.indexOf(status as AssetInboxStatus);
  return index < 0 ? 99 : index;
}

function assetStatusClass(status: string): string {
  if (status === "accepted") return "qa";
  if (status === "impact_pending" || status === "drift_confirmed") return "failed";
  if (status === "drift_suspected") return "running";
  if (status === "drift_resolved" || status === "drift_waived") return "qa";
  if (status === "ignored" || status === "archive") return "muted";
  if (status === "source_orphan" || status === "stale") return "failed";
  if (status === "doc_candidate" || status === "test_candidate" || status === "config_pending_decision") return "running";
  return "queued";
}

function countStatus(assetInbox: AssetInboxResponse, status: string): number {
  return assetInbox.summary?.by_status?.[status] ?? 0;
}

function formatBytes(size?: number): string {
  if (size == null || Number.isNaN(size)) return "n/a";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatImpactScope(scope?: boolean | string | string[]): string {
  if (Array.isArray(scope)) return scope.length === 0 ? "n/a" : scope.join(", ");
  if (scope === true) return "yes";
  if (scope === false) return "no";
  return scope || "n/a";
}
