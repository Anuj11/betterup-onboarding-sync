"""Runnable end-to-end demo on mocked systems:  python -m bu_sync.demo [--live]"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import date

from .agent import ClaudeHandler, FallbackHandler, OfflineHandler
from .connectors import MockSlack, build_systems
from .models import Address, Hire, System
from .orchestrator import Orchestrator
from .projections import project
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


def main(live: bool = False):
    orch, sys_, slack, store = build(live)
    a, b = sample_hires()
    print(f"exception handler: {type(orch.handler).__name__}")

    show("1. initial sync of two Ashby hires + one off-cycle hire found in Workday")
    print("   A:", orch.process(a)); print("   B:", orch.process(b))
    sys_["workday"].records["H-9001"] = {"status": "PRE_HIRE", "payload": {
        "legal_first_name": "Priya", "legal_last_name": "Nair", "preferred_name": None, "start_date": "2026-11-09",
        "job_title": "Data Analyst", "manager_email": "mgr3@betterup.com", "personal_email": "priya.n@gmail.com",
        "home_address": vars(Address("12 Pine St", "Seattle", "WA", "98101"))}}
    print("   off-cycle discovered:", orch.discover_off_cycle(), "-> tracker row:", sys_["tracker"].get_state("H-9001")["payload"]["cohort"])

    show("2. replay same events (webhook retry / nightly poll): expect zero new writes")
    before = writes(sys_); orch.process(a); orch.process(b)
    print("    new writes:", {k: writes(sys_)[k] - before[k] for k in before})

    show("3. start date moves Oct 26 -> Nov 2 for H-1001: fans out to every system + recomputes gate deadlines")
    a = replace(a, start_date=date(2026, 11, 2))
    print("   ", orch.process(a))
    t = sys_["tracker"].get_state("H-1001")["payload"]
    print("    tracker:", {k: t[k] for k in ("cohort", "hardware_form_due")}, "| equipment ship_by:", sys_["equipment"].get_state("H-1001")["payload"]["ship_by"])

    show("4. bad Ashby edit (digit in name, short ZIP, Saturday start): quarantined, nothing cascades")
    before = writes(sys_)
    bad = replace(a, first_name="J0n", start_date=date(2026, 10, 31), address=Address("1 Market St", "San Francisco", "CA", "9410"))
    print("   ", orch.process(bad)); orch.process(bad)
    print("    new writes:", {k: writes(sys_)[k] - before[k] for k in before}, "| alerts sent:", sum("BAD_DATA" in m for _, m in slack.messages), "(deduped)")

    show("5. Okta flaps (503) then times out AFTER applying: retries reuse the idempotency key")
    b2 = replace(b, start_date=date(2026, 11, 9))
    sys_["okta"].inject("transient", "timeout_after_apply")
    before = writes(sys_)
    print("   ", orch.process(b2)); print("    okta real writes:", writes(sys_)["okta"] - before["okta"], "(exactly 1)")

    show("6. name typo fixed AFTER Okta provisioned: login would change -> exception handler decides")
    sys_["okta"].records["H-1001"]["status"] = "PROVISIONED"
    fixed = replace(a, first_name="John", last_name="Smith")
    print("   ", orch.process(fixed))
    print("    okta:", sys_["okta"].records["H-1001"]["payload"]["login"], "aliases:", sys_["okta"].records["H-1001"].get("aliases"))

    show("7a. address change after shipping label exists -> void & recreate (runway permitting)")
    sys_["equipment"].records["H-1001"]["status"] = "LABEL_CREATED"
    moved = replace(fixed, address=Address("200 Pine St", "San Francisco", "CA", "94104"))
    print("   ", orch.process(moved), "| voided labels:", sys_["equipment"].records["H-1001"].get("voided_labels"))
    show("7b. same change but package already SHIPPED -> API can't help, human is notified")
    sys_["equipment"].records["H-1001"]["status"] = "SHIPPED"
    moved2 = replace(moved, address=Address("9 Mission St", "San Francisco", "CA", "94103"))
    print("   ", orch.process(moved2))

    show("8. Workday hard-fails (400) on a start-date move: Okta/equipment blocked, tracker still updates, next run self-heals")
    sys_["workday"].inject("permanent")
    b3 = replace(b2, start_date=date(2026, 11, 16))
    print("   ", orch.process(b3)); print("    next run:", orch.process(b3))

    show("9. audit trail")
    print("    rows:", len(store.audit_rows()), "| chain valid:", store.verify_audit())
    store.db.execute("UPDATE audit SET detail='{}' WHERE id=3"); print("    after tampering row 3 -> chain valid:", store.verify_audit())
    print("\nSlack messages posted:")
    for ch, m in slack.messages:
        print(f"  {ch}: {m[:170]}")


if __name__ == "__main__":
    main(live="--live" in sys.argv)
