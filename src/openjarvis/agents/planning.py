"""Overview totals, the plan (tasks, estimates) and on-demand proposals.

* ``overview`` feeds the main-page cards (totals only) and the Maintenance
  dashboard sections (top 10 issues each, collapsed in the UI)
* ``build_plan`` turns clusters into visible tasks ordered quick wins first, with
  the user's selection, any proposal already requested, and token / dollar
  estimates. Selecting a task is a decision about priority, not an approval to
  change anything: applying changes is a later phase and needs Telegram approval
* ``generate_proposal`` is only ever called when the user asks for one task
"""

from __future__ import annotations

import json
import time
from typing import Any

from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudUnavailable,
    cost_usd,
)
from openjarvis.agents.clusters import build_clusters, matrix_counts
from openjarvis.agents.escalation import ReadOnlyHA, escalate, estimate_investigation
from openjarvis.agents.redaction import RedactionError
from openjarvis.agents.reporting import TRIAGE_MAX_TOKENS, _loads_json
from openjarvis.agents.store import RISKS, Store

SECTION_LIMIT = 10
CHARS_PER_TOKEN = 3.5
PROPOSAL_MAX_TOKENS = 3000
SYSTEM_TOKENS = 900
CONTEXT_TOKENS = 2500  # allowance for what Sonnet is told about the cluster
HARD_FACTOR = 1.6
LOCAL_READOUT_TOKENS = 450  # Qwen reading the digest aloud: local, free

LANES = ("quick_win", "scheduled", "major_project", "defer", "fill_in")
SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

ASSUMPTIONS = [
    "Proposal = one Sonnet call per task, without live Home Assistant lookups.",
    "Input = system prompt + task payload + a context allowance "
    f"({CONTEXT_TOKENS} tokens, x{HARD_FACTOR} for hard tasks); output = full "
    f"{PROPOSAL_MAX_TOKENS} tokens. Prices from settings.prices, no caching credit.",
    "Phase 4 adds read-only lookups (roughly +6,000 to +10,000 input tokens per task).",
    "Qwen (local) only reads reports aloud: free, not part of the dollar estimate.",
]

PROPOSAL_SYSTEM = (
    "You are a careful Home Assistant maintenance planner. You get one cluster of "
    "related findings. You never run anything: you only write a proposal for a "
    "human to decide on. Return JSON only with keys: summary (string), steps "
    "(list of short strings), risk (low|medium|high), breaking_change_check "
    "(what must be verified before the change, e.g. release notes or deprecations), "
    "rollback (string), verification (list of strings), needs_user (true when a "
    "human must act in the physical world or outside Home Assistant), user_action "
    "(string, empty when not needed). Be concrete and short. Placeholders like "
    "<email_1> are redacted values: keep them as they are."
)


