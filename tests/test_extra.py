"""Coverage-completing tests: validators, policies, approval flow, handlers, demo smoke test."""
from dataclasses import replace
from datetime import date

import pytest

from bu_sync import agent, demo
from bu_sync.agent import OfflineHandler, default_handler, FallbackHandler
from bu_sync.connectors import MockSlack, PermanentError, build_systems
from bu_sync.demo import TODAY, sample_hires
from bu_sync.models import Action, Address, Status, System, prev_business_day
from bu_sync.orchestrator import Orchestrator
from bu_sync.store import Store
from bu_sync.validators import errors, validate


@pytest.fixture
def env():
    systems, slack, store = build_systems(), MockSlack(), Store()
    return Orchestrator(systems, store, slack, OfflineHandler(), today=TODAY, sleep=lambda s: None), systems, slack, store


def codes(h, sev=None):
    return {i.code for i in validate(h, TODAY) if sev is None or i.severity == sev}


def test_validator_warnings_and_remaining_errors():
    a, _ = sample_hires()
    assert "NAME_EMPTY" in codes(replace(a, first_name=" "), "error")
    assert "NAME_CASE" in codes(replace(a, last_name="SMITH"), "warning")
    assert "NAME_DUPLICATE" in codes(replace(a, first_name="Lee", last_name="Lee"), "warning")
    assert "EMAIL_FORMAT" in codes(replace(a, personal_email="nope"), "error")
    assert "START_NOT_MONDAY" in codes(replace(a, start_date=date(2026, 10, 27)), "warning")
    assert "ADDRESS_INCOMPLETE" in codes(replace(a, address=Address("", "Austin", "TX", "78701")), "error")
    assert "STATE_INVALID" in codes(replace(a, address=Address("x", "Austin", "ZZ", "78701")), "error")
    assert not codes(replace(a, address=Address("x", "Mumbai", "MH", "400001", "IN")), "error")   # non-US path
    assert len(errors(validate(replace(a, first_name="J0n"), TODAY))) == 1


def test_warnings_do_not_block_sync(env):
    orch, _, _, store = env
    a, _ = sample_hires()
    res = orch.process(replace(a, start_date=date(2026, 10, 27)))        # not a Monday: warning only
    assert set(res.values()) == {"APPLIED"}
    assert any(r["event"] == "VALIDATION_WARNING" for r in store.audit_rows())


def test_out_of_band_edit_is_held_then_human_can_overwrite(env):
    orch, systems, slack, store = env
    a, _ = sample_hires()
    orch.process(a)
    systems["tracker"].records[a.hire_id]["payload"]["cohort"] = "hand-edited"     # someone edits the sheet
    res = orch.process(replace(a, start_date=date(2026, 11, 9)))
    assert res["tracker"] == Status.HELD.value
    assert any("OUT_OF_BAND_EDIT" in m for _, m in slack.messages)
    assert orch.approve(a.hire_id, System.TRACKER, Action.APPLY, "alice") == Status.APPLIED.value
    assert systems["tracker"].records[a.hire_id]["payload"]["cohort"] == "cohort-2026-11-09"
    assert any(r["event"] == "HUMAN_APPROVED" for r in store.audit_rows(a.hire_id))


def test_workday_already_hired_start_move_notifies_and_blocks_dependents(env):
    orch, systems, slack, _ = env
    a, _ = sample_hires()
    orch.process(a)
    systems["workday"].records[a.hire_id]["status"] = "HIRED"
    res = orch.process(replace(a, start_date=date(2026, 11, 9)))
    assert res["workday"] == Status.NOTIFIED.value
    assert res["okta"] == res["equipment"] == Status.BLOCKED_UPSTREAM.value
    assert res["tracker"] == Status.APPLIED.value


def test_held_item_is_not_re_alerted_on_next_poll(env):
    orch, systems, slack, _ = env
    a, _ = sample_hires()
    orch.process(a)
    systems["equipment"].records[a.hire_id]["status"] = "SHIPPED"
    moved = replace(a, address=Address("9 Mission St", "San Francisco", "CA", "94103"))
    orch.process(moved)
    n = len(slack.messages)
    orch.process(moved)
    assert len(slack.messages) == n


def test_offline_handler_rules():
    h = OfflineHandler()
    base = {"hire_id": "H", "system": "x", "live_status": "PROVISIONED", "days_until_start": 20}
    ctx = lambda **k: {**base, **k}
    assert h.decide(ctx(code="LABEL_ALREADY_CREATED", allowed_actions=["APPLY_FORCE", "HOLD"], safe_default="HOLD")).action is Action.APPLY_FORCE
    assert h.decide(ctx(code="LABEL_ALREADY_CREATED", days_until_start=3, allowed_actions=["APPLY_FORCE", "HOLD"], safe_default="HOLD")).action is Action.HOLD
    assert h.decide(ctx(code="LOGIN_CHANGE_AFTER_PROVISION", live_status="ACTIVE",
                        allowed_actions=["APPLY_KEEP_LOGIN", "APPLY", "HOLD"], safe_default="APPLY_KEEP_LOGIN")).action is Action.APPLY_KEEP_LOGIN
    assert h.decide(ctx(code="OUT_OF_BAND_EDIT", allowed_actions=["APPLY", "HOLD"], safe_default="HOLD")).action is Action.HOLD
    # rule picks an action that isn't legal here -> falls back to safe default
    assert h.decide(ctx(code="LABEL_ALREADY_CREATED", allowed_actions=["HOLD"], safe_default="HOLD")).action is Action.HOLD


def test_default_handler_selection(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(default_handler(), OfflineHandler)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(default_handler(), FallbackHandler)


def test_fallback_handler_annotates_when_claude_unavailable():
    class Down:
        def decide(self, ctx): raise ConnectionError("no network")
    ctx = {"hire_id": "H", "system": "s", "code": "ALREADY_SHIPPED", "days_until_start": 5, "live_status": "SHIPPED",
           "allowed_actions": ["NOTIFY_ONLY", "HOLD"], "safe_default": "HOLD"}
    d = FallbackHandler(Down()).decide(ctx)
    assert "Claude unavailable: ConnectionError" in d.rationale and d.decided_by == "offline-rules"


def test_equipment_mock_rejects_writes_after_shipment():
    eq = build_systems()["equipment"]
    eq.upsert("H", {"a": 1}, "k1")
    eq.records["H"]["status"] = "SHIPPED"
    with pytest.raises(PermanentError):
        eq.upsert("H", {"a": 2}, "k2")


def test_prev_business_day_and_store_filters(env):
    assert prev_business_day(date(2026, 10, 31)) == date(2026, 10, 30)       # Saturday -> Friday
    orch, _, _, store = env
    a, b = sample_hires()
    orch.process(a); orch.process(b)
    assert store.audit_rows(a.hire_id) and len(store.audit_rows()) > len(store.audit_rows(a.hire_id))
    assert store.known_hire_ids() == {a.hire_id, b.hire_id}


def test_demo_runs_end_to_end(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    demo.main(live=True)                                  # --live without a key must degrade, not crash
    out = capsys.readouterr().out
    assert "using offline rules" in out
    assert "chain valid: True" in out and "after tampering row 3 -> chain valid: False" in out
    assert "new writes: {'workday': 0, 'okta': 0, 'equipment': 0, 'tracker': 0}" in out
