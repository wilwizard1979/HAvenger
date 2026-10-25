"""L2 triage (Haiku), the digest and its schedule.

* ``build_digest`` is deterministic text from real data, never model output
* ``run_triage`` asks Haiku for a one-line note, a suspected cause and (only for
  clusters whose rule is ``auto``) an effort rating; it re-runs for a cluster only
  when its members changed; it goes through ``CloudClient`` (redaction, budget,
  dry-run) and never touches Home Assistant
* ``generate_report`` = triage (when allowed) + digest, stored in ``reports``
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudUnavailable,
)
from openjarvis.agents.clusters import build_clusters, matrix_counts
from openjarvis.agents.redaction import RedactionError
from openjarvis.agents.store import EFFORTS, Store

logger = logging.getLogger(__name__)

TRIAGE_BATCH = 25
TRIAGE_MAX_TOKENS = 4000
CRITICAL_CATEGORIES = ("critical_entity", "backup", "log")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

TRIAGE_SYSTEM = (
    "You triage Home Assistant maintenance findings for a read-only assistant. "
    "You get clusters of related issues as JSON. For each cluster return a short "
    "note (max 140 characters, what to look at first), a suspected root cause "
    "(max 140 characters) and, only when 'rate_effort' is true, an effort: "
    "quick (minutes, no risk), medium (under an hour) or hard (research, risk or "
    "waiting for a release). Never propose commands or config changes. Placeholders "
    "like <email_1> are redacted values: keep them as they are. Answer with JSON "
    'only: {"clusters":[{"id":"...","note":"...","cause":"...","effort":"quick"}]}'
)


# -- schedule ------------------------------------------------------------------
def last_scheduled(now: float, schedule: dict[str, Any]) -> float:
    """Epoch of the most recent scheduled report time at or before ``now``."""
    tz = ZoneInfo(schedule["timezone"])
    local = datetime.fromtimestamp(now, tz)
    hh, mm = (int(x) for x in schedule["time"].split(":"))
    at = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if schedule["frequency"] == "weekly":
        back = (local.weekday() - WEEKDAYS.index(schedule["weekday"])) % 7
        at -= timedelta(days=back)
    if at > local:
        at -= timedelta(days=7 if schedule["frequency"] == "weekly" else 1)
    return at.timestamp()


def report_due(store: Store, now: float) -> bool:
    last = store.latest_report()
    return (last["created_at"] if last else 0.0) < last_scheduled(
        now, store.settings()["schedule"]
    )


# -- triage ------------------------------------------------------------------------
def _loads_json(text: str) -> Any:
    """JSON from a model reply: plain, in a code fence, or after a short preface."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise
        return json.loads(t[a : b + 1])


def run_triage(
    store: Store, client: CloudClient, clusters: list[dict[str, Any]], *, manual: bool
) -> dict[str, Any]:
    todo = [c for c in clusters if not c["triage_current"]][:TRIAGE_BATCH]
    if not todo:
        return {"status": "nothing_to_do", "clusters": 0}
    payload = [
        {
            "id": c["id"],
            "label": c["label"],
            "categories": c["categories"],
            "issues": c["count"],
            "severity": c["severity"],
            "rate_effort": c["effort_source"] in ("default", "haiku"),
            "examples": c["examples"],
        }
        for c in todo
    ]
    try:
        res = client.complete(
            "triage",
            system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
            max_tokens=TRIAGE_MAX_TOKENS,
            manual=manual,
            purpose="triage",
        )
    except BudgetExceeded as exc:
        return {"status": "skipped_budget", "detail": str(exc), "clusters": len(todo)}
    except CloudUnavailable as exc:
        return {"status": "skipped_no_key", "detail": str(exc), "clusters": len(todo)}
    except RedactionError as exc:
        return {
            "status": "blocked_redaction",
            "detail": str(exc),
            "clusters": len(todo),
        }
    if res.stop_reason == "max_tokens" and not res.text.strip():
        return {
            "status": "truncated",
            "detail": "the model used all its output tokens before answering",
            "clusters": len(todo),
            "cost_usd": res.cost_usd,
        }
    if res.dry_run:
        return {
            "status": "dry_run",
            "clusters": len(todo),
            "estimated_cost_usd": round(
                client.estimate_cost(
                    "triage", len(json.dumps(payload)), TRIAGE_MAX_TOKENS
                ),
                5,
            ),
        }
    try:
        rows = _loads_json(res.text)["clusters"]
    except (ValueError, KeyError, TypeError):
        return {"status": "bad_reply", "clusters": len(todo), "cost_usd": res.cost_usd}
    by_id = {c["id"]: c for c in todo}
    saved = 0
    for row in rows if isinstance(rows, list) else []:
        c = by_id.get(row.get("id")) if isinstance(row, dict) else None
        if not c:
            continue
        effort = row.get("effort") if row.get("effort") in EFFORTS else None
        store.put_triage(
            c["id"],
            c["members_hash"],
            effort=effort if c["effort_source"] in ("default", "haiku") else None,
            note=str(row.get("note", ""))[:200],
            cause=str(row.get("cause", ""))[:200],
            model=res.model,
        )
        saved += 1
    return {
        "status": "ok",
        "clusters": len(todo),
        "saved": saved,
        "cost_usd": res.cost_usd,
    }


