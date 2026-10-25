"""Clusters and the severity x effort matrix (pure functions, no I/O).

Issues are grouped by root cause so that one fix can close many of them:

* ``repair`` / ``integration`` / ``log`` -> one cluster per integration domain
* ``entity_group`` -> one per entity domain, ``motion`` / ``critical_entity`` /
  ``battery`` / ``backup`` -> one each

Effort comes from, in this order: the user's override, the rule in
``settings.matrix.effort_defaults`` (``auto`` = rated by Haiku, see ``triage``),
the Haiku rating, then ``default_effort``. Nothing here applies a change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from openjarvis.agents.store import EFFORTS

SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
SEVERITIES = ("critical", "warning", "info")
INTEGRATION_CATEGORIES = ("repair", "integration", "log")

FIXED_CLUSTERS = {
    "motion": ("devices:motion", "Motion sensors"),
    "critical_entity": ("devices:critical", "Critical devices"),
    "battery": ("chore:batteries", "Batteries"),
    "backup": ("backup", "Backups"),
}


def _meta(issue: dict[str, Any]) -> dict[str, Any]:
    raw = issue.get("meta")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def cluster_of(issue: dict[str, Any]) -> tuple[str, str]:
    """(cluster id, label) for one issue."""
    cat, meta = issue["category"], _meta(issue)
    if cat in INTEGRATION_CATEGORIES:
        dom = str(meta.get("domain") or meta.get("component") or "unknown")
        return f"integration:{dom}", f"Integration: {dom}"
    if cat == "entity_group":
        dom = str(meta.get("domain") or issue.get("source_id") or "unknown")
        return f"entities:{dom}", f"Unavailable {dom} entities"
    if cat in FIXED_CLUSTERS:
        return FIXED_CLUSTERS[cat]
    return f"other:{cat}", f"Other: {cat}"


def members_hash(members: list[dict[str, Any]]) -> str:
    """Changes when the set of issues or their severities change (re-triage cue)."""
    raw = "|".join(sorted(f"{m['fingerprint']}:{m['severity']}" for m in members))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324


def quadrant(severity: str, effort: str) -> str:
    if effort == "quick":
        return "quick_win" if severity != "info" else "fill_in"
    if effort == "medium":
        return "scheduled"
    return "major_project" if severity == "critical" else "defer"


def build_clusters(
    issues: list[dict[str, Any]],
    settings: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None = None,
    notes: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Active issues (open / acknowledged) -> prioritised clusters."""
    matrix = settings["matrix"]
    overrides, notes = overrides or {}, notes or {}
    groups: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue["state"] not in ("open", "acknowledged"):
            continue
        cid, label = cluster_of(issue)
        g = groups.setdefault(
            cid, {"id": cid, "label": label, "members": [], "categories": set()}
        )
        g["members"].append(issue)
        g["categories"].add(issue["category"])

    out: list[dict[str, Any]] = []
    for cid, g in groups.items():
        members = g["members"]
        severity = max((m["severity"] for m in members), key=lambda s: SEV_RANK[s])
        cats = sorted(g["categories"])
        ov = overrides.get(cid) or {}
        note = notes.get(cid) or {}
        mh = members_hash(members)

        rule = None
        for c in cats:
            r = matrix["effort_defaults"].get(c, "auto")
            if r != "auto":
                rule = r if rule is None else max((rule, r), key=EFFORTS.index)
        if ov.get("effort"):
            effort, source = ov["effort"], "override"
        elif rule:
            effort, source = rule, "rule"
        elif note.get("effort") in EFFORTS:
            effort, source = note["effort"], "haiku"
        else:
            effort, source = matrix["default_effort"], "default"

        kind = ov.get("kind") or (
            "chore" if all(c in matrix["chore_categories"] for c in cats) else "change"
        )
        breaks = sorted(
            {
                str(_meta(m).get("breaks_in"))
                for m in members
                if _meta(m).get("breaks_in")
            }
        )
        priority = (
            (1000 if breaks else 0)
            + SEV_RANK[severity] * 100
            + (len(EFFORTS) - 1 - EFFORTS.index(effort)) * 10
            + min(len(members), 9)
        )
        out.append(
            {
                "id": cid,
                "label": g["label"],
                "categories": cats,
                "count": len(members),
                "severity": severity,
                "effort": effort,
                "effort_source": source,
                "kind": kind,
                "needs_user": kind == "change",
                "deadline": breaks[0] if breaks else None,
                "quadrant": quadrant(severity, effort),
                "priority": priority,
                "members_hash": mh,
                "triage_current": note.get("members_hash") == mh,
                "note": note.get("note") if note.get("members_hash") == mh else None,
                "cause": note.get("cause") if note.get("members_hash") == mh else None,
                "fingerprints": [m["fingerprint"] for m in members],
                "examples": [m["title"] for m in members[:3]],
            }
        )
    out.sort(key=lambda c: (-c["priority"], c["id"]))
    return out


def matrix_counts(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    """Cluster and issue counts per (severity, effort) cell, plus the quick wins."""
    cells = {s: {e: {"clusters": 0, "issues": 0} for e in EFFORTS} for s in SEVERITIES}
    for c in clusters:
        cell = cells[c["severity"]][c["effort"]]
        cell["clusters"] += 1
        cell["issues"] += c["count"]
    return {
        "cells": cells,
        "quick_wins": [c["id"] for c in clusters if c["quadrant"] == "quick_win"],
        "chores": [c["id"] for c in clusters if c["kind"] == "chore"],
        "needs_user": [c["id"] for c in clusters if c["needs_user"]],
    }
