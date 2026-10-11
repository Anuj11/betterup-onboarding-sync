# Build note (deliverable 3)

## Design decisions

**Desired-state projection, not event forwarding.** `projections.project(system, hire)` is a pure function. Each sync converges a system to its projection. Its hash is the idempotency key (`hire:system:hash:action`), so replays, webhook retries and nightly polls are no-ops (test: `test_replay_is_noop`).

**Data model.** `Hire` (canonical), `ledger(hire_id, system, desired_hash, applied_payload, status)`, hash-chained `audit`. Statuses: APPLIED, HELD, NOTIFIED, FAILED, BLOCKED_UPSTREAM, QUARANTINED. Ledger gives resumability: after a crash the next run starts at the first system not APPLIED for the current hash.

**Failure handling.**
- Transient errors: exponential backoff, same idempotency key. The mock deliberately simulates *timeout after apply*, the case where naive retry duplicates writes.
- Permanent errors: FAILED + Slack; dependents (Okta, equipment) are BLOCKED_UPSTREAM until Workday succeeds; the tracker is a read model and never blocked.
- Bad data: validation errors quarantine the hire before any fan-out; downstream stays at last-good; alerts are de-duplicated.
- Out-of-band edits: if a live system no longer matches our last write, the change is escalated rather than overwritten.

**Claude as a component, not the solution.** Deterministic code detects the exception and enumerates the legal actions (`policies.py`). Claude is called with a forced tool call (`resolve_exception`, enum-constrained), sees redacted context (no street, postal code, emails), and returns one action + a Slack note. The orchestrator re-checks the action against the allowed list (`test_guard_rejects_illegal_model_action`). API failure, bad schema or no key falls back to conservative rules. Claude never writes to a system.

**Off-cycle hires.** Imported from Workday, same pipeline, no write-back to the source system.

**Bonus included:** hash-chained audit trail (tamper test), tool schema for the Claude call. Not included: OAuth/SSO, MCP server.

## What I'd productionize
1. Real connectors (Ashby webhooks + poll fallback, Workday RaaS/REST, Okta API, Lumos for access requests, ExpoIT, Sheets API) behind the same `upsert/get_state` interface; secrets in the existing vault, OAuth service accounts per system.
2. Postgres instead of SQLite, a per-hire lock/queue so concurrent events serialize, and a dead-letter view.
3. Persist decisions as per-hire overrides (today, after `APPLY_KEEP_LOGIN` a *later* unrelated Okta change re-asks, because desired login still differs).
4. Exception monitoring on the same ledger: a scheduled job flags hires whose hardware form / background check / Okta provisioning gate is due within N days and not APPLIED, and nudges the owner in Slack.
5. Human approval path (`Orchestrator.approve`) behind a Slack button or MCP tool; the audit already records `HUMAN_APPROVED`.
6. Real address verification via Google Maps Address Validation (existing Google estate) instead of the small city/state table.
7. Metrics: sync latency, % hires auto-resolved vs escalated, quarantine rate, Claude fallback rate.

## AI tools used, and one thing the AI got wrong
Claude (chat) drafted the code and tests; I reviewed and ran everything.

**What it got wrong:** the first demo narrative claimed a Workday failure on a *job-title* change still "updates the tracker". Running the demo showed the tracker stayed `NOOP`, because the tracker projection doesn't include job title, so nothing had changed for it. I caught it by reading the printed per-system statuses instead of trusting the caption; I switched the scenario to a start-date change, which does touch the tracker. A second one: my first audit-tamper test "tampered" a row by rewriting a field to the value it already had, so it passed for the wrong reason; the failing run exposed it.

> If you have your own example from building this, use it instead. You need to be able to tell it in your own words.

## Known limits (honest)
- Mocks, not real APIs; field names and statuses (Okta STAGED/PROVISIONED/ACTIVE, shipment states) are assumed.
- Single-process, no concurrency control.
- Policies encode my assumptions about BetterUp rules (7-day gate, Monday starts); they're data to confirm with People Ops.