def _meta(issue: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(issue.get("meta") or "{}")
    except (TypeError, ValueError):
        return {}


def _active(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i["state"] in ("open", "acknowledged")]


def _item(i: dict[str, Any]) -> dict[str, Any]:
    meta = _meta(i)
    return {
        "fingerprint": i["fingerprint"],
        "title": i["title"],
        "severity": i["severity"],
        "state": i["state"],
        "category": i["category"],
        "first_seen": i["first_seen"],
        "detail": meta.get("example") or "",
    }


def _top(issues: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        issues, key=lambda i: (-SEV_RANK.get(i["severity"], 0), -i["last_seen"])
    )
    return {"total": len(ranked), "items": [_item(i) for i in ranked[:SECTION_LIMIT]]}


# -- overview ------------------------------------------------------------------
def overview(store: Store, now: float | None = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    s = store.settings()
    facts = store.facts()
    active = _active(store.issues())

    def where(pred: Any) -> list[dict[str, Any]]:
        return [i for i in active if pred(i)]

    backup = where(
        lambda i: (
            i["category"] == "backup"
            or (i["category"] == "repair" and _meta(i).get("domain") == "backup")
        )
    )
    integrations = where(lambda i: i["category"] == "integration")
    automations = where(
        lambda i: i["category"] == "repair" and _meta(i).get("domain") == "automation"
    )
    deprecations = where(lambda i: bool(_meta(i).get("breaks_in")))
    logs = where(lambda i: i["category"] == "log" and i["severity"] == "critical")
    versions = sorted({str(_meta(i)["breaks_in"]) for i in deprecations})

    last = facts.get("backup_last_success")
    age_h = None
    if isinstance(last, str):
        from openjarvis.agents.watchdog import parse_ts

        ts = parse_ts(last)
        age_h = round((now - ts) / 3600, 1) if ts else None
    limit_h = s["thresholds"]["backup_age_hours"]
    updates = facts.get("updates_pending")
    crit_lines = facts.get("log_critical_lines")

    days = int(s["thresholds"].get("stale_unavailable_days", 30))
    stale = store.unavailable_longer_than(days, now)
    boots = store.boots(SECTION_LIMIT)
    since_h = round((now - boots[0]["boot_at"]) / 3600, 1) if boots else None

    mb = facts.get("motion_batteries")
    warn_pct = s["thresholds"].get("battery_warn_pct", 50)

    def mb_level(r: dict[str, Any]) -> str:
        """red = unknown or offline, orange = low or below the warn limit."""
        if r.get("state") in ("unavailable", "unknown") or (
            r.get("battery") is None and r.get("battery_offline")
        ):
            return "red"
        if r.get("low_flag") or (
            r.get("battery") is not None and r["battery"] < warn_pct
        ):
            return "orange"
        return "ok" if r.get("battery") is not None else "none"

    order = {"red": 0, "orange": 1, "ok": 2, "none": 3}
    mb_rows = sorted(
        mb or [],
        key=lambda r: (
            order[mb_level(r)],
            r["battery"] if r.get("battery") is not None else 101,
            r["name"],
        ),
    )
    mb_red = sum(1 for r in mb_rows if mb_level(r) == "red")
    mb_orange = sum(1 for r in mb_rows if mb_level(r) == "orange")

    cards = {
        "motion_batteries": {
            # red (unknown or offline) prevails; otherwise the orange ones
            "value": None if mb is None else mb_red if mb_red else mb_orange,
            "kind": "idle"
            if mb is None
            else "red"
            if mb_red
            else "orange"
            if mb_orange
            else "ok",
            "red": mb_red,
            "orange": mb_orange,
            "warn_pct": warn_pct,
            "total": len(mb_rows),
            "with_battery": sum(1 for r in mb_rows if r.get("has_battery")),
            "tone": "idle"
            if mb is None
            else "bad"
            if mb_red
            else "warn"
            if mb_orange
            else "ok",
        },
        "stale_unavailable": {
            "value": stale["count"],
            "days": days,
            "tone": "warn" if stale["count"] else "ok",
        },
        "log_levels": {
            "value": sum(
                x["critical"] + x["error"] + x["warning"]
                for x in facts.get("log_levels") or []
            ),
            "tone": "bad"
            if crit_lines
            else "warn"
            if facts.get("log_levels")
            else "ok",
        },
        "last_restart": {
            "value": since_h,
            "unit": "h ago",
            "tone": "idle" if since_h is None else "warn" if since_h < 24 else "ok",
        },
        "backup_failed": {"value": len(backup), "tone": "bad" if backup else "ok"},
        "last_backup": {
            "value": age_h,
            "unit": "h ago",
            "at": last,
            "limit_hours": limit_h,
            "tone": "idle" if age_h is None else ("bad" if age_h > limit_h else "ok"),
        },
        "integrations_failed": {
            "value": len(integrations),
            "tone": "bad"
            if len(integrations) >= 5
            else "warn"
            if integrations
            else "ok",
        },
        "automation_errors": {
            "value": len(automations),
            "tone": "bad" if len(automations) >= 5 else "warn" if automations else "ok",
        },
        "updates_pending": {
            "value": updates,
            "tone": "idle" if updates is None else "warn" if updates else "ok",
        },
        "deprecations": {
            "value": len(deprecations),
            "next_version": versions[0] if versions else None,
            "tone": "warn" if deprecations else "ok",
        },
        "log_critical": {
            "value": crit_lines,
            "tone": "idle" if crit_lines is None else "bad" if crit_lines else "ok",
        },
    }
    counting = stale["counting_since"]
    sections = {
        "motion_batteries": {
            "total": len(mb_rows),
            "items": [
                {
                    "title": r["name"] + (f" ({r['area']})" if r.get("area") else ""),
                    "severity": {"red": "critical", "orange": "warning"}.get(
                        mb_level(r), "info"
                    ),
                    "detail": (
                        f"battery {r['battery']:g}%"
                        if r.get("battery") is not None
                        else "battery low"
                        if r.get("low_flag")
                        else "battery unknown (its sensor is offline)"
                        if r.get("battery_offline")
                        else "no battery sensor (mains powered or a camera)"
                    )
                    + (
                        " · sensor offline"
                        if r.get("state") in ("unavailable", "unknown")
                        else ""
                    ),
                }
                for r in mb_rows
            ],
        },
        "log_levels": {
            "total": len(facts.get("log_levels") or []),
            "items": [
                {
                    "title": f"{x['component']}: {x['critical']} critical, "
                    f"{x['error']} errors, {x['warning']} warnings",
                    "severity": "critical"
                    if x["critical"]
                    else "warning"
                    if x["error"]
                    else "info",
                    "detail": x.get("example", ""),
                }
                for x in (facts.get("log_levels") or [])[:SECTION_LIMIT]
            ],
        },
        "stale_unavailable": {
            "total": stale["count"],
            "items": [
                {
                    "title": r["name"] or r["entity_id"],
                    "detail": f"{r['entity_id']} · unavailable about "
                    f"{int((now - r['since']) // 86400)} days",
                    "severity": "info",
                }
                for r in stale["items"][:SECTION_LIMIT]
            ],
            "counting_since": counting,
            "reliable_from": counting + days * 86400 if counting else None,
            "tracked": stale["tracked"],
        },
        "last_restart": {
            "total": len(boots),
            "items": [
                {
                    "title": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(b["boot_at"])
                    ),
                    "detail": "preceded by an unreachable check (possible crash)"
                    if b["after_outage"]
                    else "restart seen by the watchdog",
                    "severity": "warning" if b["after_outage"] else "info",
                    "first_seen": b["boot_at"],
                }
                for b in boots
            ],
        },
        "backup_failed": _top(backup),
        "integrations_failed": _top(integrations),
        "automation_errors": _top(automations),
        "deprecations": _top(deprecations),
        "log_critical": _top(logs),
        "updates_pending": {
            "total": updates or 0,
            "items": [
                {"title": u.get("name") or u.get("entity_id", ""), "severity": "info"}
                for u in (facts.get("updates_list") or [])[:SECTION_LIMIT]
            ],
        },
    }
    return {
        "cards": cards,
        "sections": sections,
        "checked_at": facts.get("checked_at"),
        "ha_version": facts.get("ha_version"),
    }


# -- estimates -------------------------------------------------------------------
def estimate_task(
    prices: dict[str, Any] | None, cluster: dict[str, Any]
) -> dict[str, Any]:
    payload_chars = len(
        json.dumps({k: cluster[k] for k in ("id", "label", "examples")})
    )
    factor = HARD_FACTOR if cluster["effort"] == "hard" else 1.0
    tokens_in = int(
        SYSTEM_TOKENS + payload_chars / CHARS_PER_TOKEN + CONTEXT_TOKENS * factor
    )
    usage = {"input_tokens": tokens_in, "output_tokens": PROPOSAL_MAX_TOKENS}
    return {
        "tokens_in": tokens_in,
        "tokens_out": PROPOSAL_MAX_TOKENS,
        "usd": round(cost_usd(prices, usage), 4) if prices else None,
    }


def investigate_estimate(
    prices: dict[str, Any] | None, cluster: dict[str, Any], rounds: int
) -> dict[str, Any]:
    base = estimate_task(prices, cluster)["tokens_in"]
    return estimate_investigation(prices, base, rounds)


def _sum(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usd = [r["usd"] for r in rows]
    return {
        "tasks": len(rows),
        "tokens_in": sum(r["tokens_in"] for r in rows),
        "tokens_out": sum(r["tokens_out"] for r in rows),
        "usd": None if any(u is None for u in usd) else round(sum(usd), 4),
    }


# -- the plan -------------------------------------------------------------------
def plan_clusters(store: Store) -> list[dict[str, Any]]:
    return build_clusters(
        store.issues(), store.settings(), store.overrides(), store.triage_notes()
    )


def build_plan(store: Store, client: CloudClient | None = None) -> dict[str, Any]:
    client = client or CloudClient(store)
    settings = store.settings()
    clusters = plan_clusters(store)
    selected = store.selected_tasks()
    proposals = store.plan_proposals()
    model = client.model_for("diagnose")
    prices = settings["prices"].get(model)

    tasks: list[dict[str, Any]] = []
    for c in clusters:
        prop = proposals.get(c["id"])
        current = bool(prop and prop["members_hash"] == c["members_hash"])
        tasks.append(
            {
                **{
                    k: c[k]
                    for k in (
                        "id",
                        "label",
                        "severity",
                        "effort",
                        "effort_source",
                        "kind",
                        "needs_user",
                        "quadrant",
                        "count",
                        "deadline",
                        "note",
                        "cause",
                        "examples",
                    )
                },
                "selected": c["id"] in selected,
                "proposal": prop["body"] if current else None,
                "proposal_stale": bool(prop) and not current,
                "estimate": estimate_task(prices, c),
                "estimate_investigate": investigate_estimate(
                    prices, c, settings["escalation"]["max_rounds"]
                ),
                "estimate_deep": investigate_estimate(
                    settings["prices"].get(client.model_for("deep")),
                    c,
                    settings["escalation"]["max_rounds"],
                ),
            }
        )

    lanes = {lane: [t["id"] for t in tasks if t["quadrant"] == lane] for lane in LANES}
    pending = [t["estimate"] for t in tasks if not t["proposal"]]
    chosen = [t["estimate"] for t in tasks if t["selected"] and not t["proposal"]]
    triage_todo = [c for c in clusters if not c["triage_current"]]
    triage_chars = sum(
        len(json.dumps({"id": c["id"], "x": c["examples"]})) for c in triage_todo
    )
    spend = client.store.month_spend()
    limit = settings["budget"]["monthly_limit_usd"]
    return {
        "tasks": tasks,
        "lanes": lanes,
        "matrix": matrix_counts(clusters),
        "estimates": {
            "model": model,
            "proposals_all_pending": _sum(pending),
            "proposals_selected_pending": _sum(chosen),
            "triage_next_run": {
                "model": client.model_for("triage"),
                "clusters": len(triage_todo),
                "usd": round(
                    client.estimate_cost("triage", triage_chars, TRIAGE_MAX_TOKENS), 4
                )
                if triage_todo
                else 0.0,
            },
            "local_readout_tokens_per_digest": LOCAL_READOUT_TOKENS,
            "budget": {
                "limit_usd": limit,
                "spent_usd": round(spend, 4),
                "remaining_usd": round(max(limit - spend, 0), 4),
            },
            "assumptions": ASSUMPTIONS,
        },
        "dry_run": client.is_dry_run(),
    }


def select_task(store: Store, cluster_id: str, selected: bool) -> None:
    store.set_selected(cluster_id, selected)


# -- proposals (on request only) ---
def generate_proposal(
    store: Store,
    client: CloudClient,
    cluster_id: str,
    mode: str = "quick",
    ha: ReadOnlyHA | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    """``quick`` = one Sonnet call; ``investigate`` = Sonnet with read-only
    lookups; ``deep`` = the same with Opus. All three are started by the user."""
    cluster = next((c for c in plan_clusters(store) if c["id"] == cluster_id), None)
    if cluster is None:
        return {"status": "unknown_task"}
    settings = store.settings()
    prices = settings["prices"].get(client.model_for("diagnose"))
    estimate = estimate_task(prices, cluster)
    members = [i for i in store.issues() if i["fingerprint"] in cluster["fingerprints"]]
    payload = {
        "id": cluster["id"],
        "label": cluster["label"],
        "severity": cluster["severity"],
        "effort": cluster["effort"],
        "kind": cluster["kind"],
        "deadline": cluster["deadline"],
        "note": cluster["note"],
        "suspected_cause": cluster["cause"],
        "issues": [
            {
                "title": m["title"],
                "source": m["source_id"],
                "severity": m["severity"],
                "category": m["category"],
            }
            for m in sorted(
                members,
                key=lambda m: not (focus and focus.lower() in m["source_id"].lower()),
            )[:15]
        ],
    }
    if focus:
        payload["focus"] = focus.strip()[:300]
    if mode not in ("quick", "investigate", "deep"):
        return {"status": "bad_mode"}
    if mode != "quick":
        tier = "deep" if mode == "deep" else "diagnose"
        out = escalate(store, client, payload, tier=tier, ha=ha)
        if out["status"] == "dry_run":
            prices_t = settings["prices"].get(client.model_for(tier))
            return {
                "status": "dry_run",
                "estimate": investigate_estimate(
                    prices_t, cluster, settings["escalation"]["max_rounds"]
                ),
            }
        if out["status"] != "ok":
            return {**out, "estimate": estimate}
        body = {**out["proposal"], "mode": mode, "tool_calls": out["tool_calls"]}
        if focus:
            body["focus"] = focus.strip()[:300]
        store.put_plan_proposal(
            cluster_id,
            cluster["members_hash"],
            body,
            model=out["model"],
            cost_usd=out["spent_usd"],
        )
        return {
            "status": "ok",
            "proposal": body,
            "cost_usd": out["spent_usd"],
            "tool_calls": out["tool_calls"],
        }
    try:
        res = client.complete(
            "diagnose",
            system=PROPOSAL_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
            max_tokens=PROPOSAL_MAX_TOKENS,
            manual=True,
            purpose=f"proposal:{cluster_id}",
        )
    except BudgetExceeded as exc:
        return {"status": "skipped_budget", "detail": str(exc), "estimate": estimate}
    except CloudUnavailable as exc:
        return {"status": "skipped_no_key", "detail": str(exc), "estimate": estimate}
    except RedactionError as exc:
        return {"status": "blocked_redaction", "detail": str(exc), "estimate": estimate}
    if res.dry_run:
        return {"status": "dry_run", "estimate": estimate}
    try:
        body = _loads_json(res.text)
        risk = body["risk"]
        if risk not in RISKS:
            raise ValueError("risk")
        clean = {
            "summary": str(body.get("summary", ""))[:2000],
            "steps": [str(x)[:600] for x in body.get("steps", [])][:12],
            "risk": risk,
            "breaking_change_check": str(body.get("breaking_change_check", ""))[:3000],
            "rollback": str(body.get("rollback", ""))[:2000],
            "verification": [str(x)[:200] for x in body.get("verification", [])][:8],
            "needs_user": bool(body.get("needs_user", False)),
            "user_action": str(body.get("user_action", ""))[:3000],
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"status": "bad_reply", "cost_usd": res.cost_usd, "estimate": estimate}
    store.put_plan_proposal(
        cluster_id,
        cluster["members_hash"],
        clean,
        model=res.model,
        cost_usd=res.cost_usd,
    )
    return {"status": "ok", "proposal": clean, "cost_usd": res.cost_usd}
