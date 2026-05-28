# Simple Requirement Workspace Design

Status: P0 desktop implementation contract, 2026-05-28

Backlog: SIMPLE-MODE-REQUEST-FIRST-UE-GATE-20260528

## Goal

Simple Mode is a request workspace for ordinary users. The first screen answers:

- What did I ask for?
- What is happening with it?
- What should I do next, or what am I waiting on?

Desktop web is the P0 validation target. Mobile first-viewport validation is out
of scope for this P0 because there is no current mobile app usage scenario.

## First Viewport

The first content block after the project shell must be Your requests. When at
least one request exists, show request cards before helper status, counters,
system health, or proof details.

Desktop order:

1. Project selector and page title.
2. Your requests request-card grid/list.
3. Compact helper status and request summary counters.
4. Request capture input and detailed lane tabs.
5. Secondary proof/details surfaces.

Do not make the user open a modal to see their saved requests, request status,
or next step.

## Request Card Contract

Every visible request card must show these three fields:

- Original request excerpt: a concise excerpt of the user's own wording.
- User-facing status: one of the approved labels below.
- Next-action or waiting line: one sentence that says what the user can do now
  or what the product is waiting for.

Optional card fields:

- saved time or completed time
- friendly title generated from the request
- Details action
- retry action when the request needs attention

The card must never rely only on an interpreted title. Users need the original
request wording visible so they can verify the work still maps to what they
asked for.

## Lifecycle Lanes

Use request-centered lane names and copy:

- Saved requests: newly captured or manually reviewed requests.
- Waiting to start: confirmed requests that are ready but not active yet.
- Working: requests currently being handled.
- Ready for review: requests that need the user's decision or acceptance.
- Done: completed requests.
- Needs attention: requests blocked by missing context or a failed helper step.

Queued, in-progress, ready-for-review, and done cards must carry one of:

- `original_request_excerpt` from the raw request source; or
- an explicit missing-source state: "Original request text is not linked yet."

Do not synthesize the original request from implementation notes, task details,
or proof metadata. If the source is missing, show the missing-source state and
keep the item visible.

## User-Facing Status Copy

Approved default labels:

- Saved
- Needs review
- Ready to start
- Waiting to start
- Working
- Ready for your review
- Done
- Needs attention

Approved next-action and waiting-line patterns:

- "Review the summary before work starts."
- "Add the missing details so this can continue."
- "Waiting for a helper to pick this up."
- "Work is underway. No action needed right now."
- "Review the result and choose approve or request changes."
- "Finished. You can open details for proof."

Avoid default Simple Mode copy that exposes operator vocabulary, including
worker, execution queue, audit, backlog, commit, graph, reconcile, and chain.
Those terms may appear only in secondary proof/details surfaces intended for
operators or engineers.

## Details And Proof Boundary

The Details surface opens from a request card and may show:

- full original request
- interpreted summary
- missing context
- acceptance criteria
- action history in friendly language
- proof details for engineers

Secondary proof/details may include operator fields such as backlog id, commit
hash, graph evidence, audit timeline, reconcile state, and chain trailers. These
fields must not be required to understand the default request list.

## Helper Status

The compact helper status near the top of the page uses ordinary-user copy:

- Helper connected
- Waiting for helper
- Request saved
- Waiting to start
- Working
- Done
- Needs attention

Button clicks only save or request an action. The UI must not claim work has
started until the system confirms it is running.

## Empty And Error States

Empty state:

- Keep the new request input available.
- Use copy such as "No saved requests yet."
- Do not introduce graph, backlog, queue, or audit language.

Missing helper:

- Keep saved request cards visible.
- Allow manual review where possible.
- Disable helper-only actions with "Waiting for helper."

Failure:

- Keep the request card in its lane.
- Show Needs attention.
- Provide Retry and Details.
- Explain the next user action in plain language.

## Desktop Success Checks

The P0 desktop screenshot should pass these checks:

- Your requests is the first content block when requests exist.
- At least one request card is visible above helper status, counters, or proof
  details.
- Each visible card has original request excerpt, status, and next-action or
  waiting line.
- Waiting to start, Working, Ready for review, and Done cards preserve original
  request wording or show the explicit missing-source state.
- Default copy avoids worker, execution queue, audit, backlog, commit, graph,
  reconcile, and chain outside Details/proof.
- No mobile validation is claimed for this P0.
