"""Change-propagation orchestrator.

process(hire):  validate -> for each system in dependency order:
                project -> noop? -> assess live state -> (Claude if exception)
                -> idempotent write with retry -> ledger + audit.
Safe to call any number of times, from any trigger (Ashby webhook, nightly
poll, manual re-run): the ledger turns repeats into no-ops.
"""
from __future__ import annotations

import time
from datetime import date

from .agent import ExceptionHandler, OfflineHandler, redact
from .connectors import PermanentError, TransientError
from .models import (Action, Decision, DEPENDS_ON, Hire, Status, SYNC_ORDER, System,
                     hire_to_dict, stable_hash)
from .policies import assess
from .projections import project
from .store import Store
from .validators import errors, validate

TERMINAL_OK = (Status.APPLIED.value, Status.HELD.value, Status.NOTIFIED.value)
SLACK_CHANNEL = "#people-ops-onboarding"


class Orchestrator:
    def __init__(self, systems: dict, store: Store, slack, handler: ExceptionHandler | None = None,
                 today: date | None = None, sleep=time.sleep, max_attempts: int = 3):
        self.systems, self.store, self.slack = systems, store, slack
        self.handler = handler or OfflineHandler()
        self.today, self.sleep, self.max_attempts = today or date.today(), sleep, max_attempts

    # ------------------------------------------------------------------ main
    def process(self, hire: Hire) -> dict[str, str]:
        hid, results = hire.hire_id, {}
        self.store.save_hire(hid, hire_to_dict(hire))

        issues = validate(hire, self.today)
        errs = errors(issues)
        if errs:
            return self._quarantine(hire, errs)
        self.store.kv_set(f"quarantine:{hid}", "")  # clear so a future bad edit alerts again
        for i in issues:
            self.store.audit(hid, None, "VALIDATION_WARNING", {"code": i.code, "msg": i.message})

        ok_systems: set[System] = set()
        for system in SYNC_ORDER:
            if hire.source == system.value:  # source system: never write back
                ok_systems.add(system)
                continue
            status = self._sync_one(hire, system, ok_systems)
            results[system.value] = status
            if status == Status.APPLIED.value or status == "NOOP":
                ok_systems.add(system)
        return results

    def _sync_one(self, hire: Hire, system: System, ok_systems: set) -> str:
        hid, conn = hire.hire_id, self.systems[system.value]
        want = project(system, hire)
        dh = stable_hash(want)
        led = self.store.get_ledger(hid, system.value)

        if led and led["desired_hash"] == dh and led["status"] in TERMINAL_OK:
            self.store.audit(hid, system.value, "SKIPPED_NOOP", {"status": led["status"]})
            return "NOOP" if led["status"] == Status.APPLIED.value else led["status"]

        dep = DEPENDS_ON.get(system)
        if dep and dep not in ok_systems:
            self.store.put_ledger(hid, system.value, desired_hash=dh, status=Status.BLOCKED_UPSTREAM,
                                  last_error=f"waiting for {dep.value}")
            self.store.audit(hid, system.value, "BLOCKED_UPSTREAM", {"waiting_for": dep.value})
            return Status.BLOCKED_UPSTREAM.value

        try:
            live = self._with_retry(lambda: conn.get_state(hid), hid, system)
        except (TransientError, PermanentError) as e:
            return self._fail(hid, system, dh, e)

        a = assess(system, live, want, led["applied_payload"] if led else None)
        if a.ok:
            return self._execute(hire, system, want, dh, Action.APPLY, "policy:auto", live)

        decision = self._escalate(hire, system, a, live, want)
        return self._act_on_decision(hire, system, want, dh, a, decision, live)

    # ------------------------------------------------------------ escalation
    def _escalate(self, hire, system, a, live, want) -> Decision:
        changes = [{"field": k, "from": redact(live["payload"].get(k)), "to": redact(v)}
                   for k, v in want.items() if live["payload"].get(k) != v]
        ctx = {"hire_id": hire.hire_id, "system": system.value, "code": a.code, "reason": a.reason,
               "live_status": live["status"], "days_until_start": (hire.start_date - self.today).days,
               "changes": changes, "allowed_actions": [x.value for x in a.allowed_actions],
               "safe_default": a.safe_default.value}
        try:
            d = self.handler.decide(ctx)
        except Exception as e:  # handler must never break the pipeline
            d = OfflineHandler().decide(ctx)
            d.rationale += f" (handler error: {type(e).__name__})"
        if d.action not in a.allowed_actions:  # GUARD: model output is advisory
            self.store.audit(hire.hire_id, system.value, "DECISION_REJECTED",
                             {"proposed": d.action.value, "allowed": ctx["allowed_actions"]})
            d = Decision(a.safe_default, f"guard override of {d.action.value}: {d.rationale}",
                         d.slack_message, d.decided_by + "+guard")
        self.store.audit(hire.hire_id, system.value, "ESCALATION_DECIDED",
                         {"code": a.code, "action": d.action.value, "by": d.decided_by, "why": d.rationale})
        return d

    def _act_on_decision(self, hire, system, want, dh, a, d: Decision, live) -> str:
        hid = hire.hire_id
        if d.action in (Action.HOLD, Action.NOTIFY_ONLY):
            st = Status.HELD if d.action is Action.HOLD else Status.NOTIFIED
            self.store.put_ledger(hid, system.value, desired_hash=dh, status=st, decided_by=d.decided_by,
                                  last_error=a.code)
            self.slack.post(SLACK_CHANNEL, d.slack_message)
            return st.value
        status = self._execute(hire, system, want, dh, d.action, d.decided_by, live)
        if status == Status.APPLIED.value:
            self.slack.post(SLACK_CHANNEL, d.slack_message)
        return status

    def approve(self, hire_id: str, system: System, action: Action, approver: str) -> str:
        """Human resolves a HELD item (e.g. via the MCP tool or a Slack button)."""
        import json
        row = self.store.db.execute("SELECT canonical FROM hires WHERE hire_id=?", (hire_id,)).fetchone()
        hire = hire_from_dict(json.loads(row["canonical"]))
        want = project(system, hire)
        live = self.systems[system.value].get_state(hire_id)
        self.store.audit(hire_id, system.value, "HUMAN_APPROVED", {"action": action.value, "by": approver})
        return self._execute(hire, system, want, stable_hash(want), action, f"human:{approver}", live)

    # -------------------------------------------------------------- execution
    def _execute(self, hire, system, want, dh, action: Action, decided_by, live) -> str:
        hid, conn = hire.hire_id, self.systems[system.value]
        payload = dict(want)
        if action is Action.APPLY_KEEP_LOGIN:
            payload["login"] = live["payload"]["login"]
        idem = f"{hid}:{system.value}:{dh}:{action.value}"  # same intent => same key
        try:
            self._with_retry(lambda: conn.upsert(hid, payload, idem, force=action is Action.APPLY_FORCE), hid, system)
        except (TransientError, PermanentError) as e:
            return self._fail(hid, system, dh, e)
        self.store.put_ledger(hid, system.value, desired_hash=dh, status=Status.APPLIED,
                              applied_payload=payload, decided_by=decided_by)
        self.store.audit(hid, system.value, "APPLIED", {"action": action.value, "idem": idem, "by": decided_by})
        return Status.APPLIED.value

    def _with_retry(self, fn, hid, system):
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except TransientError as e:
                self.store.audit(hid, system.value, "RETRY", {"attempt": attempt, "error": str(e)})
                if attempt == self.max_attempts:
                    raise
                self.sleep(min(2 ** attempt, 30))  # exponential backoff
            # PermanentError propagates immediately

    def _fail(self, hid, system, dh, err) -> str:
        self.store.put_ledger(hid, system.value, desired_hash=dh, status=Status.FAILED, last_error=str(err))
        self.store.audit(hid, system.value, "FAILED", {"error": str(err)})
        self.slack.post(SLACK_CHANNEL, f"[SYNC_FAILED] hire {hid} / {system.value}: {err}. Will retry next run.")
        return Status.FAILED.value

    def _quarantine(self, hire: Hire, errs) -> dict[str, str]:
        hid = hire.hire_id
        sig = stable_hash([(e.code, e.field) for e in errs])
        for s in SYNC_ORDER:  # downstream keeps last good data
            led = self.store.get_ledger(hid, s.value)
            self.store.put_ledger(hid, s.value, desired_hash="", status=Status.QUARANTINED,
                                  last_error=",".join(e.code for e in errs),
                                  applied_payload=led["applied_payload"] if led else None)
        self.store.audit(hid, None, "QUARANTINED", {"errors": [e.__dict__ for e in errs]})
        if self.store.kv_get(f"quarantine:{hid}") != sig:  # don't re-alert on every poll
            self.store.kv_set(f"quarantine:{hid}", sig)
            lines = "; ".join(f"{e.field}: {e.message}" for e in errs)
            self.slack.post(SLACK_CHANNEL,
                            f"[BAD_DATA] hire {hid} held before fan-out, nothing written downstream. {lines}")
        return {s.value: Status.QUARANTINED.value for s in SYNC_ORDER}

    # ---------------------------------------------------- off-cycle discovery
    def discover_off_cycle(self) -> list[str]:
        """Hires that exist in Workday but were never seen via Ashby."""
        found = []
        known = self.store.known_hire_ids()
        for hid, rec in self.systems["workday"].records.items():
            if hid not in known:
                found.append(hid)
                self.store.audit(hid, "workday", "OFF_CYCLE_DISCOVERED", {})
                self.process(hire_from_workday(hid, rec["payload"]))
        return found


def hire_from_dict(d: dict) -> Hire:
    from .models import Address
    d = dict(d)
    d["start_date"] = date.fromisoformat(d["start_date"])
    d["address"] = Address(**d["address"])
    return Hire(**d)


def hire_from_workday(hid: str, p: dict) -> Hire:
    from .models import Address
    return Hire(hire_id=hid, first_name=p["legal_first_name"], last_name=p["legal_last_name"],
                personal_email=p.get("personal_email", ""), start_date=date.fromisoformat(p["start_date"]),
                address=Address(**p["home_address"]), job_title=p["job_title"],
                manager_email=p["manager_email"], source="workday", preferred_name=p.get("preferred_name"))
