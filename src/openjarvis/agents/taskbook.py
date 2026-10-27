"""Two readable files: ``tasks.md`` (open work) and ``resolved.md`` (the library).

Both are exports of the Janitor database, rewritten when findings, proposals,
ticks or closings change. Edit findings and solutions in the Plan tab; changes
made inside the files are not read back.

Task ids are ``year_month_day_sequence`` (for example ``2026_10_27_001``): the
day the task first appeared, then a 3-digit counter for that day. A problem that
comes back after its task was closed gets a new id tagged ``seen before: <id>``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openjarvis.agents.planning import build_plan
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store

TASKS_FILE = "tasks.md"
RESOLVED_FILE = "resolved.md"


def book_dir() -> Path:
    """A visible folder (not the hidden ~/.openjarvis), so the files are easy to find."""
    env = os.environ.get("JARVIS_TASKBOOK_DIR") or os.environ.get("JARVIS_JANITOR_DIR")
    return Path(env or "~/OpenJarvis/jarvis").expanduser()


def taskbook_path() -> Path:
    return book_dir() / TASKS_FILE


def resolved_path() -> Path:
    return book_dir() / RESOLVED_FILE


def _bullets(items: list[Any] | None) -> list[str]:
    return [f"- {x}" for x in (items or [])]


def _stamp(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _tags(*tags: str | None) -> str:
    return " · ".join(t for t in tags if t)


def _proposal_lines(p: dict[str, Any]) -> list[str]:
    mode = {"quick": "Proposed", "investigate": "Investigated", "deep": "Deep (Opus)"}
    out = [
        f"**{mode.get(p.get('mode', 'quick'), 'Proposed')}** · risk {p.get('risk')}"
        + (" · needs you" if p.get("needs_user") else ""),
        "",
        str(p.get("summary", "")),
    ]
    if p.get("findings_used"):
        out += ["", f"_Based on your findings:_ {p['findings_used']}"]
    if p.get("diagnosis"):
        out += ["", "**Analysis**", str(p["diagnosis"])]
    if p.get("breaking_change_check"):
        out += ["", "**Check before changing**", str(p["breaking_change_check"])]
    ins = p.get("instruction") or {}
    if ins.get("steps"):
        out += ["", "**Steps the executor would run**"]
        for i, s in enumerate(ins["steps"]):
            out.append(f"{i}. `{s['tool']}` {s.get('purpose', '')}")
    elif p.get("steps"):
        out += ["", "**Steps**", *_bullets(p["steps"])]
    if p.get("user_action"):
        out += ["", "**For you to do**", str(p["user_action"])]
    if p.get("verification"):
        out += ["", "**Verify**", *_bullets(p["verification"])]
    return out


def build_taskbook(store: Store, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    plan = build_plan(store)
    runs: dict[str, list[dict[str, Any]]] = {}
    for e in store.executions(200):
        runs.setdefault(e["cluster_id"], []).append(e)
    tasks = plan["tasks"]
    lines = [
        "# Jarvis the Janitor: tasks",
        "",
        f"Updated {_stamp(now)} · {len(tasks)} open · {len(plan['cleared'])} cleared "
        "(need a solution text) · export of the Janitor database. Edit findings and "
        "solutions in the Plan tab. Closed tasks live in resolved.md.",
        "",
        "| ID | Task | Severity | Effort | Status |",
        "|---|---|---|---|---|",
    ]
    for t in tasks:
        status = ("ticked" if t["selected"] else "open") + (
            f" · {t['proposal'].get('mode', 'quick')}" if t["proposal"] else ""
        )
        lines.append(
            f"| {t['task_id']} | {t['label']} | {t['severity']} | {t['effort']} "
            f"| {status} |"
        )
    for c in plan["cleared"]:
        lines.append(
            f"| {c['task_id']} | {c['label']} | - | - | cleared, needs solution |"
        )
    lines.append("")
    for t in tasks:
        lines += [
            f"## {t['task_id']}",
            f"**{t['label']}**",
            "",
            "Tags: "
            + _tags(
                t["id"],
                f"seen before: {t['seen_before']}" if t["seen_before"] else None,
                t["severity"],
                f"effort {t['effort']} ({t['effort_source']})",
                t["quadrant"],
                f"{t['count']} issue(s)",
                f"breaks in {t['deadline']}" if t["deadline"] else None,
            ),
            "",
        ]
        if t["note"] or t["cause"]:
            lines += [
                f"Triage (Haiku): {t['note'] or ''} Likely cause: {t['cause'] or '-'}",
                "",
            ]
        lines += ["Issues: " + " · ".join(t["examples"]), ""]
        lines += ["### My findings", t["findings"] or "_none yet_", ""]
        if t["proposal"]:
            lines += ["### Proposal / analysis", *_proposal_lines(t["proposal"]), ""]
        elif t["proposal_stale"]:
            lines += ["_A proposal exists but the issues changed; refresh it._", ""]
        if t["solution"]:
            lines += ["### Closing text (draft)", t["solution"], ""]
        for e in runs.get(t["id"], [])[:3]:
            lines.append(
                f"- Execution #{e['id']}: {e['status']} "
                f"({'live' if e['live'] else 'simulation'})"
            )
        if runs.get(t["id"]):
            lines.append("")
    for c in plan["cleared"]:
        lines += [
            f"## {c['task_id']}",
            f"**{c['label']}**",
            "",
            f"Tags: {c['cluster_id']} · cleared (issues gone in Home Assistant)",
            "",
            "### My findings",
            c["findings"] or "_none_",
            "",
            "### Closing text (draft)",
            c["solution"] or c["solution_draft"] or "_write the solution to close it_",
            "",
        ]
    return Redactor.redact_secrets("\n".join(lines))


def build_record(store: Store, task_id: str) -> dict[str, Any]:
    """Everything worth keeping about a task, frozen at the moment it is closed."""
    row = store.task(task_id) or {}
    meta = json.loads(row.get("meta") or "{}")
    cid = row.get("cluster_id", "")
    prop = store.plan_proposals().get(cid)
    body = prop["body"] if prop else {}
    runs = [e for e in store.executions(200) if e["cluster_id"] == cid]
    return {
        "label": row.get("label"),
        "cluster_id": cid,
        "severity": meta.get("severity"),
        "examples": meta.get("examples") or [],
        "cause": meta.get("cause"),
        "findings": store.findings().get(cid, ""),
        "analysis": body.get("diagnosis") or body.get("summary") or "",
        "steps": [
            s.get("purpose") or s["tool"]
            for s in (body.get("instruction") or {}).get("steps", [])
        ]
        or [str(x) for x in body.get("steps", [])],
        "executions": [
            {"id": e["id"], "status": e["status"], "live": bool(e["live"])}
            for e in runs[:5]
        ],
    }


def close_task(store: Store, task_id: str, now: float | None = None) -> dict[str, Any]:
    """Close a task. Refuses while there is no solution text."""
    row = store.task(task_id)
    if row is None or row["status"] == "closed":
        return {"status": "unknown_task"}
    if not row["solution"].strip():
        return {"status": "needs_solution"}
    store.close_task(task_id, build_record(store, task_id), now)
    return {"status": "ok", "task_id": task_id}


def build_resolved(store: Store, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    rows = store.closed_tasks()
    lines = [
        "# Jarvis the Janitor: resolved (knowledge library)",
        "",
        f"Updated {_stamp(now)} · {len(rows)} resolved. Newest last. A problem that "
        "comes back is a new task tagged 'seen before'.",
        "",
    ]
    for r in rows:
        rec = json.loads(r["record"] or "{}")
        lines += [
            f"## {r['id']}",
            f"**{r['label']}**",
            "",
            "Tags: "
            + _tags(
                r["cluster_id"],
                f"seen before: {r['seen_before']}" if r["seen_before"] else None,
                rec.get("severity"),
            ),
            f"Opened {_stamp(r['opened_at'])} · closed {_stamp(r['closed_at'])}",
            "",
            "**Problem:** " + " · ".join(rec.get("examples") or []),
        ]
        if rec.get("cause"):
            lines.append(f"**Suspected cause (triage):** {rec['cause']}")
        if rec.get("findings"):
            lines.append(f"**Owner finding:** {rec['findings']}")
        if rec.get("analysis"):
            lines.append(f"**Analysis:** {rec['analysis']}")
        lines += ["", "**Solution:**", r["solution"], ""]
        if rec.get("steps"):
            lines += ["**Steps:**", *_bullets(rec["steps"]), ""]
        if rec.get("executions"):
            lines.append(
                "**Executions:** "
                + ", ".join(
                    f"#{e['id']} {e['status']}{' (live)' if e['live'] else ''}"
                    for e in rec["executions"]
                )
            )
            lines.append("")
    return Redactor.redact_secrets("\n".join(lines))


def write_taskbook(store: Store, now: float | None = None) -> Path:
    path = taskbook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_taskbook(store, now), encoding="utf-8")
    resolved_path().write_text(build_resolved(store, now), encoding="utf-8")
    return path
