"""Lean files for Qwen: a short index, then one small detail file per topic.

Qwen reads things, it does not reason over the Janitor's history. So it never gets
the findings, proposals or logs. ``qwen-index.md`` is a few lines (topic, keywords,
file, one-line summary). Python matches the question to the topics through the
keywords and hands Qwen only the matching detail file(s). All files are written by
Python from real data; short problem notes come from Haiku (triage), never from Qwen.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from openjarvis.agents.house import build_house_status, plain_label
from openjarvis.agents.planning import overview, plan_clusters, sync_open_tasks
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store

logger = logging.getLogger(__name__)

INDEX_FILE = "qwen-index.md"
MAX_PICK = 2
LIST_LIMIT = 15
STALE_LIMIT = 5

# topic -> (file, title, keywords). The keywords are what a question is matched to;
# add words here when a question does not find its topic.
TOPICS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "open-tasks": (
        "qwen-open-tasks.md",
        "Open tasks",
        (
            "open task",
            "open tasks",
            "open issue",
            "open issues",
            "issue",
            "issues",
            "task",
            "tasks",
            "problem",
            "problems",
            "what is wrong",
            "what's wrong",
            "broken",
            "not working",
            "failing",
            "failed",
            "errors",
            "to fix",
            "to do",
            "todo",
            "needs attention",
            "alerts",
            "warnings",
            "maintenance",
            "backup",
            "integration",
            "integrations",
            "automation",
            "automations",
            "repair",
            "repairs",
            "deprecated",
            "deprecation",
            "severity",
            "serious",
            "urgent",
            "what should i fix",
            "fix first",
        ),
    ),
    "solutions": (
        "qwen-solutions.md",
        "Solutions found",
        (
            "solution",
            "solutions",
            "solved",
            "resolved",
            "fixed",
            "closed",
            "how did we fix",
            "how was it fixed",
            "seen before",
            "happened before",
            "knowledge",
            "library",
            "history",
            "earlier",
            "previous",
            "what worked",
        ),
    ),
    "projects": (
        "qwen-projects.md",
        "Projects",
        (
            "project",
            "projects",
            "backlog",
            "ideas",
            "idea",
            "planned",
            "in progress",
            "roadmap",
            "working on",
            "on hold",
            "blocked",
            "needs me",
            "needs you",
            "waiting for me",
            "my tasks",
            "my to do",
            "next step",
            "next steps",
            "what is next",
        ),
    ),
    "devices": (
        "qwen-devices.md",
        "Devices needing attention",
        (
            "battery",
            "batteries",
            "low battery",
            "motion",
            "motion sensor",
            "motion sensors",
            "sensor",
            "sensors",
            "offline",
            "unavailable",
            "not responding",
            "dead",
            "device",
            "devices",
            "entity",
            "entities",
        ),
    ),
    "status": (
        "house-status.md",
        "House status",
        (
            "status",
            "house",
            "home",
            "overall",
            "summary",
            "how is",
            "how are",
            "everything",
            "all good",
            "okay",
            "backup",
            "backups",
            "update",
            "updates",
            "restart",
            "restarted",
            "cost",
            "budget",
            "spend",
            "safety",
            "smoke",
            "lock",
            "alarm",
        ),
    ),
}


def brief_dir() -> Path:
    from openjarvis.agents.taskbook import book_dir

    return book_dir() / "qwen"


def _redact(text: str) -> str:
    """Secrets only: these files stay on this Mac and are read by local Qwen."""
    return Redactor.redact_secrets(text)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _open_tasks(store: Store, now: float) -> tuple[str, str]:
    clusters = plan_clusters(store)
    live = sync_open_tasks(store, clusters)
    rows = [c for c in clusters if c["id"] in live]
    serious = sum(1 for c in rows if c["severity"] == "critical")
    lines = ["# Open tasks", ""]
    if not rows:
        lines.append("There are no open tasks.")
    for c in rows:
        t = live[c["id"]]
        lines.append(
            f"- {t['id']}: {plain_label(c)}. {_plural(c['count'], 'problem')}, "
            f"severity {c['severity']}, effort {c['effort']}."
        )
        if t.get("seen_before"):
            lines.append(f"  Seen before: {t['seen_before']}.")
        if c.get("note"):
            lines.append(f"  Note: {c['note']}")
        if c.get("examples"):
            lines.append("  Affected: " + "; ".join(c["examples"][:3]) + ".")
    summary = (
        "No open tasks."
        if not rows
        else f"{_plural(len(rows), 'open task')}, {serious} serious."
    )
    return "\n".join(lines) + "\n", summary


def _solutions(store: Store, now: float) -> tuple[str, str]:
    rows = list(reversed(store.closed_tasks()))
    lines = ["# Solutions found", ""]
    if not rows:
        lines.append("Nothing has been resolved yet.")
    for r in rows[:LIST_LIMIT]:
        solution = " ".join((r["solution"] or "no solution text").split())[:240]
        lines.append(f"- {r['id']}: {r['label']}. Solution: {solution}")
    if len(rows) > LIST_LIMIT:
        lines.append(f"(and {len(rows) - LIST_LIMIT} older ones)")
    return "\n".join(lines) + "\n", f"{_plural(len(rows), 'solution')} found."


def _projects(store: Store, now: float) -> tuple[str, str]:
    from openjarvis.server.projects_routes import load_overview

    try:
        ov = load_overview()
    except Exception:  # noqa: BLE001 - missing project files are not an error
        ov = {"projects": [], "counts": {}, "needs_you": []}
    lines = ["# Projects", ""]
    counts = ov.get("counts") or {}
    if counts:
        lines.append(
            "Count by status: " + ", ".join(f"{k} {v}" for k, v in counts.items())
        )
    for p in ov.get("projects", [])[:LIST_LIMIT]:
        lines.append(f"- {p['title']} ({p['status']}, {p['area']}).")
    needs = ov.get("needs_you") or []
    if needs:
        lines += ["", "Waiting for you:"]
        lines += [f"- {n['text']} ({n['project']})" for n in needs[:LIST_LIMIT]]
    n = len(ov.get("projects", []))
    return "\n".join(
        lines
    ) + "\n", f"{_plural(n, 'project')}, {len(needs)} waiting for you."


def _devices(store: Store, now: float) -> tuple[str, str]:
    """Only what needs attention: low or unknown batteries, long-offline devices."""
    ov = overview(store, now)
    sec = ov["sections"]
    low = [i for i in sec["motion_batteries"]["items"] if i["severity"] != "info"]
    stale = sec["stale_unavailable"]
    lines = ["# Devices needing attention", ""]
    if low:
        lines.append("Motion sensors with a low or unknown battery:")
        lines += [f"- {i['title']}: {i['detail']}" for i in low[:LIST_LIMIT]]
    else:
        lines.append("Motion sensor batteries: all fine.")
    lines.append("")
    if stale["total"]:
        lines.append(f"Devices offline for a long time ({stale['total']}):")
        lines += [
            f"- {i['title']}: {i['detail']}" for i in stale["items"][:STALE_LIMIT]
        ]
    else:
        lines.append("No device has been offline for a long time.")
    return (
        "\n".join(lines) + "\n",
        f"{_plural(len(low), 'low battery', 'low batteries')}, "
        f"{stale['total']} long offline.",
    )


def _status(store: Store, now: float) -> tuple[str, str]:
    text = build_house_status(store, now)
    first = next((ln for ln in text.splitlines()[2:] if ln.strip()), "")
    return text, first


BUILDERS = {
    "open-tasks": _open_tasks,
    "solutions": _solutions,
    "projects": _projects,
    "devices": _devices,
    "status": _status,
}


def build_index(summaries: dict[str, str]) -> str:
    lines = [
        "# Qwen index",
        "",
        "Read this first. Find the topic whose keywords match the question, then "
        "open only that file.",
        "",
    ]
    for topic, (fname, title, words) in TOPICS.items():
        lines += [
            f"## {title} -> {fname}",
            f"Now: {summaries.get(topic, '-')}",
            "Keywords: " + ", ".join(words),
            "",
        ]
    return "\n".join(lines)


def build_briefs(store: Store, now: float | None = None) -> dict[str, str]:
    """File name -> text for the index and every detail file."""
    now = now if now is not None else time.time()
    files: dict[str, str] = {}
    summaries: dict[str, str] = {}
    for topic, build in BUILDERS.items():
        try:
            text, summary = build(store, now)
        except Exception as exc:  # noqa: BLE001 - one topic failing must not stop the rest
            logger.warning("Qwen brief %s failed: %s", topic, exc)
            continue
        files[TOPICS[topic][0]] = _redact(text)
        summaries[topic] = _redact(summary)
    files[INDEX_FILE] = build_index(summaries)
    return files


def write_briefs(store: Store, now: float | None = None) -> dict[str, str]:
    files = build_briefs(store, now)
    base = brief_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (base / name).write_text(text, encoding="utf-8")
    except OSError as exc:  # a read-only disk must not break anything
        logger.warning("Could not write Qwen briefs to %s: %s", base, exc)
    return files


def pick_topics(question: str) -> list[str]:
    """Topics whose keywords occur in the question, best match first (at most two).

    Longer keywords count more, so 'low battery' beats 'which'. Nothing matches
    -> the house status.
    """
    q = " " + re.sub(r"[^a-z0-9' ]+", " ", question.lower()) + " "
    scores: dict[str, int] = {}
    for topic, (_, _, words) in TOPICS.items():
        score = sum(len(w.split()) * 2 + 1 for w in words if f" {w} " in q)
        if score:
            scores[topic] = score
    ranked = sorted(scores, key=lambda t: -scores[t])
    keep = (
        [t for t in ranked if scores[t] * 10 >= scores[ranked[0]] * 7] if ranked else []
    )
    return keep[:MAX_PICK] or ["status"]


def context_for(store: Store, question: str, now: float | None = None) -> str:
    """The text Qwen reads for this question: only the matching detail files."""
    files = write_briefs(store, now)
    parts = [
        files[TOPICS[t][0]] for t in pick_topics(question) if TOPICS[t][0] in files
    ]
    return "\n".join(parts) if parts else files.get(INDEX_FILE, "")


__all__ = [
    "INDEX_FILE",
    "TOPICS",
    "build_briefs",
    "context_for",
    "pick_topics",
    "write_briefs",
]
