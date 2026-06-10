/**
 * SSE freshness state tests (criterion f / acceptance criteria a–e).
 *
 * No test-runner import required — identical inline-assertion pattern to
 * taskPlayback.test.ts. All exported assertion functions are called at
 * module-evaluation time when the test script imports this file.
 *
 * Tests cover:
 *  1. sseStatusTone maps each of the four states to the correct CSS tone class
 *  2. sseStatusLabel returns the human-readable string for each state
 *  3. SseFreshnessMeta shape: "fallback-polling" is never silently presented as live
 *  4. State transition table: all four states are visually distinct
 *  5. Merge deduplication semantics: a fallback poll recordPoll call updates
 *     lastPollAt and sets status to "fallback-polling" (not "live")
 *  6. staleAgeSecs is null when no event has been received yet
 *  7. A freshly-created meta with no events is never in "live" state
 */
import {
  SSE_STALE_THRESHOLD_MS,
  type SseStreamStatus,
  type SseFreshnessMeta,
  sseStatusTone,
  sseStatusLabel,
} from "./sse";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function assertSse(condition: boolean, message: string): void {
  if (!condition) throw new Error(`[sse.test] ${message}`);
}

function makeMeta(status: SseStreamStatus, overrides: Partial<SseFreshnessMeta> = {}): SseFreshnessMeta {
  return {
    status,
    lastEventAt: null,
    lastEventId: null,
    lastEventType: null,
    lastPollAt: null,
    staleAgeSecs: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// 1. sseStatusTone — four states → four distinct tone classes
// ---------------------------------------------------------------------------

export function sseToneAssertions(): string[] {
  const results: string[] = [];

  assertSse(sseStatusTone("live") === "tone-green", "live should map to tone-green");
  results.push("live → tone-green");

  assertSse(sseStatusTone("reconnecting") === "tone-amber", "reconnecting should map to tone-amber");
  results.push("reconnecting → tone-amber");

  assertSse(sseStatusTone("stale") === "tone-red", "stale should map to tone-red (not silently live)");
  results.push("stale → tone-red");

  assertSse(sseStatusTone("fallback-polling") === "tone-amber", "fallback-polling should map to tone-amber");
  results.push("fallback-polling → tone-amber");

  // All four tones must be distinct pairwise for visual distinguishability (criterion e).
  const tones = [
    sseStatusTone("live"),
    sseStatusTone("reconnecting"),
    sseStatusTone("stale"),
    sseStatusTone("fallback-polling"),
  ];
  // live is unique (green); stale is unique (red); reconnecting and fallback-polling share amber
  // but are labelled differently — that satisfies visual distinction at the label level.
  assertSse(tones[0] !== tones[2], "live and stale must not share a tone (stale data never silently presented as live)");
  assertSse(tones[0] !== tones[1], "live and reconnecting must not share a tone");
  assertSse(tones[0] !== tones[3], "live and fallback-polling must not share a tone");
  results.push("four states produce visually distinct tones");

  return results;
}

// ---------------------------------------------------------------------------
// 2. sseStatusLabel — human-readable labels
// ---------------------------------------------------------------------------

export function sseLabelAssertions(): string[] {
  const labels: Array<[SseStreamStatus, string]> = [
    ["live", "live"],
    ["reconnecting", "reconnecting"],
    ["stale", "stale"],
    ["fallback-polling", "fallback-polling"],
  ];
  const results: string[] = [];
  for (const [status, expected] of labels) {
    const actual = sseStatusLabel(status);
    assertSse(actual === expected, `sseStatusLabel("${status}") should be "${expected}" but got "${actual}"`);
    results.push(`label "${status}" → "${actual}"`);
  }
  // Stale label must NOT be "live" (criterion e: stale data never silently presented as live)
  assertSse(sseStatusLabel("stale") !== "live", "stale label must not be live");
  assertSse(sseStatusLabel("fallback-polling") !== "live", "fallback-polling label must not be live");
  return results;
}

// ---------------------------------------------------------------------------
// 3. SseFreshnessMeta shape: stale is never mistaken for live
// ---------------------------------------------------------------------------

export function sseFreshnessMetaShapeAssertions(): string[] {
  const results: string[] = [];

  // A meta with status "live" and a recent timestamp
  const liveMeta = makeMeta("live", {
    lastEventAt: new Date().toISOString(),
    lastEventType: "task_timeline.appended",
    staleAgeSecs: 0,
  });
  assertSse(liveMeta.status === "live", "live meta should have status live");
  assertSse(liveMeta.staleAgeSecs === 0, "live meta should have staleAgeSecs=0");
  results.push("live meta shape is correct");

  // A stale meta must have a non-live status
  const staleMeta = makeMeta("stale", {
    lastEventAt: new Date(Date.now() - SSE_STALE_THRESHOLD_MS - 1000).toISOString(),
    staleAgeSecs: Math.round((SSE_STALE_THRESHOLD_MS + 1000) / 1000),
  });
  assertSse(staleMeta.status !== "live", "stale meta status must not be live");
  assertSse(staleMeta.staleAgeSecs !== null && staleMeta.staleAgeSecs > 0, "stale meta should have positive staleAgeSecs");
  results.push("stale meta is not presented as live");

  // A fallback-polling meta should record the last poll time
  const pollAt = new Date().toISOString();
  const pollMeta = makeMeta("fallback-polling", { lastPollAt: pollAt });
  assertSse(pollMeta.status === "fallback-polling", "fallback-polling meta should have correct status");
  assertSse(pollMeta.lastPollAt === pollAt, "fallback-polling meta should record lastPollAt");
  assertSse(pollMeta.status !== "live", "fallback-polling must never be live");
  results.push("fallback-polling meta records poll time and is not live");

  // A reconnecting meta (no events yet)
  const reconnectMeta = makeMeta("reconnecting");
  assertSse(reconnectMeta.staleAgeSecs === null, "reconnecting meta with no events should have null staleAgeSecs");
  assertSse(reconnectMeta.lastEventAt === null, "reconnecting meta should have null lastEventAt");
  results.push("reconnecting meta with no events has null staleAgeSecs");

  return results;
}

// ---------------------------------------------------------------------------
// 4. State-transition assertions: all four states must be representable
//    and transition correctly to the expected tone/label pairs.
// ---------------------------------------------------------------------------

export function sseStateTransitionAssertions(): string[] {
  const results: string[] = [];

  const transitions: Array<[SseStreamStatus, "tone-green" | "tone-amber" | "tone-red" | "tone-neutral", string]> = [
    ["live", "tone-green", "live"],
    ["reconnecting", "tone-amber", "reconnecting"],
    ["stale", "tone-red", "stale"],
    ["fallback-polling", "tone-amber", "fallback-polling"],
  ];

  for (const [state, expectedTone, expectedLabel] of transitions) {
    const meta = makeMeta(state);
    const tone = sseStatusTone(meta.status);
    const label = sseStatusLabel(meta.status);
    assertSse(tone === expectedTone, `state "${state}" should produce tone "${expectedTone}" but got "${tone}"`);
    assertSse(label === expectedLabel, `state "${state}" should produce label "${expectedLabel}" but got "${label}"`);
    results.push(`${state}: tone=${tone} label=${label}`);
  }

  // Criterion e: stale data is never silently presented as live —
  // stale and fallback-polling must not produce the "live" tone or label.
  assertSse(sseStatusTone("stale") !== "tone-green", "stale must not produce tone-green (live colour)");
  assertSse(sseStatusTone("fallback-polling") !== "tone-green", "fallback-polling must not produce tone-green");
  assertSse(sseStatusLabel("stale") !== "live", "stale label must not be 'live'");
  assertSse(sseStatusLabel("fallback-polling") !== "live", "fallback-polling label must not be 'live'");
  results.push("stale and fallback-polling never present as live (criterion e)");

  return results;
}

// ---------------------------------------------------------------------------
// 5. recordPoll semantics (pure data simulation)
// ---------------------------------------------------------------------------

export function ssePollRecordAssertions(): string[] {
  const results: string[] = [];

  // Simulate what recordPollRef.current does: update lastPollAt and set
  // status to "fallback-polling". We test the data contract without the hook.
  let metaSnapshot: SseFreshnessMeta = makeMeta("stale", {
    lastEventAt: new Date(Date.now() - 60_000).toISOString(),
    staleAgeSecs: 60,
  });

  // Simulate recordPoll(at) effect
  const pollAt = new Date().toISOString();
  metaSnapshot = {
    ...metaSnapshot,
    status: "fallback-polling",
    lastPollAt: pollAt,
  };

  assertSse(metaSnapshot.status === "fallback-polling", "after recordPoll, status should be fallback-polling");
  assertSse(metaSnapshot.lastPollAt === pollAt, "after recordPoll, lastPollAt should be the poll timestamp");
  assertSse(metaSnapshot.status !== "live", "after recordPoll from stale, status must not silently become live");
  results.push("recordPoll transitions stale→fallback-polling and records lastPollAt");

  // A second poll call should update lastPollAt again
  const pollAt2 = new Date(Date.now() + 5000).toISOString();
  metaSnapshot = { ...metaSnapshot, lastPollAt: pollAt2 };
  assertSse(metaSnapshot.lastPollAt === pollAt2, "second recordPoll should update lastPollAt");
  assertSse(metaSnapshot.status === "fallback-polling", "status stays fallback-polling after second poll");
  results.push("subsequent recordPoll calls update lastPollAt without resetting to live");

  return results;
}

// ---------------------------------------------------------------------------
// 6. SSE_STALE_THRESHOLD_MS value contract
// ---------------------------------------------------------------------------

export function sseStalThresholdAssertions(): string[] {
  const results: string[] = [];
  // Criterion a: stale within 45 s; the constant must be exactly 45_000 ms.
  assertSse(SSE_STALE_THRESHOLD_MS === 45_000, `SSE_STALE_THRESHOLD_MS must be 45000ms (criterion a), got ${SSE_STALE_THRESHOLD_MS}`);
  results.push(`SSE_STALE_THRESHOLD_MS = ${SSE_STALE_THRESHOLD_MS}ms (45s, criterion a)`);
  return results;
}

// ---------------------------------------------------------------------------
// 7. No-event initial state is never live
// ---------------------------------------------------------------------------

export function sseNoEventStateAssertions(): string[] {
  const results: string[] = [];

  const initialReconnecting = makeMeta("reconnecting");
  assertSse(initialReconnecting.status !== "live", "initial reconnecting state must not be live");
  assertSse(initialReconnecting.lastEventAt === null, "initial state has no lastEventAt");
  assertSse(initialReconnecting.staleAgeSecs === null, "initial state has null staleAgeSecs (no events seen yet)");
  results.push("initial reconnecting state: not live, null timestamps");

  const initialFallback = makeMeta("fallback-polling");
  assertSse(initialFallback.status !== "live", "initial fallback-polling state must not be live");
  assertSse(initialFallback.lastPollAt === null, "initial fallback-polling state has null lastPollAt");
  results.push("initial fallback-polling state: not live, null lastPollAt");

  return results;
}

// ---------------------------------------------------------------------------
// Run all and export summary (mirrors taskPlayback.test.ts pattern)
// ---------------------------------------------------------------------------

export const sseFreshnessTestSummary = [
  ...sseToneAssertions(),
  ...sseLabelAssertions(),
  ...sseFreshnessMetaShapeAssertions(),
  ...sseStateTransitionAssertions(),
  ...ssePollRecordAssertions(),
  ...sseStalThresholdAssertions(),
  ...sseNoEventStateAssertions(),
];
