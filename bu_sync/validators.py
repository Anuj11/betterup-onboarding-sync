"""Pre-flight validation gate. Runs BEFORE any fan-out so one bad Ashby edit
cannot cascade into Workday, the Okta email, shipping labels and the tracker."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .models import Hire

NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)*$", re.UNICODE)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
POSTAL = {"US": re.compile(r"^\d{5}(-\d{4})?$"), "IN": re.compile(r"^\d{6}$"),
          "GB": re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$")}
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
                "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
# Tiny cross-field sanity table (prod: Google Maps Address Validation, part of
# the Google Workspace/Cloud estate, rather than a new vendor).
KNOWN_CITY_STATE = {"san francisco": "CA", "austin": "TX", "new york": "NY", "seattle": "WA", "denver": "CO"}


@dataclass(frozen=True)
class Issue:
    severity: str   # "error" blocks fan-out, "warning" is recorded and passes
    field: str
    code: str
    message: str


def validate(h: Hire, today: date) -> list[Issue]:
    out: list[Issue] = []
    for f, v in (("first_name", h.first_name), ("last_name", h.last_name)):
        if not v or not v.strip():
            out.append(Issue("error", f, "NAME_EMPTY", f"{f} is empty"))
        elif v != v.strip() or not NAME_RE.match(v):
            out.append(Issue("error", f, "NAME_CHARS",
                             f"{f}={v!r} has digits/symbols/extra whitespace; it would flow into the Okta login and "
                             f"shipping label"))
        elif v.isupper() or v.islower():
            out.append(Issue("warning", f, "NAME_CASE", f"{f}={v!r} looks mis-cased"))
    if h.first_name and h.first_name.strip().lower() == h.last_name.strip().lower():
        out.append(Issue("warning", "name", "NAME_DUPLICATE", "first and last name are identical"))
    if not EMAIL_RE.match(h.personal_email or ""):
        out.append(Issue("error", "personal_email", "EMAIL_FORMAT", "personal email is not valid"))
    if h.start_date < today:
        out.append(Issue("error", "start_date", "START_IN_PAST", f"start_date {h.start_date} is before {today}"))
    elif h.start_date.weekday() >= 5:
        out.append(Issue("error", "start_date", "START_WEEKEND", f"start_date {h.start_date} is a weekend"))
    elif h.start_date.weekday() != 0:
        out.append(Issue("warning", "start_date", "START_NOT_MONDAY", "BetterUp cohorts normally start on a Monday"))
    a = h.address
    if not (a.line1 and a.city and a.postal_code):
        out.append(Issue("error", "address", "ADDRESS_INCOMPLETE", "line1/city/postal_code required for shipping"))
    pat = POSTAL.get(a.country)
    if pat and not pat.match(a.postal_code or ""):
        out.append(Issue("error", "address.postal_code", "POSTAL_FORMAT", f"{a.postal_code!r} invalid for {a.country}"))
    if a.country == "US":
        if a.state not in US_STATES:
            out.append(Issue("error", "address.state", "STATE_INVALID", f"{a.state!r} is not a US state code"))
        elif KNOWN_CITY_STATE.get(a.city.lower(), a.state) != a.state:
            out.append(Issue("error", "address", "CITY_STATE_MISMATCH",
                             f"{a.city} is in {KNOWN_CITY_STATE[a.city.lower()]}, not {a.state}"))
    return out


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
