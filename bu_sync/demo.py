"""Runnable end-to-end demo on mocked systems:  python -m bu_sync.demo [--live]"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from dataclasses import replace
from datetime import date

from .agent import ClaudeHandler, FallbackHandler, OfflineHandler
from .connectors import MockSlack, build_systems
from .models import Address, Hire, System
from .orchestrator import Orchestrator
from .projections import project
from .validators import errors, validate
from .store import Store

TODAY = date(2026, 10, 12)


def sample_hires():
    a = Hire("H-1001", "Jon", "Smyth", "jon.s@gmail.com", date(2026, 10, 26),
             Address("1 Market St", "San Francisco", "CA", "94105"), "Solutions Engineer", "mgr1@betterup.com")
    b = Hire("H-1002", "Maria", "Garcia", "maria.g@gmail.com", date(2026, 11, 2),
             Address("500 Congress Ave", "Austin", "TX", "78701"), "Coach Success Partner", "mgr2@betterup.com")
    return a, b


def build(live: bool = False):
    systems, slack, store = build_systems(), MockSlack(), Store()
    if live and not os.getenv("ANTHROPIC_API_KEY"):
        print("NOTE: --live requested but ANTHROPIC_API_KEY is not set; using offline rules.")
        live = False
    handler = FallbackHandler(ClaudeHandler()) if live else OfflineHandler()
    orch = Orchestrator(systems, store, slack, handler, today=TODAY, sleep=lambda s: None)
    return orch, systems, slack, store


def writes(systems):
    return {k: len(v.write_log) for k, v in systems.items()}


def show(title, results=None):
    print(f"\n=== {title}")
    if results:
        print("   ", results)


STATUS_WORDS = {
    "APPLIED": "updated", "NOOP": "already up to date", "QUARANTINED": "held - bad data, nothing changed",
    "BLOCKED_UPSTREAM": "waiting on Workday", "FAILED": "failed, will retry on next run",
    "HELD": "waiting for a human", "NOTIFIED": "human notified, system left untouched"}
LABEL = {"workday": "Workday", "okta": "Okta", "equipment": "Equipment", "tracker": "Tracker"}


class Report:
    """Prints each scenario as: situation -> what the system did -> self-checks."""

    def __init__(self):
        self.n, self.checks, self.failed_scenarios, self._cur_ok = 0, 0, 0, True

    def _wrap(self, text, indent="   ", hanging=None):
        print(textwrap.fill(text, width=92, initial_indent=indent,
                            subsequent_indent=hanging if hanging is not None else indent))

    def scenario(self, title, situation, expect):
        self._close()
        self.n += 1
        print(f"\n{'-' * 92}\nSCENARIO {self.n}: {title}\n{'-' * 92}")
        self._wrap(f"Situation: {situation}")
        self._wrap(f"Expected : {expect}")
        self._cur_ok = True

    def result(self, res: dict, who="Result"):
        print(f"   {who}:")
        for k, v in res.items():
            print(f"      {LABEL[k]:<10} {STATUS_WORDS.get(v, v)}")

    def note(self, text, indent="   "):
        self._wrap(text, indent)

    def check(self, ok: bool, text: str):
        self.checks += 1
        self._cur_ok &= bool(ok)
        print(f"   [{'PASS' if ok else 'FAIL'}] {text}")

    def _close(self):
        if self.n and not self._cur_ok:
            self.failed_scenarios += 1

    def summary(self) -> bool:
        self._close()
        good = self.n - self.failed_scenarios
        print(f"\n{'=' * 92}\nSUMMARY: {good} of {self.n} scenarios behaved as expected ({self.checks} checks)."
              + ("  All checks passed." if not self.failed_scenarios else "  SOME CHECKS FAILED."))
        return not self.failed_scenarios


def decision(store, hid, system):
    rows = [r for r in store.audit_rows(hid) if r["event"] == "ESCALATION_DECIDED" and r["system"] == system]
    return json.loads(rows[-1]["detail"]) if rows else {}


def main(live: bool = False) -> bool:
    orch, sys_, slack, store = build(live)
    r = Report()
    a, b = sample_hires()
    handler = "Claude (with rule-based fallback)" if isinstance(orch.handler, FallbackHandler) else \
        "simple built-in rules standing in for Claude (run with --live and an API key for real Claude decisions)"
    print("BetterUp onboarding sync - demo on MOCKED systems (no real data, no credentials)")
    print("=" * 92)
    r._wrap("What this shows: when a new hire's details change in Ashby (or a hire is created outside Ashby), "
            "the change is checked first, then applied safely to every system that needs it.", "")
    print("\nSystems:  Workday = HR record (source for the others)   Okta = login and email")
    print("          Equipment = laptop shipping                    Tracker = cohort spreadsheet")
    print("Words:    updated | already up to date | held (bad data) | waiting on Workday | failed, will retry |")
    print("          waiting for a human | human notified (system left untouched)")
    r._wrap(f"Who decides on tricky cases: {handler}.", "")

    # 1 -----------------------------------------------------------------------------------
    r.scenario("First sync of new hires, including one created outside Ashby",
               "Two hires (Jon Smyth, Maria Garcia) are in Ashby. A third (Priya Nair) was entered directly in Workday.",
               "Ashby hires reach all four systems. The Workday-only hire is discovered and her other systems are set "
               "up,"
               "without writing back to Workday.")
    ra, rb = orch.process(a), orch.process(b)
    r.result(ra, "Jon Smyth (H-1001)")
    r.result(rb, "Maria Garcia (H-1002)")
    sys_["workday"].records["H-9001"] = {"status": "PRE_HIRE", "payload": {
        "legal_first_name": "Priya", "legal_last_name": "Nair", "preferred_name": None, "start_date": "2026-11-09",
        "job_title": "Data Analyst", "manager_email": "mgr3@betterup.com", "personal_email": "priya.n@gmail.com",
        "home_address": vars(Address("12 Pine St", "Seattle", "WA", "98101"))}}
    found = orch.discover_off_cycle()
    r.check(set(ra.values()) == {"APPLIED"} and set(rb.values()) == {"APPLIED"}, "Both Ashby hires were created in "
                                                                                 "all four systems")
    r.check(found == ["H-9001"] and sys_["okta"].get_state("H-9001")["exists"], "Priya (Workday-only) was discovered "
                                                                                "and given an Okta account")
    r.check(not sys_["workday"].write_log or all(h != "H-9001" for h, _ in sys_["workday"].write_log), "Workday was "
                                                                                                       "not "
                                                                                                       "overwritten "
                                                                                                       "for Priya (it "
                                                                                                       "is her source)")

    # 2 -----------------------------------------------------------------------------------
    r.scenario("The same update arrives twice",
               "Ashby re-sends the same event (webhook retry) and the nightly poll sees it again.",
               "Nothing is written a second time.")
    before = writes(sys_)
    res = orch.process(a);
    orch.process(b)
    delta = {k: writes(sys_)[k] - before[k] for k in before}
    r.result(res)
    r.check(sum(delta.values()) == 0, f"Zero new writes across all systems (new writes: {sum(delta.values())})")

    # 3 -----------------------------------------------------------------------------------
    r.scenario("Start date changes after the offer",
               "Jon's start date moves from Mon 26 Oct to Mon 2 Nov.",
               "Every system that depends on the date is updated, including the hardware and background-check "
               "deadlines.")
    t0 = sys_["tracker"].get_state("H-1001")["payload"]
    a = replace(a, start_date=date(2026, 11, 2))
    res = orch.process(a)
    t1 = sys_["tracker"].get_state("H-1001")["payload"]
    r.result(res)
    r.note(
        f"Tracker before -> after:  cohort {t0['cohort']} -> {t1['cohort']};"
        f"hardware form due {t0['hardware_form_due']} -> {t1['hardware_form_due']}")
    r.check(set(res.values()) == {"APPLIED"}, "All four systems received the new date")
    r.check(t1["hardware_form_due"] == "2026-10-26" and sys_["equipment"].get_state("H-1001")["payload"][
        "ship_by"] == "2026-10-26",
            "Deadlines were recalculated to 7 days before the new start date (26 Oct), with no manual work")

    # 4 -----------------------------------------------------------------------------------
    r.scenario("Someone enters bad data in Ashby",
               "Jon's record is edited to: first name 'J0n' (a zero), ZIP '9410' (4 digits), start date on a Saturday.",
               "The edit is stopped BEFORE it reaches any system. The team gets one alert, not one per retry.")
    before = writes(sys_)
    bad = replace(a, first_name="J0n", start_date=date(2026, 10, 31),
                  address=Address("1 Market St", "San Francisco", "CA", "9410"))
    for e in errors(validate(bad, TODAY)):
        r.note(f"Problem found - {e.field}: {e.message}", "      ")
    res = orch.process(bad);
    orch.process(bad)
    r.result(res)
    delta = sum(writes(sys_)[k] - before[k] for k in before)
    r.check(delta == 0, "No system was changed (the bad name never reached Okta, shipping labels or the tracker)")
    r.check(sys_["tracker"].get_state("H-1001")["payload"]["name"] == "Jon Smyth",
            "Downstream data still holds the last good values")
    r.check(sum("BAD_DATA" in m for _, m in slack.messages) == 1,
            "Only one alert was sent even though the bad record arrived twice")

    # 5 -----------------------------------------------------------------------------------
    r.scenario("Okta has a hiccup in the middle of an update",
               "Maria's start date changes. Okta first returns an error (503), then accepts the change but the reply "
               "is lost (timeout).",
               "The system retries safely. Okta ends up with exactly one update, not duplicates.")
    b2 = replace(b, start_date=date(2026, 11, 9))
    sys_["okta"].inject("transient", "timeout_after_apply")
    before = writes(sys_)["okta"]
    res = orch.process(b2)
    retries = sum(1 for x in store.audit_rows("H-1002") if x["event"] == "RETRY" and x["system"] == "okta")
    r.result(res)
    r.check(retries == 2, f"Okta was retried {retries} times after the two failures")
    r.check(writes(sys_)["okta"] - before == 1,
            "Okta recorded exactly 1 real change (the retry was recognised as a repeat, not applied twice)")

    # 6 -----------------------------------------------------------------------------------
    r.scenario("A name typo is fixed AFTER the Okta account exists",
               "Jon's surname 'Smyth' is corrected to 'Smith', and the first name 'Jon' to 'John'. Changing it would "
               "change his login email.",
               "This is a risky change, so a decision is made: update the login (keeping the old one as an alias) or "
               "hold it back.")
    sys_["okta"].records["H-1001"]["status"] = "PROVISIONED"
    fixed = replace(a, first_name="John", last_name="Smith")
    res = orch.process(fixed)
    d = decision(store, "H-1001", "okta")
    okta = sys_["okta"].records["H-1001"]
    r.result(res)
    r.note(f"Decision: {d.get('action')}  (made by: {d.get('by')})  Reason: {d.get('why')}")
    r.check(
        okta["payload"]["login"] == "john.smith@betterup.com" and "jon.smyth@betterup.com" in okta.get("aliases", []),
        "Login is now john.smith@betterup.com, and the old jon.smyth@betterup.com still works as an alias")

    # 7 -----------------------------------------------------------------------------------
    r.scenario("Address changes after the laptop shipping label was created",
               "Jon moves to a new address. The label for the old address already exists.",
               "With enough time before the start date, the old label is cancelled and a new one is made.")
    sys_["equipment"].records["H-1001"]["status"] = "LABEL_CREATED"
    moved = replace(fixed, address=Address("200 Pine St", "San Francisco", "CA", "94104"))
    res = orch.process(moved)
    d = decision(store, "H-1001", "equipment")
    r.result(res)
    r.note(f"Decision: {d.get('action')}  (made by: {d.get('by')})  Reason: {d.get('why')}")
    r.check(sys_["equipment"].records["H-1001"].get("voided_labels") == 1,
            "The old label was voided and the new address applied")

    r.scenario("Address changes again, but the laptop has already shipped",
               "Jon changes address once more, after the package left the warehouse.",
               "The system must NOT try to change a package in transit. It tells a human instead.")
    sys_["equipment"].records["H-1001"]["status"] = "SHIPPED"
    old_addr = dict(sys_["equipment"].records["H-1001"]["payload"]["ship_to"])
    moved2 = replace(moved, address=Address("9 Mission St", "San Francisco", "CA", "94103"))
    res = orch.process(moved2)
    r.result(res)
    r.check(res["equipment"] == "NOTIFIED" and sys_["equipment"].records["H-1001"]["payload"]["ship_to"] == old_addr,
            "The shipment was left untouched and People Ops was alerted to arrange a reroute")

    # 8 -----------------------------------------------------------------------------------
    r.scenario("Workday rejects an update",
               "Maria's start date moves again, but Workday (the HR system everything else depends on) returns an "
               "error.",
               "Systems that depend on Workday wait. The tracker, which depends on nothing, still updates. The next "
               "run fixes the rest.")
    sys_["workday"].inject("permanent")
    b3 = replace(b2, start_date=date(2026, 11, 16))
    res1 = orch.process(b3)
    res2 = orch.process(b3)
    r.result(res1, "Run 1 (Workday failing)")
    r.result(res2, "Run 2 (next scheduled run)")
    r.check(res1["workday"] == "FAILED" and res1["okta"] == res1["equipment"] == "BLOCKED_UPSTREAM" and res1[
        "tracker"] == "APPLIED",
            "While Workday failed: Okta and Equipment waited, the Tracker still updated")
    r.check(res2["workday"] == res2["okta"] == res2["equipment"] == "APPLIED",
            "On the next run everything caught up with no manual cleanup")

    # 9 -----------------------------------------------------------------------------------
    r.scenario("Audit trail",
               "Every decision and write above was logged in a tamper-evident record (each entry includes a "
               "fingerprint of the one before it).",
               "The log verifies as intact, and any edit to an old entry is detected.")
    rows = len(store.audit_rows())
    intact = store.verify_audit()
    store.db.execute("UPDATE audit SET detail='{}' WHERE id=3")
    r.check(intact, f"All {rows} audit entries verify as intact")
    r.check(not store.verify_audit(), "After secretly editing entry #3, verification fails (tampering detected)")

    print(f"\n{'-' * 92}\nALERTS PEOPLE OPS WOULD HAVE RECEIVED IN SLACK ({len(slack.messages)})\n{'-' * 92}")
    for _, m in slack.messages:
        r._wrap(m, "  * ", "    ")
    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if main(live="--live" in sys.argv) else 1)
