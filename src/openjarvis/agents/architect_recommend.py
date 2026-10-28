"""Recommend step of Jarvis the Architect: one Sonnet/Opus call, only on a click.

The page first shows the preview (``preview``): what would be sent, its size and the
worst-case cost. Nothing reaches a model until ``recommend`` is called, and that
call goes through the Janitor's ``CloudClient`` (monthly budget, price table, usage
ledger and the dry-run switch). On top of that the Architect has its own monthly
sub-limit. The answer is text for the user to read; nothing is applied here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from openjarvis.agents.architect import ENTITY_DOMAINS, ENTITY_IN_TEXT
from openjarvis.agents.architect_context import (
    OUTPUT_TOKENS,
    RULES,
    TOKEN_CAP,
    build_context,
)
from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudUnavailable,
)
from openjarvis.agents.store import Store

ARCHITECT_LIMIT_USD = 5.0
# page label -> Janitor tier (diagnose = Sonnet, deep = Opus)
TIER_OF = {"sonnet": "diagnose", "opus": "deep"}
PURPOSE = "architect"


class ArchitectRefused(RuntimeError):
    """The request cannot be sent (too big, nothing to review, sub-limit)."""


def month_start(ts: float | None = None) -> float:
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def limit_usd(store: Store) -> float:
    cfg = store.settings().get("architect") or {}
    return float(cfg.get("monthly_limit_usd", ARCHITECT_LIMIT_USD))


def spend_usd(store: Store, ts: float | None = None) -> float:
    rows = store._all(  # noqa: SLF001 - the ledger has no per-purpose total
        "SELECT COALESCE(SUM(cost_usd),0) AS s FROM runs "
        "WHERE purpose LIKE 'architect%' AND started >= ?",
        (month_start(ts),),
    )
    return float(rows[0]["s"])


def _estimates(client: CloudClient, ctx: dict[str, Any]) -> dict[str, float]:
    chars = len(RULES) + len(ctx["text"])
    return {
        name: round(client.estimate_cost(tier, chars, OUTPUT_TOKENS), 4)
        for name, tier in TIER_OF.items()
    }


def preview(
    inv: dict[str, Any],
    question: str,
    picked: list[str] | None = None,
    client: CloudClient | None = None,
    intent: str = "",
) -> dict[str, Any]:
    """What would be sent and what it could cost. Calls no model."""
    client = client or CloudClient()
    ctx = build_context(inv, question, picked, intent)
    ctx["estimate_usd"] = _estimates(client, ctx)
    ctx["token_cap"] = TOKEN_CAP
    ctx["spent_usd"] = round(spend_usd(client.store), 4)
    ctx["limit_usd"] = limit_usd(client.store)
    ctx["dry_run"] = client.is_dry_run()
    ctx["can_send"] = (
        bool(ctx["targets"] or ctx["intent"] == "create") and not ctx["over_cap"]
    )
    return ctx


def unknown_entities(answer: str, inv: dict[str, Any]) -> list[str]:
    """Entity ids in the answer that Home Assistant does not have."""
    known = set(inv.get("entity_info", {})) | {it["entity_id"] for it in inv["items"]}
    found = {
        m
        for m in ENTITY_IN_TEXT.findall(answer.lower())
        if m.split(".")[0] in ENTITY_DOMAINS
    }
    return sorted(found - known)


def cases_path() -> Path:
    from openjarvis.core.config import DEFAULT_CONFIG_DIR

    return Path(DEFAULT_CONFIG_DIR) / "architect" / "cases.json"


def load_cases() -> list[dict[str, Any]]:
    try:
        return json.loads(cases_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _next_case_id(cases: list[dict[str, Any]], now: float) -> str:
    day = time.strftime("%Y_%m_%d", time.localtime(now))
    n = sum(1 for c in cases if c["id"].startswith(f"A_{day}_")) + 1
    return f"A_{day}_{n:03d}"


def save_case(case: dict[str, Any]) -> None:
    cases = load_cases()
    cases.append(case)
    path = cases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases[-200:]), encoding="utf-8")


def recommend(
    inv: dict[str, Any],
    question: str,
    model: str,
    picked: list[str] | None = None,
    client: CloudClient | None = None,
    intent: str = "",
) -> dict[str, Any]:
    """Send the request to Sonnet or Opus. Call this only from an explicit click."""
    if model not in TIER_OF:
        raise ArchitectRefused("model must be 'sonnet' or 'opus'")
    client = client or CloudClient()
    now = time.time()
    pre = preview(inv, question, picked, client, intent)
    if pre["over_cap"]:
        raise ArchitectRefused(
            f"The request is about {pre['tokens']} tokens (cap {TOKEN_CAP}). "
            "Ask a narrower question or pick the automation."
        )
    if not pre["can_send"]:
        raise ArchitectRefused("No enabled automation matches. Pick one or rephrase.")
    cost = pre["estimate_usd"][model]
    if pre["spent_usd"] + cost > pre["limit_usd"]:
        raise ArchitectRefused(
            f"Architect limit: spent ${pre['spent_usd']:.2f} of "
            f"${pre['limit_usd']:.2f} this month."
        )
    cases = load_cases()
    case_id = _next_case_id(cases, now)
    try:
        res = client.complete(
            TIER_OF[model],
            system=RULES,
            messages=[{"role": "user", "content": pre["text"]}],
            max_tokens=OUTPUT_TOKENS,
            manual=True,
            purpose=f"{PURPOSE}:{case_id}",
        )
    except (BudgetExceeded, CloudUnavailable) as exc:
        raise ArchitectRefused(str(exc)) from exc
    answer = res.text
    case = {
        "id": case_id,
        "at": now,
        "question": question,
        "intent": pre["intent"],
        "model": model,
        "targets": pre["targets"],
        "tokens": pre["tokens"],
        "cost_usd": res.cost_usd,
        "dry_run": res.dry_run,
        "answer": answer,
        "unknown_entities": unknown_entities(answer, inv) if answer else [],
    }
    if not res.dry_run:
        save_case(case)
    return case
