# Observer-Safe Expertise Routing

This reference is safe to include in public skills and worker-adjacent docs. It
does not include private strategy or local-only reasoning. It describes
how an observer should turn specialized judgment into governed work.

## Operating Rule

When a task needs specialized judgment, do not rely on a vague prompt. Choose a
review contract pack, collect the required artifacts, run the review, validate
the structured output, and convert accepted findings into backlog rows or
contract requirements.

## Default Review Flow

1. Identify the surface being judged: product UX, onboarding, demo credibility,
   architecture, security, evidence integrity, or documentation clarity.
2. Resolve the matching review context or contract template. For development
   work, prefer the source-controlled review packs for architecture/data
   continuity, frontend/UI implementation, or QA evidence gate when those
   domains apply.
3. Provide concrete artifacts: screenshots, URLs, source files, backlog rows,
   timeline events, graph traces, or reproduced errors.
4. Require structured findings with severity, evidence references, user impact,
   recommendation, and acceptance impact.
5. Validate the review output before using it for gates or backlog conversion.
6. Keep the expert review separate from implementation. Review first, then
   patch from accepted findings.

## Ordinary-User Product Bias

For ordinary-user and vibe-coding surfaces, prioritize:

- whether the user's original request is visible;
- whether the current state of each request is visible;
- whether the next action is obvious;
- whether the product preview or acceptance surface is easy to find;
- whether engineering concepts are hidden until needed.

Developer-grade evidence can remain available in engineer mode, but it should
not be the first surface for ordinary users.

## Worker Boundary

Workers should receive only the context needed to execute their contract. They
do not need private product strategy, broad founder reasoning, or unrelated
review history. The observer translates judgment into scoped requirements,
acceptance criteria, target files, and review gates.