# -- digest ------------------------------------------------------------------------
def _status(issues: list[dict[str, Any]]) -> str:
    active = [i for i in issues if i["state"] in ("open", "acknowledged")]
    if any(
        i["severity"] == "critical" and i["category"] in CRITICAL_CATEGORIES
        for i in active
    ):
        return "critical"
    return "attention" if active else "ok"


def build_digest(
    store: Store, clusters: list[dict[str, Any]], now: float
) -> tuple[str, str, dict[str, Any]]:
    """(status, text of at most 10 lines, structured body)."""
    settings = store.settings()
    issues = store.issues()
    last = store.latest_report()
    since = last["created_at"] if last else 0.0
    active = [i for i in issues if i["state"] in ("open", "acknowledged")]
    sev = {
        s: sum(1 for i in active if i["severity"] == s)
        for s in ("critical", "warning", "info")
    }
    new = sum(1 for i in active if i["first_seen"] > since)
    resolved = sum(
        1 for i in issues if i["state"] == "resolved" and i["last_seen"] > since
    )
    mx = matrix_counts(clusters)
    status = _status(issues)
    tz = ZoneInfo(settings["schedule"]["timezone"])
    day = datetime.fromtimestamp(now, tz).strftime("%a %d %b")
    spend = store.month_spend(now)
    limit = settings["budget"]["monthly_limit_usd"]
    by_id = {c["id"]: c for c in clusters}

    lines = [
        f"Janitor {day}: {status.upper()}",
        f"Open issues {len(active)} (critical {sev['critical']}, "
        f"warning {sev['warning']}, info {sev['info']}) in {len(clusters)} clusters",
        f"Since last report: {new} new, {resolved} resolved",
    ]
    quick = [by_id[i]["label"] for i in mx["quick_wins"][:3]]
    lines.append(
        f"Quick wins: {len(mx['quick_wins'])}"
        + (f" ({', '.join(quick)})" if quick else "")
    )
    lines.append(
        f"Needs you: {len(mx['needs_user'])} clusters · Chores: {len(mx['chores'])}"
    )
    deadlines = sorted({c["deadline"] for c in clusters if c["deadline"]})
    if deadlines:
        lines.append(f"Deadline: breaking changes in HA {', '.join(deadlines[:3])}")
    crit_down = [i for i in active if i["category"] == "critical_entity"]
    lines.append(
        "Critical devices: all OK"
        if not crit_down
        else f"Critical devices DOWN: {len(crit_down)}"
    )
    lines.append(f"Budget: ${spend:.2f} of ${limit:.2f} this month")
    text = "\n".join(lines[:10])
    body = {
        "generated_at": now,
        "status": status,
        "severity_counts": sev,
        "new": new,
        "resolved": resolved,
        "matrix": mx,
        "clusters": [
            {
                k: c[k]
                for k in (
                    "id",
                    "label",
                    "count",
                    "severity",
                    "effort",
                    "kind",
                    "quadrant",
                    "note",
                )
            }
            for c in clusters
        ],
    }
    return status, text, body


def generate_report(
    store: Store,
    client: CloudClient | None = None,
    *,
    now: float | None = None,
    manual: bool = False,
    triage: bool = True,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    client = client or CloudClient(store)
    settings = store.settings()

    def clusters() -> list[dict[str, Any]]:
        return build_clusters(
            store.issues(), settings, store.overrides(), store.triage_notes()
        )

    tri: dict[str, Any] = {"status": "skipped"}
    if triage:
        try:
            tri = run_triage(store, client, clusters(), manual=manual)
        except Exception as exc:  # noqa: BLE001 - the digest must still go out
            logger.warning("Janitor triage failed: %s", exc)
            tri = {"status": "error", "detail": str(exc)[:200]}
    final = clusters()  # re-read: triage may have added notes and ratings
    status, text, body = build_digest(store, final, now)
    body["triage"] = tri
    rid = store.add_report(status, text, body, ts=now)
    from openjarvis.agents.house import write_house_status

    write_house_status(store, now)  # the plain-English file Qwen reads
    return {"id": rid, "status": status, "text": text, "triage": tri}
