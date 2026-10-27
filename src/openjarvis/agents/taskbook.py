"""One readable file with every task: issues, your findings, the analysis, the plan.

``~/.openjarvis/janitor/tasks.md`` is an export, rewritten whenever you save
findings, a proposal is stored or a task is ticked. The database stays the source
of truth; edits made in the file are not read back (use "My findings" in the UI).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openjarvis.agents.planning import build_plan
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store
from openjarvis.agents.watchdog import janitor_dir

FILE_NAME = "tasks.md"


def taskbook_path() -> Path:
    return janitor_dir() / FILE_NAME


def _bullets(items: list[Any] | None) -> list[str]:
    return [f"- {x}" for x in (items or [])]


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
    lines = [
        "# Jarvis the Janitor: task book",
        "",
        f"Updated {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')} · "
        f"{len(plan['tasks'])} tasks · export of the Janitor database, rewritten "
        "when findings, proposals or ticks change. Edit findings in the Plan tab.",
        "",
    ]
    for t in plan["tasks"]:
        lines += [
            f"## {t['label']}  (`{t['id']}`)",
            f"{t['severity']} · effort {t['effort']} ({t['effort_source']}) · "
            f"{t['quadrant']} · {t['count']} issue(s)"
            + (" · ticked" if t["selected"] else "")
            + (f" · breaks in {t['deadline']}" if t["deadline"] else ""),
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
            lines += [
                "_A proposal exists but the issues changed since; refresh it._",
                "",
            ]
        for e in runs.get(t["id"], [])[:3]:
            lines.append(
                f"- Execution #{e['id']}: {e['status']} "
                f"({'live' if e['live'] else 'simulation'})"
            )
        if runs.get(t["id"]):
            lines.append("")
    return Redactor.redact_secrets("\n".join(lines))


def write_taskbook(store: Store, now: float | None = None) -> Path:
    path = taskbook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_taskbook(store, now), encoding="utf-8")
    return path
