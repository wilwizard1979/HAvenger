"""Real Claude cost over time, from the Janitor ledger (dry runs excluded).

The split is by what the call was for: ``architect*`` purposes belong to the
Architect, everything else to the Janitor. A reset stores a "counted from" date; it
never deletes ledger rows, so the budget limits keep working.
"""

from __future__ import annotations

import time
from typing import Any

from openjarvis.agents.store import Store

FACT = "cost_counted_from"


def _sum(store: Store, where: str, args: tuple) -> float:
    rows = store._all(  # noqa: SLF001 - the ledger has no per-function total
        f"SELECT COALESCE(SUM(cost_usd),0) AS s FROM runs WHERE dry_run=0 AND {where}",
        args,
    )
    return float(rows[0]["s"])


def month_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def summary(store: Store, now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    since = float(store.facts().get(FACT, 0) or 0)
    first = store._all(  # noqa: SLF001
        "SELECT MIN(started) AS t FROM runs WHERE dry_run=0 AND cost_usd>0"
    )[0]["t"]
    arch = _sum(store, "started>=? AND purpose LIKE 'architect%'", (since,))
    total = _sum(store, "started>=?", (since,))
    month = _sum(store, "started>=?", (max(since, month_start(now)),))
    return {
        "total_usd": round(total, 4),
        "month_usd": round(month, 4),
        "janitor_usd": round(total - arch, 4),
        "architect_usd": round(arch, 4),
        "counted_from": since or first,
        "reset": bool(since),
    }


def reset(store: Store, now: float | None = None) -> dict[str, Any]:
    store.set_facts({FACT: now if now is not None else time.time()})
    return summary(store, now)
