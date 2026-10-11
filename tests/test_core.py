import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from bu_sync.agent import ClaudeHandler, FallbackHandler, OfflineHandler, redact
from bu_sync.connectors import MockSlack, build_systems
from bu_sync.demo import TODAY, sample_hires
from bu_sync.models import Action, Address, Decision, Status, System
from bu_sync.orchestrator import Orchestrator
from bu_sync.projections import okta_login
from bu_sync.store import Store
from bu_sync.validators import validate, errors


@pytest.fixture
def env():
    systems, slack, store = build_systems(), MockSlack(), Store()
    orch = Orchestrator(systems, store, slack, OfflineHandler(), today=TODAY, sleep=lambda s: None)
    return orch, systems, slack, store


def total_writes(systems):
    return sum(len(s.write_log) for s in systems.values())


def test_replay_is_noop(env):
    orch, systems, _, _ = env
    a, _ = sample_hires()
    orch.process(a)
    n = total_writes(systems)
    res = orch.process(a)
    assert total_writes(systems) == n
    assert set(res.values()) == {"NOOP"}


def test_change_propagates_only_to_affected_systems(env):
    orch, systems, _, _ = env
    a, _ = sample_hires()
    orch.process(a)
    before = {k: len(v.write_log) for k, v in systems.items()}
    orch.process(replace(a, job_title="Staff SE"))      # title: workday + okta only
    delta = {k: len(v.write_log) - before[k] for k, v in systems.items()}
    assert delta == {"workday": 1, "okta": 1, "equipment": 0, "tracker": 0}


def test_bad_data_quarantined_and_downstream_untouched(env):
    orch, systems, slack, _ = env
    a, _ = sample_hires()
    orch.process(a)
    n = total_writes(systems)
    bad = replace(a, last_name="Sm1th", address=Address("1 Market St", "San Francisco", "TX", "94105"))
    res = orch.process(bad)
    assert set(res.values()) == {"QUARANTINED"} and total_writes(systems) == n
    orch.process(bad)
    assert sum("BAD_DATA" in m for _, m in slack.messages) == 1       # alert de-duped


def test_validator_rules():
    a, _ = sample_hires()
    codes = lambda h: {i.code for i in errors(validate(h, TODAY))}
    assert codes(a) == set()
    assert "START_WEEKEND" in codes(replace(a, start_date=date(2026, 10, 31)))
    assert "START_IN_PAST" in codes(replace(a, start_date=date(2026, 10, 5)))
    assert "POSTAL_FORMAT" in codes(replace(a, address=Address("x", "Austin", "TX", "123")))
    assert "CITY_STATE_MISMATCH" in codes(replace(a, address=Address("x", "Austin", "CA", "78701")))


def test_timeout_after_apply_does_not_duplicate(env):
    orch, systems, _, _ = env
    a, _ = sample_hires()
    orch.process(a)
    systems["okta"].inject("transient", "timeout_after_apply")
    n = len(systems["okta"].write_log)
    res = orch.process(replace(a, start_date=date(2026, 11, 9)))
    assert res["okta"] == "APPLIED" and len(systems["okta"].write_log) == n + 1


def test_retries_exhausted_then_recovers(env):
    orch, systems, slack, _ = env
    a, _ = sample_hires()
    systems["workday"].inject("transient", "transient", "transient")
    res = orch.process(a)
    assert res["workday"] == "FAILED" and res["okta"] == "BLOCKED_UPSTREAM" and res["equipment"] == "BLOCKED_UPSTREAM"
    assert res["tracker"] == "APPLIED"                                   # read model never blocked
    res = orch.process(a)
    assert set(res.values()) == {"APPLIED"} or set(res.values()) <= {"APPLIED", "NOOP"}
    assert len(systems["workday"].write_log) == 1


def test_okta_rename_after_provision_uses_handler_and_keeps_alias(env):
    orch, systems, _, _ = env
    a, _ = sample_hires()
    orch.process(a)
    systems["okta"].records[a.hire_id]["status"] = "PROVISIONED"
    orch.process(replace(a, first_name="John", last_name="Smith"))
    rec = systems["okta"].records[a.hire_id]
    assert rec["payload"]["login"] == "john.smith@betterup.com" and "jon.smyth@betterup.com" in rec["aliases"]


