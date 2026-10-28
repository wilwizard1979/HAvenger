"""Read-only overview of the owner's project backlog, for the Projects page.

The Markdown files stay the source of truth (they are edited by hand and by
Claude Code). This module only reads and structures them; it never writes.
Files, in ``~/OpenJarvis/jarvis`` (override: ``JARVIS_PROJECTS_DIR``):

* ``Projects Backlog.md``       one ``## Title`` section per project, ``**Status:**``
* ``Home Assistant Projects.md`` ``##`` sections that carry a status
* ``Janitor Handoff*.md``       section 6 (backlog) and its "User tasks"
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

projects_router = APIRouter(prefix="/v1/projects", tags=["projects"])

STATUSES = ("idea", "planned", "in_progress", "on_hold", "blocked", "done")
SKIP_HEADINGS = {
    "<project name>",
    "done",
    "limitations / issues found",
    "open / next (when revisited)",
}
AREAS = [
    (
        "Home Assistant",
        ("home assistant", "ha-mcp", "janitor", "tesvor", "led ", "ha ", "telegram"),
    ),
    ("Google & skills", ("google", "gmail", "email", "calendar", "skills")),
    ("Jarvis core", ("voice", "rust", "claude as cloud", "dashboard")),
]


def projects_dir() -> Path:
    return Path(
        os.environ.get("JARVIS_PROJECTS_DIR") or "~/OpenJarvis/jarvis"
    ).expanduser()


def normalize_status(text: str) -> str:
    t = text.lower()
    if re.search(r"\bdone\b|\bcomplete", t):
        return "done"
    if "blocked" in t:
        return "blocked"
    if "on hold" in t or "parked" in t:
        return "on_hold"
    if "in progress" in t or "built" in t or "phases" in t:
        return "in_progress"
    if "planned" in t or "prepared" in t:
        return "planned"
    return "idea"


def area_of(title: str) -> str:
    t = f"{title.lower()} "
    for name, words in AREAS:
        if any(w in t for w in words):
            return name
    return "Other"


def _clean(text: str) -> str:
    return re.sub(r"\*\*|`", "", text).strip()


def _sections(text: str, level: str = "## ") -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    depth = len(level.strip())
    inside = False
    for line in text.splitlines():
        m = re.match(r"^(#+)\s", line)
        if m and len(m.group(1)) <= depth:  # a heading of this level or higher
            if len(m.group(1)) == depth:
                out.append((line[len(level) :].strip(), []))
                inside = True
            else:
                inside = False
        elif inside:
            out[-1][1].append(line)
    return out


def _field(lines: list[str], name: str) -> str:
    """Value of a ``**Name:** value`` line (first match, rest of the line)."""
    pat = re.compile(rf"^\*\*{re.escape(name)}[^*]*\*\*:?\s*(.*)$", re.IGNORECASE)
    for line in lines:
        m = pat.match(line.strip())
        if m:
            return m.group(1).strip()
    return ""


def _numbered_after(lines: list[str], name: str) -> list[str]:
    out: list[str] = []
    on = False
    for line in lines:
        s = line.strip()
        if s.lower().startswith(f"**{name.lower()}"):
            on = True
            continue
        if on:
            if re.match(r"^\d+\.\s", s):
                out.append(_clean(re.sub(r"^\d+\.\s*", "", s)))
            elif s.startswith("**") or (s and not out and False):
                break
            elif out and s and not s.startswith(("-", "*")):
                out[-1] += " " + _clean(s)  # wrapped line of the previous step
    return out


def _summary(lines: list[str]) -> str:
    para: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("**") or s.startswith("###"):
            if para:
                break
            continue
        if not s:
            if para:
                break
            continue
        para.append(s)
    return _clean(" ".join(para))[:500]


def _backlog(lines: list[str], plan: list[str], next_step: str) -> str:
    """One short line on what is still open: ``**Backlog:**``, else Open, Next or
    the first plan step that is not ticked off."""
    for name in ("Backlog", "Open"):
        text = _clean(_field(lines, name))
        if text:
            return text[:220]
    if next_step:
        return next_step[:220]
    todo = next((x for x in plan if "\u2705" not in x), "")
    return _clean(todo)[:220]


def parse_projects(text: str, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for title, lines in _sections(text):
        if title.lower() in SKIP_HEADINGS:
            continue
        status_line = _field(lines, "Status")
        if not status_line and not re.search(r"parked|prepared", title, re.I):
            continue
        status_text = status_line.split("· **")[0].strip() or title
        added = re.search(r"Added:\*\*\s*([0-9-]+)", status_line)
        user_tasks = _field(lines, "User tasks")
        plan = _numbered_after(lines, "Proposed plan")[:10]
        next_step = _clean(_field(lines, "Next"))[:500]
        out.append(
            {
                "id": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60],
                "title": re.sub(r"\s*—\s*(PARKED|PREPARED).*$", "", title).strip(),
                "area": area_of(title),
                "status": normalize_status(status_text or title),
                "status_text": _clean(status_text)[:200],
                "added": added.group(1) if added else None,
                "summary": _summary(
                    [x for x in lines if not x.strip().startswith("**Status")]
                ),
                "plan": plan,
                "next": next_step,
                "backlog": _backlog(lines, plan, next_step),
                "notes": _clean(_field(lines, "Notes"))[:500],
                "user_tasks": [
                    _clean(t) for t in re.split(r";\s*", user_tasks) if t.strip()
                ],
                "source": source,
            }
        )
    return out


def parse_handoff(text: str) -> dict[str, Any]:
    """Backlog bullets of the Janitor handoff (section 6) and the user's tasks."""
    groups: list[dict[str, Any]] = []
    needs_you: list[str] = []
    for title, lines in _sections(text, "### "):
        m = re.match(r"6([a-d])\.\s*(.*)", title)
        if not m:
            continue
        items: list[str] = []
        for line in lines:
            s = line.strip()
            if re.match(r"^(\d+\.|-)\s", s):
                items.append(_clean(re.sub(r"^(\d+\.|-)\s*", "", s))[:400])
            elif items and s and not s.startswith(("#", "---")):
                items[-1] += " " + _clean(s)
        if m.group(1) == "d":
            needs_you = items
        else:
            groups.append({"group": m.group(2).strip(), "items": items})
    return {"groups": groups, "needs_you": needs_you}


