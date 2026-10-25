"""L3 / L4: escalation. Sonnet (or Opus) investigates ONE task and writes the plan.

Only ever started by the user, never by a schedule. The model may look at Home
Assistant through a short allow-list of read-only tools; it cannot change anything.
Its result is a proposal that includes a detailed, step-by-step *instruction* for
the later executor (Haiku, Phase 5). Nothing in here executes a step: the
instruction is stored and shown, and only checked for shape.

Limits per escalation: ``settings.escalation`` (tool rounds and a dollar cap);
the monthly budget guard still applies to every call.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudResult,
    CloudUnavailable,
    cost_usd,
)
from openjarvis.agents.redaction import RedactionError
from openjarvis.agents.reporting import _loads_json
from openjarvis.agents.store import RISKS, Store

TOOL_RESULT_CHARS = 6000
MAX_STEPS = 15
TOOL_NAME = re.compile(r"^ha_[a-z0-9_]+$")

# Read-only Home Assistant lookups an escalated model may use. ``ha_get_overview``
# is called directly, the others through the read proxy (see ``ReadOnlyHA``).
DIAG_TOOLS: dict[str, dict[str, Any]] = {
    "ha_get_state": {
        "description": "Current state and attributes of one or more entities.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "array", "items": {"type": "string"}}},
            "required": ["entity_id"],
        },
    },
    "ha_search": {
        "description": "Search entities by name; optional domain filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "domain_filter": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    "ha_get_integration": {
        "description": "Config entries of an integration (state, title).",
        "input_schema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["domain"],
        },
    },
    "ha_get_logs": {
        "description": "Recent Home Assistant error log lines.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    "ha_get_automation_traces": {
        "description": "Recent execution traces of one automation or script.",
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["automation_id"],
        },
    },
}

SYSTEM = (
    "You are the senior Home Assistant maintenance planner. You investigate ONE task "
    "and write a plan for a human to approve. You may call the read-only tools to "
    "look at the system; you can never change anything yourself. Look only at what "
    "you need (a few calls), then answer with JSON only and no other text.\n"
    "Keys: summary (string), diagnosis (string), risk (low|medium|high), "
    "breaking_change_check (what to verify before the change, e.g. release notes), "
    "needs_user (true when a human must act outside Home Assistant), user_action "
    "(string), instruction (object), verification (list of strings).\n"
    'instruction = {"preconditions": [strings], "steps": [{"tool": "ha_...", '
    '"args": {...}, "purpose": "why", "verify": "how to check this step"}], '
    '"rollback_steps": [same shape]}. The steps are executed later, literally, by '
    "a small model that is not allowed to improvise, so each step must contain the "
    "exact tool name and the exact arguments. Use an empty steps list when no change "
    "in Home Assistant is needed. Placeholders like <email_1> are redacted values: "
    "keep them unchanged."
)


class ReadOnlyHA:
    """The only way an escalated model reaches Home Assistant: allow-listed reads."""

    def __init__(self, ha: Any | None = None) -> None:
        self._ha = ha

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in DIAG_TOOLS:
            return json.dumps({"error": f"tool {name} is not allowed"})
        if self._ha is None:
            from openjarvis.server.ha_dashboard_routes import _HA

            self._ha = _HA()
        client = self._ha._client
        try:
            raw = client.call_tool(
                "ha_call_read_tool", {"name": name, "arguments": args or {}}
            )
            text = raw["content"][0]["text"]
        except Exception as exc:  # noqa: BLE001 - tell the model, keep going
            return json.dumps({"error": str(exc)[:200]})
        return text[:TOOL_RESULT_CHARS]

    def close(self) -> None:
        if self._ha is not None:
            self._ha.close()


def validate_instruction(raw: Any) -> dict[str, Any]:
    """Shape check only. Phase 5 re-validates against the executor allow-list."""
    if not isinstance(raw, dict):
        return {"preconditions": [], "steps": [], "rollback_steps": []}

    def steps(items: Any) -> list[dict[str, Any]]:
        out = []
        for st in (items if isinstance(items, list) else [])[:MAX_STEPS]:
            if (
                isinstance(st, dict)
                and isinstance(st.get("tool"), str)
                and TOOL_NAME.match(st["tool"])
                and isinstance(st.get("args", {}), dict)
            ):
                out.append(
                    {
                        "tool": st["tool"],
                        "args": st.get("args", {}),
                        "purpose": str(st.get("purpose", ""))[:200],
                        "verify": str(st.get("verify", ""))[:200],
                    }
                )
        return out

    return {
        "preconditions": [str(x)[:200] for x in raw.get("preconditions", [])][:8],
        "steps": steps(raw.get("steps")),
        "rollback_steps": steps(raw.get("rollback_steps")),
    }


def clean_proposal(body: dict[str, Any]) -> dict[str, Any]:
    risk = body["risk"]
    if risk not in RISKS:
        raise ValueError("risk")
    return {
        "summary": str(body.get("summary", ""))[:600],
        "diagnosis": str(body.get("diagnosis", ""))[:800],
        "risk": risk,
        "breaking_change_check": str(body.get("breaking_change_check", ""))[:500],
        "needs_user": bool(body.get("needs_user", False)),
        "user_action": str(body.get("user_action", ""))[:300],
        "instruction": validate_instruction(body.get("instruction")),
        "verification": [str(x)[:200] for x in body.get("verification", [])][:8],
    }


def estimate_investigation(
    prices: dict[str, Any] | None, base_in: int, rounds: int
) -> dict[str, Any]:
    """Upper bound: every round re-sends the growing conversation."""
    per_result = 1500
    tokens_in = sum(base_in + r * per_result for r in range(rounds + 1))
    tokens_out = rounds * 350 + 1800
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": round(cost_usd(prices, usage), 4) if prices else None,
        "rounds": rounds,
    }


def escalate(
    store: Store,
    client: CloudClient,
    payload: dict[str, Any],
    *,
    tier: str,
    ha: ReadOnlyHA | None = None,
    on_tool: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the tool loop. Returns status, the cleaned proposal and what it cost."""
    cfg = store.settings()["escalation"]
    cap = float(cfg["opus_max_usd"] if tier == "deep" else cfg["max_usd"])
    model = client.model_for(tier)
    tools = [{"name": n, **d} for n, d in DIAG_TOOLS.items()]
    messages: list[dict[str, Any]] = [{"role": "user", "content": json.dumps(payload)}]
    ha = ha or ReadOnlyHA()
    spent, calls, last = 0.0, 0, None
    try:
        for rnd in range(int(cfg["max_rounds"]) + 1):
            final = rnd == int(cfg["max_rounds"]) or spent >= cap
            res: CloudResult = client.complete(
                tier,
                system=SYSTEM,
                messages=messages,
                max_tokens=2200,
                tools=None if final else tools,
                manual=True,
                purpose=f"escalate:{payload['id']}",
            )
            if res.dry_run:
                return {"status": "dry_run", "model": model}
            spent += res.cost_usd
            last = res
            uses = [b for b in res.blocks if b["type"] == "tool_use"]
            if not uses or final:
                break
            messages.append({"role": "assistant", "content": res.blocks})
            results = []
            for u in uses:
                calls += 1
                if on_tool:
                    on_tool(u["name"], u["input"])
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": u["id"],
                        "content": ha.call(u["name"], u["input"]),
                    }
                )
            messages.append({"role": "user", "content": results})
    except BudgetExceeded as exc:
        return {
            "status": "skipped_budget",
            "detail": str(exc),
            "spent_usd": round(spent, 4),
        }
    except CloudUnavailable as exc:
        return {"status": "skipped_no_key", "detail": str(exc)}
    except RedactionError as exc:
        return {"status": "blocked_redaction", "detail": str(exc)}
    finally:
        ha.close()

    if last is None:
        return {"status": "bad_reply", "spent_usd": round(spent, 4)}
    try:
        proposal = clean_proposal(_loads_json(last.text))
    except (ValueError, KeyError, TypeError, AttributeError):
        return {
            "status": "bad_reply",
            "spent_usd": round(spent, 4),
            "tool_calls": calls,
        }
    return {
        "status": "ok",
        "proposal": proposal,
        "model": last.model,
        "spent_usd": round(spent, 4),
        "tool_calls": calls,
        "capped": spent >= cap,
    }
