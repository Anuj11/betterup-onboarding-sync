# BetterUp Onboarding Change-Propagation Prototype

A Python prototype for the **AI Automation Engineer** take-home. When a hire changes in Ashby (name, start date, address, title) or an off-cycle hire appears in Workday, the change is validated and propagated to **Workday, Okta/Lumos, equipment and the cohort tracker**. Claude is used only as the exception handler, never as the pipeline.

Everything runs on **mocked systems**. No credentials, no network and no real data are needed.

## Quick start

Requires Python 3.10+.

```bash
pip install -r requirements.txt     # pytest always; anthropic only needed for --live
python -m pytest                    # 26 tests + coverage report (fails under 90%)
python -m bu_sync.demo              # plain-English walkthrough, 10 self-checking scenarios (offline rules stand in for Claude)
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

## Tests and coverage

```bash
python -m pytest                       # runs with coverage by default (pytest.ini)
python -m pytest --cov-report=html     # then open htmlcov/index.html
```

Branch coverage is on, and the run fails if total coverage drops below 90%. Current result: **26 tests, 98% coverage**. The few uncovered lines are the error path when reading a system's live state fails (`orchestrator.py`), the unknown-system `ValueError` in `projections.py`, and a couple of branch exits. The real Claude API call is exercised only through a fake client, so the network path itself is untested.

## Reading the demo output

`python -m bu_sync.demo` is written to be read by someone who has never seen the code. A full captured run is in [`docs/SAMPLE_OUTPUT.txt`](docs/SAMPLE_OUTPUT.txt). Each scenario has the same shape:

```
SCENARIO 5: Okta has a hiccup in the middle of an update
   Situation: <what happens in the real world>
   Expected : <what the system should do>
   Result:    <per-system outcome in plain words: updated / already up to date / held / waiting ...>
   [PASS] <a check the demo ran against the mock systems, not just a printed claim>
```

It ends with the Slack alerts People Ops would receive and a one-line summary. The process exits with a non-zero code if any check fails, so it also works as a smoke test in CI.

| # | Scenario | What it proves |
|---|---|---|
| 1 | First sync, incl. a hire created outside Ashby | Off-cycle hires are discovered and set up without overwriting their source system |
| 2 | Same update arrives twice | Idempotency: zero duplicate writes |
| 3 | Start date changes | Change fans out to every affected system; deadlines recalculated |
| 4 | Bad data entered in Ashby | Validation gate: nothing cascades, one alert |
| 5 | Okta error, then timeout after applying | Safe retries: exactly one real write |
| 6 | Name typo fixed after Okta account exists | Risky change goes to the exception handler; old login kept as alias |
| 7 | Address change after label created, then after shipping | Void and recreate when there is time; never touch a shipped package |
| 8 | Workday rejects an update | Dependents wait, tracker still updates, next run self-heals |
| 9 | Audit trail | Tamper-evident log; edits to old entries are detected |

(The label-created and already-shipped cases are two separate scenarios in the output, so it shows 10.)

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
| `bu_sync/demo.py` | Plain-English, self-checking walkthrough |
| `docs/SAMPLE_OUTPUT.txt` | Captured demo output |
| `tests/test_core.py` | Core behaviour: idempotency, failure modes, guard, fallback, PII redaction, audit |
| `tests/test_extra.py` | Validators, out-of-band edits, human approval, handler rules, demo smoke test |
| `.coveragerc`, `pytest.ini` | Coverage config (branch coverage, 90% minimum) |
| `.github/workflows/ci.yml` | CI: tests with coverage, then the demo |
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
