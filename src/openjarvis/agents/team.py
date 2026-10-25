"""One view of the whole agent team: who did what, with which tokens and cost.

L0 Python (deterministic report) -> L1 Qwen (read-out job, local) -> L2 Haiku
(triage reasoning) -> L3 Sonnet (proposals, only when escalated) -> L4 Opus (on
demand). Read-only; it only reports what the stores already hold.
"""

from __future__ import annotations

from typing import Any

from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.clusters import build_clusters
from openjarvis.agents.store import Store

RUN_FIELDS = (
    "id",
    "model",
    "started",
    "outcome",
    "purpose",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "dry_run",
)


def _runs(store: Store, layer: str, limit: int = 5) -> list[dict[str, Any]]:
    return [{k: r[k] for k in RUN_FIELDS} for r in store.runs_by_layer(layer, limit)]


def team_overview(store: Store, watchdog: dict[str, Any]) -> dict[str, Any]:
    s = store.settings()
    client = CloudClient(store)
    clusters = build_clusters(
        store.issues(), s, store.overrides(), store.triage_notes()
    )
    labels = {c["id"]: c["label"] for c in clusters}
    members = {c["id"]: c["members_hash"] for c in clusters}
    notes = store.triage_notes()
    proposals = store.plan_proposals()
    report = store.latest_report()
    readout = store.latest_readout()

    reasoning = [
        {
            "cluster_id": cid,
            "label": labels.get(cid, cid),
            "note": n["note"],
            "cause": n["cause"],
            "effort": n["effort"],
            "updated": n["updated"],
            "model": n["model"],
        }
        for cid, n in sorted(notes.items(), key=lambda kv: -kv[1]["updated"])
        if cid in labels
    ][:10]

    escalated = [
        {
            "cluster_id": cid,
            "label": labels.get(cid, cid),
            "risk": p["body"].get("risk"),
            "summary": p["body"].get("summary"),
            "proposal": p["body"],
            "current": p["members_hash"] == members.get(cid),
            "cost_usd": p["cost_usd"],
            "model": p["model"],
            "updated": p["updated"],
        }
        for cid, p in sorted(proposals.items(), key=lambda kv: -kv[1]["updated"])
    ][:10]

    return {
        "dry_run": client.is_dry_run(),
        "l0": {
            "role": "Python watchdog and report (deterministic, no model)",
            "enabled": watchdog.get("enabled"),
            "interval_min": watchdog.get("interval_min"),
            "next_run": watchdog.get("next_run"),
            "last_tick": watchdog.get("last_tick"),
            "issues": watchdog.get("issues"),
            "report": {
                "id": report["id"],
                "created_at": report["created_at"],
                "status": report["status"],
                "text": report["summary_text"],
            }
            if report
            else None,
        },
        "l1": {
            "role": "Qwen read-out job (local, free): reads the report, never analyses",
            "latest": readout,
            "runs": _runs(store, "L1"),
        },
        "l2": {
            "role": "Haiku triage: note, suspected cause and effort per cluster",
            "model": client.model_for("triage"),
            "mode": "dry-run (no cloud call)"
            if client.is_dry_run()
            else "live, once per report",
            "runs": _runs(store, "triage"),
            "reasoning": reasoning,
        },
        "l3": {
            "role": "Sonnet: proposals and risk checks, only when you escalate a task",
            "model": client.model_for("diagnose"),
            "auto_escalation": s["auto_escalation"],
            "escalated": escalated,
            "runs": _runs(store, "diagnose"),
        },
        "l4": {
            "role": "Opus: deep analysis, only on demand",
            "model": client.model_for("deep"),
            "runs": _runs(store, "deep"),
        },
        "month": [
            {
                "model": r["model"],
                "calls": r["calls"],
                "tokens_in": r["input_tokens"]
                + r["cache_read_tokens"]
                + r["cache_write_tokens"],
                "tokens_out": r["output_tokens"],
                "cost_usd": round(r["cost_usd"], 4),
            }
            for r in store.month_breakdown()
        ],
    }
