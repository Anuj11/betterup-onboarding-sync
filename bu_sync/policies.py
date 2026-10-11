"""Deterministic guardrails. These decide WHETHER a change can be applied and
WHICH actions are even legal. Claude only chooses among the legal ones."""
from __future__ import annotations

from .models import Action, Assessment, System

ADDR_KEYS = ("ship_to", "home_address")


def _changed(live: dict, want: dict, keys) -> list[str]:
    return [k for k in keys if k in want and live.get(k) != want[k]]


def assess(system: System, live: dict, want: dict, last_applied: dict | None) -> Assessment:
    payload, status = live["payload"], live["status"]

    # 0. Someone edited the system by hand since we last wrote it.
    if last_applied and live["exists"] and payload != last_applied:
        return Assessment(False, "OUT_OF_BAND_EDIT",
                          f"{system.value} no longer matches our last write (manual edit?)",
                          [Action.APPLY, Action.HOLD], Action.HOLD)

    if not live["exists"]:
        return Assessment(True)

    if system is System.OKTA and status in ("PROVISIONED", "ACTIVE") and payload.get("login") != want["login"]:
        return Assessment(False, "LOGIN_CHANGE_AFTER_PROVISION",
                          f"login would change {payload.get('login')} -> {want['login']} on a {status} account "
                          "(SSO, email, calendar invites already reference it)",
                          [Action.APPLY_KEEP_LOGIN, Action.APPLY, Action.HOLD], Action.APPLY_KEEP_LOGIN)

    if system is System.EQUIPMENT:
        moved = _changed(payload, want, ("ship_to", "recipient"))
        if moved and status == "LABEL_CREATED":
            return Assessment(False, "LABEL_ALREADY_CREATED",
                              "shipping label exists for the old address/recipient; voiding costs a re-pick",
                              [Action.APPLY_FORCE, Action.HOLD], Action.HOLD)
        if moved and status in ("SHIPPED", "DELIVERED"):
            return Assessment(False, "ALREADY_SHIPPED",
                              f"shipment is {status} address/recipient cannot be changed via API",
                              [Action.NOTIFY_ONLY, Action.HOLD], Action.HOLD)

    if system is System.WORKDAY and status == "HIRED" and payload.get("start_date") != want["start_date"]:
        return Assessment(False, "WORKDAY_ALREADY_HIRED",
                          "hire event already completed; start date needs a Change Job business process",
                          [Action.NOTIFY_ONLY, Action.HOLD], Action.HOLD)
    return Assessment(True)