def list_md_files(base: Path | None = None) -> list[dict[str, Any]]:
    """Every Markdown file in the notes folder (and ``qwen/``): title, purpose, age."""
    base = base or projects_dir()
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    for path in sorted([*base.glob("*.md"), *base.glob("qwen/*.md")]):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            mtime = path.stat().st_mtime
        except OSError:
            continue
        title = next(
            (ln.lstrip("# ").strip() for ln in lines if ln.startswith("#")), ""
        )
        purpose = next(
            (
                ln.strip()
                for ln in lines
                if ln.strip() and not ln.startswith("#") and not ln.startswith("|")
            ),
            "",
        )
        out.append(
            {
                "name": str(path.relative_to(base)),
                "location": "Mac Mini: " + str(path).replace(str(Path.home()), "~", 1),
                "title": title or path.stem,
                "purpose": purpose[:160],
                "modified": mtime,
            }
        )
    return out


def load_overview(base: Path | None = None) -> dict[str, Any]:
    base = base or projects_dir()

    def read(name: str) -> str:
        try:
            return (base / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    projects = parse_projects(read("Projects Backlog.md"), "Projects Backlog.md")
    seen = {p["id"] for p in projects}
    for p in parse_projects(
        read("Home Assistant Projects.md"), "Home Assistant Projects.md"
    ):
        if p["id"] not in seen:
            projects.append(p)
    handoffs = sorted(base.glob("Janitor Handoff*.md")) if base.exists() else []
    handoff = (
        parse_handoff(handoffs[-1].read_text(encoding="utf-8"))
        if handoffs
        else {
            "groups": [],
            "needs_you": [],
        }
    )
    needs = [{"text": t, "project": "Jarvis the Janitor"} for t in handoff["needs_you"]]
    for p in projects:
        if "janitor" in p["title"].lower() and handoff["needs_you"]:
            continue  # the handoff list is the detailed, current one
        needs += [{"text": t, "project": p["title"]} for t in p["user_tasks"]]
    seen_text: set[str] = set()
    needs = [
        n for n in needs if not (n["text"] in seen_text or seen_text.add(n["text"]))
    ]
    counts = {s: sum(1 for p in projects if p["status"] == s) for s in STATUSES}
    return {
        "projects": projects,
        "counts": counts,
        "areas": sorted({p["area"] for p in projects}),
        "needs_you": needs,
        "janitor_backlog": handoff["groups"],
        "files": list_md_files(base),
        "dir": str(base),
    }


@projects_router.get("")
def projects_overview() -> dict[str, Any]:
    """Projects, what needs the owner, and the Janitor backlog (read-only)."""
    return load_overview()
