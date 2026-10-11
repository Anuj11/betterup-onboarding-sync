"""Mock connectors with the failure modes real APIs have.

Every write carries an idempotency key; a replay with the same key returns the
original result without touching state again (how Workday/Okta/Sheets wrappers
should be written in prod, and what makes "timeout after apply" safe to retry).
"""
from __future__ import annotations

import copy
from collections import deque


class TransientError(Exception):
    """429/503/timeout: safe to retry with the same idempotency key."""


class PermanentError(Exception):
    """400/403/404: retrying will not help."""


class MockSystem:
    name = "mock"

    def __init__(self):
        self.records: dict[str, dict] = {}
        self._idem: dict[str, dict] = {}
        self._faults: deque[str] = deque()
        self.write_log: list[tuple[str, str]] = []   # (hire_id, idem_key) real writes only

    # -- fault injection ---------------------------------------------------
    def inject(self, *faults: str):
        """'transient' | 'timeout_after_apply' | 'permanent'"""
        self._faults.extend(faults)

    # -- API ---------------------------------------------------------------
    def get_state(self, hire_id: str) -> dict:
        rec = self.records.get(hire_id)
        if not rec:
            return {"exists": False, "status": None, "payload": {}}
        return {"exists": True, "status": rec["status"], "payload": copy.deepcopy(rec["payload"])}

    def upsert(self, hire_id: str, payload: dict, idem_key: str, *, force: bool = False) -> dict:
        fault = self._faults.popleft() if self._faults else None
        if fault == "transient":
            raise TransientError(f"{self.name}: 503 service unavailable")
        if fault == "permanent":
            raise PermanentError(f"{self.name}: 400 rejected payload")
        if idem_key in self._idem:                      # replay: no second write
            result = self._idem[idem_key]
        else:
            result = self._apply(hire_id, payload, force)
            self._idem[idem_key] = result
            self.write_log.append((hire_id, idem_key))
        if fault == "timeout_after_apply":              # applied, but caller never hears
            raise TransientError(f"{self.name}: timeout after apply (ambiguous)")
        return result

    def _apply(self, hire_id, payload, force) -> dict:
        rec = self.records.setdefault(hire_id, {"status": self.initial_status, "payload": {}})
        rec["payload"] = copy.deepcopy(payload)
        return {"ok": True}

    initial_status = "ACTIVE"


class MockWorkday(MockSystem):
    name = "workday"
    initial_status = "PRE_HIRE"       # -> "HIRED" once start date passes


class MockOkta(MockSystem):
    name = "okta"
    initial_status = "STAGED"         # STAGED -> PROVISIONED -> ACTIVE

    def _apply(self, hire_id, payload, force):
        rec = self.records.get(hire_id)
        old_login = rec["payload"].get("login") if rec else None
        res = super()._apply(hire_id, payload, force)
        if old_login and old_login != payload["login"]:
            self.records[hire_id].setdefault("aliases", []).append(old_login)  # rotate keeps old as alias
        return res


class MockEquipment(MockSystem):
    name = "equipment"
    initial_status = "ORDERED"        # ORDERED -> LABEL_CREATED -> SHIPPED -> DELIVERED

    def _apply(self, hire_id, payload, force):
        rec = self.records.get(hire_id)
        if rec and rec["status"] == "LABEL_CREATED" and force:
            rec["status"] = "ORDERED"             # void label; new one cut by IT workflow
            rec["voided_labels"] = rec.get("voided_labels", 0) + 1
        if rec and rec["status"] in ("SHIPPED", "DELIVERED"):
            raise PermanentError("equipment: shipment already in transit; address is immutable")
        return super()._apply(hire_id, payload, force)


class MockTracker(MockSystem):
    name = "tracker"                  # Google Sheet row keyed by hire_id


class MockSlack:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def post(self, channel: str, text: str):
        self.messages.append((channel, text))


def build_systems():
    return {
        "workday": MockWorkday(),
        "okta": MockOkta(),
        "equipment": MockEquipment(),
        "tracker": MockTracker()
    }