def test_shipped_package_is_never_modified(env):
    orch, systems, slack, _ = env
    a, _ = sample_hires()
    orch.process(a)
    systems["equipment"].records[a.hire_id]["status"] = "SHIPPED"
    old = dict(systems["equipment"].records[a.hire_id]["payload"])
    res = orch.process(replace(a, address=Address("9 Mission St", "San Francisco", "CA", "94103")))
    assert res["equipment"] == Status.NOTIFIED.value
    assert systems["equipment"].records[a.hire_id]["payload"] == old


def test_guard_rejects_illegal_model_action(env):
    orch, systems, _, store = env

    class Rogue:                      # a model that tries to force-apply onto a shipped package
        def decide(self, ctx):
            return Decision(Action.APPLY_FORCE, "just do it", "go", "claude:test")
    orch.handler = Rogue()
    a, _ = sample_hires()
    orch.process(a)
    systems["equipment"].records[a.hire_id]["status"] = "SHIPPED"
    res = orch.process(replace(a, address=Address("9 Mission St", "San Francisco", "CA", "94103")))
    assert res["equipment"] == Status.HELD.value                         # safe_default, not APPLY_FORCE
    assert any(r["event"] == "DECISION_REJECTED" for r in store.audit_rows())


def test_handler_crash_falls_back_to_rules(env):
    orch, systems, _, _ = env

    class Boom:
        def decide(self, ctx): raise RuntimeError("api down")
    orch.handler = Boom()
    a, _ = sample_hires()
    orch.process(a)
    systems["okta"].records[a.hire_id]["status"] = "ACTIVE"
    res = orch.process(replace(a, first_name="John", last_name="Smith"))
    assert res["okta"] == "APPLIED"                                      # keep-login via offline rules
    assert systems["okta"].records[a.hire_id]["payload"]["login"] == "jon.smyth@betterup.com"


def test_claude_handler_parses_forced_tool_call_and_redacts_pii():
    seen = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                blk = SimpleNamespace(type="tool_use", input={"action": "HOLD", "rationale": "r", "slack_message": "m"})
                return SimpleNamespace(content=[blk])
    h = ClaudeHandler(client=FakeClient)
    ctx = {"hire_id": "H", "system": "equipment", "code": "X",
           "changes": [{"field": "ship_to", "to": redact({"line1": "1 Secret St", "city": "SF", "postal_code": "94105"})}]}
    d = h.decide(ctx)
    assert d.action is Action.HOLD and d.decided_by.startswith("claude:")
    assert seen["tool_choice"] == {"type": "tool", "name": "resolve_exception"}
    assert "Secret" not in json.dumps(seen["messages"]) and "94105" not in json.dumps(seen["messages"])


def test_fallback_handler_on_bad_schema():
    class Bad:
        def decide(self, ctx): raise KeyError("action")
    ctx = {"code": "ALREADY_SHIPPED", "days_until_start": 10, "live_status": "SHIPPED", "hire_id": "H",
           "system": "equipment", "allowed_actions": ["NOTIFY_ONLY", "HOLD"], "safe_default": "HOLD"}
    assert FallbackHandler(Bad()).decide(ctx).action is Action.NOTIFY_ONLY


def test_off_cycle_hire_imported_and_not_written_back(env):
    orch, systems, _, _ = env
    systems["workday"].records["H-9"] = {"status": "PRE_HIRE", "payload": {
        "legal_first_name": "Priya", "legal_last_name": "Nair", "preferred_name": None, "start_date": "2026-11-09",
        "job_title": "Analyst", "manager_email": "m@betterup.com", "personal_email": "p@gmail.com",
        "home_address": {"line1": "1 Pine", "city": "Seattle", "state": "WA", "postal_code": "98101", "country": "US"}}}
    assert orch.discover_off_cycle() == ["H-9"]
    assert systems["okta"].get_state("H-9")["exists"] and not systems["workday"].write_log
    assert orch.discover_off_cycle() == []


def test_audit_chain_detects_tampering(env):
    orch, _, _, store = env
    a, _ = sample_hires()
    orch.process(a)
    assert store.verify_audit()
    store.db.execute("UPDATE audit SET detail='{\"tampered\": true}' WHERE id=1")
    assert not store.verify_audit()


def test_okta_login_normalises_accents_and_apostrophes():
    a, _ = sample_hires()
    assert okta_login(replace(a, first_name="José", last_name="O'Brien")) == "jose.obrien@betterup.com"
