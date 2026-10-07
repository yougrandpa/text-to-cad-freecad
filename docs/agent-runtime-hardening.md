# Agent runtime hardening inspired by OpenCode

This is a bounded refinement of TCAD's existing loop. It does not replace the
IR/FreeCAD/Gate architecture or grant additional tool permissions.

## Reference and adaptations

The official OpenCode repository was inspected at revision
`661853903b8ee8388e067f77b6372db18850a67e` (v2 branch, 2026-10-02).

1. OpenCode budgets the actual request, counts tool declarations and reserves
   generation capacity; its compaction boundaries preserve tool exchanges.
   TCAD now checks before every model call and retains whole assistant/tool
   groups, including IDs, arguments and reasoning fields. It refreshes CAD state
   from the store and pins the conversation history selected at turn start.
   Only older complete tool rounds can be omitted. This is explicit omission,
   **not** an invented LLM summary. See upstream
   [compaction](https://github.com/anomalyco/opencode/blob/661853903b8ee8388e067f77b6372db18850a67e/packages/core/src/session/compaction.ts#L685-L724)
   and [request budgeting](https://github.com/anomalyco/opencode/blob/661853903b8ee8388e067f77b6372db18850a67e/packages/core/src/session/compaction.ts#L853-L923).
2. OpenCode separates retryable provider failures and bounds provider-requested
   waits. TCAD now retries transport/timeouts, 408/409/429/5xx only; permanent
   authentication/invalid requests, known context overflow and insufficient
   quota fail immediately. Retry-After milliseconds/seconds/dates are validated
   and capped at 30 seconds. Cancellation remains interruptible and parsing a
   successful response cannot replay a billed request. See upstream
   [retry policy](https://github.com/anomalyco/opencode/blob/661853903b8ee8388e067f77b6372db18850a67e/packages/core/src/session/runner/retry.ts)
   and [error classification](https://github.com/anomalyco/opencode/blob/661853903b8ee8388e067f77b6372db18850a67e/packages/ai/src/provider-error.ts#L71-L110).

Two additional protections are TCAD-specific, not claims about current OpenCode:

- The same tool operation failing identically three times at an unchanged IR
  version stops the turn with a readable failure. Diagnostic reads and changed
  commit narration do not reset it; a successful IR mutation does. This never
  automatically retries a write, bypasses approval or declares success
- Completion requires the most recent passed Gate for the current model/version,
  with no later successful mutation. A pending approval or failed recommit wins
  over an earlier green report. The experimental fork_join strategy can retain
  historical candidates for diagnostics but cannot promote one without restoring
  and re-verifying its model

## Configuration and limits

- `loop.repeated_tool_failure_limit: 3`; `null` disables this stall guard
- Total step/token/time budgets remain separately configurable and unchanged
- Context uses the configured window and reserves the larger of the configured
  output tokens or 10% of the window. Counts are UTF-8-based estimates, not exact
  provider token counts. Tool schemas and protocol fields count toward the budget
- At least the latest whole tool batch and following narration survive. All
  selected prior user/summary context and the current request survive unchanged
- If authoritative state cannot be refreshed, or protected context still cannot
  fit, the turn fails before another provider call. It does not silently delete
  requirements, split tool pairs, or repeatedly submit an oversized prompt
- There is no new summarization model, vector store, permission grant, external
  checkpoint service or production-provider authentication flow

## Verification

Regression coverage includes transient/permanent status matrices, finite retry
headers, cancellation, malformed successful responses, whole-batch protocol
validation, UTF-8/schema budgeting, preserved reasoning, repeated compaction,
unchanged initial history, duplicate current-message prevention, repeated failures
across reads, genuine IR progress, engine reuse, and pass→edit/pass→approval/
pass→failed-recommit cases across all three strategies.

The complete native FreeCAD contract suite is rerun with the Linux adapter.
The first phase's real-agent two-turn CAD test remains documented separately in
[historical preview acceptance](../review/history/interactive-preview-acceptance.md); this optimization's deterministic transport
and loop tests must not be described as a fresh commercial-provider test.

Final integrated run (2026-10-02): **1,252 Python tests passed, 6 skipped**;
**17 Node frontend runtime tests passed**. Compileall and whitespace checks
passed. The six skips remain four unconfigured commercial-provider checks,
one macOS backend check, and one legacy embedded-FreeCADCmd flag probe.

## Local repair and design intent (P0-2 / P1-5)

Two additions to the same bounded loop:

- **Retained rejected calls.** A call rejected for *missing* fields is kept
  (`LoopEngine._retained_calls`); a later reply that supplies only the missing
  fields is merged over the retained payload and re-runs the ordinary path —
  hook dispatch, access checks, schema validation — so retention is a
  convenience for the model, never a bypass. Observed motivation: a model
  told to "resend the payload" rewrote a compact-recipe batch and swapped its
  `parts` value into `topic`. A complete call supersedes the retained one;
  the state resets each turn; type errors are never accumulated.
- **Part plan + intent check.** `ir_plan` records a short part plan
  (goal + origin: user/model) when the design starts; `ir_commit` compares it
  against the built IR and reports planned-but-missing parts, and a
  user-required part the model marked simplified/dropped is a *design
  degradation* — recorded in the commit notes and appended to the completion
  review's remaining work, which keeps the delivery a draft pending
  acceptance. A user-required curvature goal whose features are all straight
  primitives raises an advisory "verify visually". The same check aggregates
  blocking failure classes across recorded gate reports: the same class
  recurring after several edits is named by count, so "a new IR version" is
  not read as recovery. Geometry validity, visual conformance and physical
  performance remain separately accepted.

Compact recipe features persist their `recipe_id` in the IR, so rotor and
polar-copy plans match their generated features even after renaming. Matching
uses this recorded identity rather than guessing from feature-name prefixes.
