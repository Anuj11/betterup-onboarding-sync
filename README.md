# BetterUp Onboarding Change-Propagation Prototype

A Python prototype for the **AI Automation Engineer** take-home. When a hire changes in Ashby (name, start date, address, title) or an off-cycle hire appears in Workday, the change is validated and propagated to **Workday, Okta/Lumos, equipment and the cohort tracker**. Claude is used only as the exception handler, never as the pipeline.

Everything runs on **mocked systems**. No credentials, no network and no real data are needed.

## Quick start

Requires Python 3.10+.

```bash
pip install -r requirements.txt     # pytest always; anthropic only needed for --live
python -m pytest -q                 # 15 tests
python -m bu_sync.demo              # 9-scenario walkthrough (offline rules stand in for Claude)
```

Optional, to use real Claude decisions:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CLAUDE_MODEL=claude-sonnet-5-5     # optional override
python -m bu_sync.demo --live
```

Without a key, `--live` prints a notice and falls back to offline rules. If the Claude call fails mid-run (network, 429, bad output), the same rules take over for that decision.

## How it works

```
Ashby change / off-cycle hire in Workday
        |
   validate (gate) --errors--> QUARANTINE + Slack (nothing written downstream)
        |
   for each system, in order: Workday -> Okta -> Equipment -> Tracker
        |   project(system, hire)      pure desired payload; its hash = idempotency key
        |   ledger says already applied?  -> no-op
        |   upstream (Workday) applied?   -> else BLOCKED_UPSTREAM
        |   assess live state (policies)  -> ok: write
        |                                 -> exception: Claude picks ONE legal action
        |   write with retry/backoff      (same idempotency key on every retry)
        v
   ledger row + hash-chained audit entry
```

- **Idempotent:** replaying an event (webhook retry, nightly poll) causes zero writes.
- **Resumable:** the ledger records the last state per hire and system, so a crash resumes where it stopped.
- **Safe at the point of no return:** `policies.py` lists the legal actions, for example Okta already provisioned, label already cut, or package shipped. Claude chooses among them through a forced tool call, and the orchestrator rejects any action outside the list.
- **Data minimisation:** Claude never sees street, postal code or email addresses.

## Demo scenarios (`python -m bu_sync.demo`)

1. Initial sync of two Ashby hires, plus an off-cycle hire discovered in Workday
2. Replay of the same events: zero new writes
3. Start-date move: fans out to every system and recomputes the gate deadlines
4. Bad Ashby edit (digit in name, short ZIP, weekend start): quarantined, one de-duplicated alert
5. Okta 503, then a timeout after apply: exactly one real write
6. Name typo fixed after Okta provisioning: the handler decides, and the old login is kept as an alias
7. Address change after the label is cut, then after shipping: void and recreate, then notify a human
8. Workday hard failure: dependents blocked, tracker still updates, next run self-heals
9. Audit trail: valid chain, then a tamper check

## Project layout

| Path | Role |
|---|---|
| `bu_sync/models.py` | Canonical `Hire`, statuses, actions, dependency order |
| `bu_sync/projections.py` | Pure hire to per-system payload; Okta login; gate deadlines |
| `bu_sync/validators.py` | Pre-flight validation gate |
| `bu_sync/policies.py` | Deterministic rules: is a change safe, and which actions are legal |
| `bu_sync/agent.py` | Claude tool-call handler, redaction, offline fallback |
| `bu_sync/orchestrator.py` | Ordering, retries, ledger, escalation, human approval, off-cycle discovery |
| `bu_sync/connectors.py` | Mock Workday/Okta/equipment/tracker/Slack with fault injection |
| `bu_sync/store.py` | SQLite ledger and hash-chained audit trail |
| `bu_sync/demo.py` | Runnable walkthrough |
| `tests/test_core.py` | 15 tests covering idempotency, failure modes, guard, fallback, PII redaction, audit |
| `docs/SYSTEMS_MAP.md` | Deliverable 1: systems map and why this slice |
| `docs/BUILD_NOTE.md` | Deliverable 3: design decisions, productionizing, AI usage |

## Using it from code

```python
from bu_sync.demo import build, sample_hires
orch, systems, slack, store = build()
a, _ = sample_hires()
print(orch.process(a))                       # {'workday': 'APPLIED', ...}
print(orch.process(a))                       # all 'NOOP'
systems["okta"].inject("transient")          # fault injection: 'transient' | 'timeout_after_apply' | 'permanent'
orch.approve("H-1001", System.OKTA, Action.APPLY, "alice")   # resolve a HELD item
```

## Assumptions and limits

- Connectors are mocks. Field names and statuses (Okta STAGED/PROVISIONED/ACTIVE, shipment states) are assumed.
- The 7-day gate lead time and Monday starts are assumptions to confirm with People Ops.
- Single process, SQLite, no concurrency control.
- After `APPLY_KEEP_LOGIN`, a later unrelated Okta change asks again, because per-hire decision overrides are not persisted yet.
- No OAuth/SSO and no MCP server. The Claude tool schema is in `agent.py`.

See `docs/BUILD_NOTE.md` for what I would productionize.
