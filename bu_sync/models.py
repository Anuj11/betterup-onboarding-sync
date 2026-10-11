"""Canonical data model. Ashby is the source of truth for pre-start hires;
Workday is the source for off-cycle hires created outside Ashby."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from enum import Enum


@dataclass(frozen=True)
class Address:
    line1: str
    city: str
    state: str
    postal_code: str
    country: str = "US"


@dataclass(frozen=True)
class Hire:
    hire_id: str                 # stable key (Ashby candidate/application id)
    first_name: str
    last_name: str
    personal_email: str
    start_date: date
    address: Address
    job_title: str
    manager_email: str
    source: str = "ashby"        # "ashby" | "workday" (off-cycle)
    preferred_name: str | None = None


class System(str, Enum):
    WORKDAY = "workday"
    OKTA = "okta"
    EQUIPMENT = "equipment"
    TRACKER = "tracker"


# Dependency order: Workday is the HR system of record; Okta and equipment
# derive from it. The tracker is a read model and never blocks anything.
SYNC_ORDER = [System.WORKDAY, System.OKTA, System.EQUIPMENT, System.TRACKER]
DEPENDS_ON = {System.OKTA: System.WORKDAY, System.EQUIPMENT: System.WORKDAY}


class Action(str, Enum):
    APPLY = "APPLY"                        # apply desired state as-is
    APPLY_KEEP_LOGIN = "APPLY_KEEP_LOGIN"  # Okta: update profile, freeze login
    APPLY_FORCE = "APPLY_FORCE"            # equipment: void label + recreate
    NOTIFY_ONLY = "NOTIFY_ONLY"            # do not touch system, tell a human
    HOLD = "HOLD"                          # park for human approval


class Status(str, Enum):
    APPLIED = "APPLIED"
    HELD = "HELD"                          # waiting for a human
    NOTIFIED = "NOTIFIED"                  # human informed, system untouched
    FAILED = "FAILED"                      # permanent error or retries exhausted
    BLOCKED_UPSTREAM = "BLOCKED_UPSTREAM"  # dependency not applied yet
    QUARANTINED = "QUARANTINED"            # failed validation, nothing fanned out


@dataclass
class Assessment:
    """Result of comparing desired state with what the live system allows."""
    ok: bool
    code: str = "OK"
    reason: str = ""
    allowed_actions: list[Action] = field(default_factory=list)
    safe_default: Action = Action.HOLD


@dataclass
class Decision:
    action: Action
    rationale: str
    slack_message: str
    decided_by: str                        # "claude:<model>" | "offline-rules" | "human"


def prev_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def hire_to_dict(h: Hire) -> dict:
    d = asdict(h)
    d["start_date"] = h.start_date.isoformat()
    return d
