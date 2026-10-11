"""Claude as the *exception handler*, not the pipeline.

Deterministic code finds the exception and enumerates the legal actions
(policies.py). Claude weighs the context and picks ONE legal action + drafts the
Slack note. Its output is schema-constrained (forced tool call), then re-checked
by the orchestrator guard. Any API failure falls back to conservative rules.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from .models import Action, Decision

DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM_PROMPT = """You are the exception handler inside BetterUp's onboarding sync.
A deterministic pipeline hit a change it cannot apply blindly and gives you the
context plus the ONLY actions that are legal. Choose exactly one.
Rules: pick from allowed_actions only; when unsure choose safe_default; weigh how
close the start date is against the cost of a wrong or delayed change; do not
invent facts that are not in the context. slack_message goes to the People-Ops
channel: under 400 characters, name the hire_id, what happened, what a human
must do (or 'no action needed'). Personal data has been redacted on purpose."""

TOOL = {
    "name": "resolve_exception",
    "description": "Pick one allowed action for a sync exception and draft the Slack note.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [a.value for a in Action]},
            "rationale": {"type": "string", "description": "<=300 chars, why this action"},
            "slack_message": {"type": "string"},
        },
        "required": ["action", "rationale", "slack_message"],
    },
}

_DROP = {"line1", "personal_email", "manager_email", "postal_code"}


def redact(v):
    """Data minimisation: Claude never sees street, postal code or emails."""
    if isinstance(v, dict):
        return {k: ("<redacted>" if k in _DROP else redact(x)) for k, x in v.items()}
    return v


class ExceptionHandler(Protocol):
    def decide(self, ctx: dict) -> Decision: ...


def _slack(ctx: dict, text: str) -> str:
    return f"[{ctx['code']}] hire {ctx['hire_id']} / {ctx['system']}: {text}"


class OfflineHandler:
    """Rule-based stand-in with the same interface. Used when there is no API
    key (tests, demo) and as the fallback if the Claude call fails."""
    label = "offline-rules"

    def decide(self, ctx: dict) -> Decision:
        code, days, status = ctx["code"], ctx["days_until_start"], ctx["live_status"]
        if code == "LOGIN_CHANGE_AFTER_PROVISION":
            a = Action.APPLY if (status == "PROVISIONED" and days >= 7) else Action.APPLY_KEEP_LOGIN
            why = ("account not yet in use, start is >=7d out: rotate login, keep old as alias"
                   if a is Action.APPLY else "account in use or start is close: keep login, update profile only")
        elif code == "LABEL_ALREADY_CREATED":
            a = Action.APPLY_FORCE if days >= 8 else Action.HOLD
            why = "enough runway to void and reship" if a is Action.APPLY_FORCE else "too close to start; IT should decide"
        elif code in ("ALREADY_SHIPPED", "WORKDAY_ALREADY_HIRED"):
            a, why = Action.NOTIFY_ONLY, "cannot be changed via API; a human must act"
        else:
            a, why = Action(ctx["safe_default"]), "no rule for this exception; using safe default"
        if a.value not in ctx["allowed_actions"]:
            a = Action(ctx["safe_default"])
        return Decision(a, why, _slack(ctx, f"{why}. Action taken: {a.value}."), self.label)


class ClaudeHandler:
    def __init__(self, client=None, model: str = DEFAULT_MODEL):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
        self.client, self.model = client, model

    def decide(self, ctx: dict) -> Decision:
        resp = self.client.messages.create(
            model=self.model, max_tokens=600, system=SYSTEM_PROMPT,
            tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": json.dumps(ctx, indent=2, default=str)}])
        block = next(b for b in resp.content if b.type == "tool_use")
        out = block.input
        return Decision(Action(out["action"]), out["rationale"][:300], out["slack_message"][:400],
                        f"claude:{self.model}")


class FallbackHandler:
    """Claude first; on ANY failure (network, 429, bad schema) use offline rules."""
    def __init__(self, primary: ExceptionHandler, fallback: ExceptionHandler | None = None):
        self.primary, self.fallback = primary, fallback or OfflineHandler()

    def decide(self, ctx: dict) -> Decision:
        try:
            return self.primary.decide(ctx)
        except Exception as e:                       # noqa: BLE001 - deliberate catch-all
            d = self.fallback.decide(ctx)
            d.rationale += f" (Claude unavailable: {type(e).__name__})"
            return d


def default_handler() -> ExceptionHandler:
    if os.getenv("ANTHROPIC_API_KEY"):
        return FallbackHandler(ClaudeHandler())
    return OfflineHandler()
