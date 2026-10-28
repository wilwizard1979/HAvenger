"""The Projects page reads the owner's Markdown backlog (read-only)."""

from __future__ import annotations

from openjarvis.server import projects_routes as pr

BACKLOG = """# Projects Backlog

## <Project name>
**Status:** Idea | Planned

## Voice Mode
**Status:** In progress (waiting) · **Added:** 2026-09-27
Hands-free voice chat.
**Proposed plan:**
1. Wait for feedback.
2. Open the PR
   from my fork.
**Notes:** branch x

## Google Workspace Connection
**Status:** Done · **Added:** 2026-09-27
Reads Gmail.
**User tasks:** sign in again; approve the test task

## Tesvor vacuum
**Status:** Blocked at pairing
Nothing yet.
"""
HA = """# Home Assistant Projects

## Done
- not a project

## LED lamp — PARKED (2026-10-12)
**Status:** Hypothesis only.
Try the app first.
"""
HANDOFF = """# Handoff

## 6. BACKLOG

### 6a. Next (Janitor)
1. **Edit-with-diff step** and a test.
2. Decide allowed tools.

### 6d. User tasks
- Rotate the password.
- Decide LIVE timing.

---

## 7. Known issues
- not a task
"""


def test_statuses_and_areas():
    assert pr.normalize_status("In progress — x") == "in_progress"
    assert pr.normalize_status("Done") == "done"
    assert pr.normalize_status("On hold — parked") == "on_hold"
    assert pr.normalize_status("Blocked at device pairing") == "blocked"
    assert pr.normalize_status("Hypothesis only") == "idea"
    assert pr.area_of("Tesvor X500 robot vacuum") == "Home Assistant"
    assert pr.area_of("Skills for Gmail") == "Google & skills"


def test_projects_are_parsed_and_template_is_skipped():
    items = pr.parse_projects(BACKLOG, "Projects Backlog.md")
    assert [p["title"] for p in items] == [
        "Voice Mode",
        "Google Workspace Connection",
        "Tesvor vacuum",
    ]
    voice = items[0]
    assert voice["status"] == "in_progress" and voice["added"] == "2026-09-27"
    assert voice["plan"] == ["Wait for feedback.", "Open the PR from my fork."]
    assert voice["summary"] == "Hands-free voice chat."
    assert items[1]["status"] == "done"
    assert items[1]["user_tasks"] == ["sign in again", "approve the test task"]
    assert items[2]["status"] == "blocked"


def test_ha_sections_without_status_are_not_projects():
    items = pr.parse_projects(HA, "Home Assistant Projects.md")
    assert [p["title"] for p in items] == ["LED lamp"]


def test_handoff_backlog_and_user_tasks_stop_at_the_next_section():
    h = pr.parse_handoff(HANDOFF)
    assert h["needs_you"] == ["Rotate the password.", "Decide LIVE timing."]
    assert h["groups"][0]["group"] == "Next (Janitor)"
    assert len(h["groups"][0]["items"]) == 2


def test_overview_from_a_folder_and_endpoint(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    (tmp_path / "Projects Backlog.md").write_text(BACKLOG)
    (tmp_path / "Home Assistant Projects.md").write_text(HA)
    (tmp_path / "Janitor Handoff 2026-10-26.md").write_text(HANDOFF)
    monkeypatch.setenv("JARVIS_PROJECTS_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(pr.projects_router)
    out = TestClient(app).get("/v1/projects").json()
    assert out["counts"]["done"] == 1 and out["counts"]["blocked"] == 1
    texts = [n["text"] for n in out["needs_you"]]
    assert "Rotate the password." in texts and "sign in again" in texts


def test_missing_folder_gives_an_empty_overview(tmp_path):
    out = pr.load_overview(tmp_path / "nope")
    assert out["projects"] == [] and out["needs_you"] == []


def test_md_file_index_lists_title_and_purpose(tmp_path):
    (tmp_path / "A.md").write_text("# Alpha\n\nWhat it is.\n")
    (tmp_path / "qwen").mkdir()
    (tmp_path / "qwen" / "b.md").write_text("# Bee\n| x |\nBee text\n")
    files = {f["name"]: f for f in pr.list_md_files(tmp_path)}
    assert (
        files["A.md"]["title"] == "Alpha" and files["A.md"]["purpose"] == "What it is."
    )
    assert files["qwen/b.md"]["purpose"] == "Bee text"
    assert files["A.md"]["location"].startswith("Mac Mini: ")
    assert pr.load_overview(tmp_path)["files"]


def test_backlog_line_uses_backlog_then_open_then_next_then_plan():
    text = (
        "## A\n**Status:** Idea\n**Backlog:** do x\n**Next:** y\n\n"
        "## B\n**Status:** Idea\n**Next:** y\n\n"
        "## C\n**Status:** Idea\n**Proposed plan:**\n1. ✅ done\n2. todo this\n"
    )
    got = {p["title"]: p["backlog"] for p in pr.parse_projects(text, "x.md")}
    assert got == {"A": "do x", "B": "y", "C": "todo this"}
