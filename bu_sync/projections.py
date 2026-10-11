"""Pure functions: canonical Hire -> the desired payload for each system.

Everything downstream is "converge system X to projection(hire)". Because the
projection is pure, its hash is a natural idempotency key: same desired state
=> same key => no duplicate write, however many times a change event replays.
"""
from __future__ import annotations

import unicodedata
from datetime import timedelta

from .models import Hire, System, prev_business_day

GATE_LEAD_DAYS = 7  # IT hardware form, background check, Okta provisioning


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum() or c == "-")


def okta_login(h: Hire) -> str:
    return f"{_ascii(h.first_name)}.{_ascii(h.last_name)}@betterup.com"


def gate_due(h: Hire):
    return prev_business_day(h.start_date - timedelta(days=GATE_LEAD_DAYS))


def project(system: System, h: Hire) -> dict:
    addr = vars(h.address)
    if system is System.WORKDAY:
        return {"legal_first_name": h.first_name, "legal_last_name": h.last_name,
                "preferred_name": h.preferred_name, "start_date": h.start_date.isoformat(),
                "job_title": h.job_title, "manager_email": h.manager_email,
                "personal_email": h.personal_email, "home_address": addr}
    if system is System.OKTA:
        return {"first_name": h.first_name, "last_name": h.last_name,
                "login": okta_login(h), "title": h.job_title,
                "manager_email": h.manager_email,
                "activation_date": h.start_date.isoformat()}
    if system is System.EQUIPMENT:
        return {"recipient": f"{h.preferred_name or h.first_name} {h.last_name}",
                "ship_to": addr, "ship_by": gate_due(h).isoformat(),
                "need_by": h.start_date.isoformat()}
    if system is System.TRACKER:
        monday = h.start_date - timedelta(days=h.start_date.weekday())
        return {"hire_id": h.hire_id, "name": f"{h.first_name} {h.last_name}",
                "start_date": h.start_date.isoformat(),
                "cohort": f"cohort-{monday.isoformat()}",
                "hardware_form_due": gate_due(h).isoformat(),
                "background_check_due": gate_due(h).isoformat(),
                "okta_provisioning_due": gate_due(h).isoformat(),
                "city": h.address.city}
    raise ValueError(system)
