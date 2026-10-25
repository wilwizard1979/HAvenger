"""Phase 7: plain-English house status, Qwen answers and read-out, log levels."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import house, planning, readout
from openjarvis.agents.ha_collector import parse_log_groups
from openjarvis.agents.store import SettingsError, Store
from openjarvis.agents.watchdog import Observation

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    return Store(tmp_path / "janitor.db")


def add(store, category, source, title, severity="warning", **meta):
    o = Observation(category, source, title, severity, meta)
    store.upsert_issue(
        o.fingerprint,
        category=category,
        source_id=source,
        title=title,
        severity=severity,
        meta=meta,
        now=NOW - 3600,
    )


def seed(store):
    add(
        store,
        "integration",
        "e1",
        "Integration fritz_box: setup_retry",
        domain="fritz_box",
    )
    add(
        store,
        "repair",
        "fritz_box/dep",
        "dep",
        domain="fritz_box",
        breaks_in="2026.11.0",
    )
    add(
        store,
        "critical_entity",
        "lock.front_door_secret",
        "lock down",
        "critical",
        entity_id="lock.front_door_secret",
    )
    add(store, "battery", "sensor.b", "Battery 5%", level=5)
    store.set_facts(
        {
            "backup_last_success": "2026-10-24T22:00:00+00:00",
            "updates_pending": 3,
            "boot_estimate": NOW - 30 * 3600,
        }
    )
    store.record_boot(NOW - 30 * 3600, NOW, False)


# --- the file ---


def test_status_is_short_plain_and_free_of_ids(store):
    seed(store)
    text = house.build_house_status(store, NOW)
    assert len(text.splitlines()) <= 24 and len(text.split()) < 300
    assert "Sunday 25 October, 09:00" in text and "Something important" in text
    assert "Safety devices: 1 not responding." in text and "Updates waiting: 3." in text
    assert "Easy wins: Low batteries (1)." in text
    assert "(first: 2026.11.0)" in text and "restarted 30 hours ago" in text
    # no entity ids, tool names or internal cluster ids
    assert not re.search(r"\b(lock|sensor|binary_sensor)\.\w+", text)
    assert "integration:" not in text and "front_door_secret" not in text
    assert "The fritz box connection: 2 problems." in text
    assert "1 problems" not in text


def test_all_good_and_haiku_notes_appear(store):
    assert "All good." in house.build_house_status(store, NOW)
    add(store, "integration", "e1", "t", domain="smtp")
    store.put_triage(
        "integration:smtp",
        "x",
        effort="quick",
        note="Check the login",
        cause="c",
        model="m",
    )
    from openjarvis.agents.clusters import build_clusters

    mh = build_clusters(store.issues(), store.settings())[0]["members_hash"]
    store.put_triage(
        "integration:smtp",
        mh,
        effort="quick",
        note="Check the login",
        cause="c",
        model="m",
    )
    assert "Note: Check the login" in house.build_house_status(store, NOW)


def test_file_is_written_where_qwen_and_you_can_find_it(store, tmp_path):
    seed(store)
    text, path = house.write_house_status(store, NOW)
    assert path == tmp_path / "out" / "house-status.md" and path.read_text() == text


def test_report_generation_refreshes_the_file(store, tmp_path):
    from openjarvis.agents import reporting

    seed(store)
    reporting.generate_report(store, now=NOW)
    assert (tmp_path / "out" / "house-status.md").exists()


# --- Qwen answers ---


def gen(*texts):
    calls = []

    def _g(model, system, prompt):
        calls.append(prompt)
        t = texts[min(len(calls) - 1, len(texts) - 1)]
        if isinstance(t, Exception):
            raise t
        return {"text": t, "tokens_in": 50, "tokens_out": 20}

    _g.calls = calls
    return _g


def test_answers_come_from_the_status_and_numbers_are_checked(store):
    seed(store)
    g = gen("There are 3 updates waiting.")
    out = house.ask_house(store, "How many updates are waiting?", generate=g)
    assert out == {"status": "ok", "answer": "There are 3 updates waiting."}
    assert (
        "Updates waiting: 3." in g.calls[0]
        and "QUESTION: How many updates" in g.calls[0]
    )
    assert store.runs_by_layer("L1", 1)[0]["purpose"] == "ask"


def test_invented_numbers_get_one_correction_then_a_safe_fallback(store):
    seed(store)
    g = gen("There are 9 updates.", "There are 3 updates.")
    assert (
        house.ask_house(store, "updates?", generate=g)["answer"]
        == "There are 3 updates."
    )
    assert "not in the status" in g.calls[1]
    out = house.ask_house(store, "updates?", generate=gen("It is 99.", "Still 77."))
    assert out["status"] == "fallback" and "Updates waiting: 3." in out["answer"]


def test_a_number_from_the_question_is_allowed(store):
    seed(store)
    out = house.ask_house(
        store, "Is 42 a lot?", generate=gen("42 is not in the status, I do not know.")
    )
    assert out["status"] == "ok"


def test_qwen_down_and_switch_off(store):
    seed(store)
    out = house.ask_house(store, "q", generate=gen(OSError("refused")))
    assert out["status"] == "unavailable" and "Updates waiting" in out["status_text"]
    store.update_settings({"qwen": {"house_status": False, "read_aloud": True}})
    assert house.ask_house(store, "q", generate=gen("x"))["status"] == "disabled"


# --- read aloud ---


def test_read_aloud_uses_the_verified_readout_of_the_same_text(store):
    seed(store)
    store.add_report("ok", "digest", {}, ts=NOW)
    text = house.build_house_status(store, NOW)
    assert house.spoken_text(store, text) == text  # nothing read out yet
    out = readout.run_readout(
        store,
        generate=lambda m, s, src: {
            "text": "Three updates are waiting.",
            "tokens_in": 1,
            "tokens_out": 1,
        },
        now=NOW,
    )
    assert out["status"] == "ok"
    latest = store.latest_readout()
    assert latest["source_hash"] and "Three updates" in latest["text"]
    status_now = house.build_house_status(store, NOW)
    # same data -> the verified read-out is used; new data -> the plain text is used
    if house.source_hash(status_now) == latest["source_hash"]:
        assert house.spoken_text(store, status_now) == latest["text"]
    add(store, "repair", "x/1", "t", domain="x")
    assert "Three updates" not in house.spoken_text(
        store, house.build_house_status(store, NOW)
    )


def test_readout_reads_the_status_file_not_the_technical_digest(store):
    seed(store)
    store.add_report("ok", "Open issues 99 (critical 0)", {}, ts=NOW)
    seen = []
    readout.run_readout(
        store,
        generate=lambda m, s, src: (
            seen.append(src)
            or {"text": "Mostly fine.", "tokens_in": 1, "tokens_out": 1}
        ),
        now=NOW,
    )
    assert seen[0].startswith("# House status") and "Open issues 99" not in seen[0]


# --- the Jarvis tool ---


def test_tool_is_registered_and_relays_the_checked_answer(store, monkeypatch):
    from openjarvis.tools.house_status import HouseStatusTool

    assert HouseStatusTool().spec.name == "house_status"
    monkeypatch.setattr(
        house, "ask_house", lambda s, q: {"status": "ok", "answer": "3 updates."}
    )
    monkeypatch.setattr("openjarvis.agents.store.get_store", lambda: store)
    r = HouseStatusTool().execute(question="updates?")
    assert r.success and r.content == "3 updates."
    monkeypatch.setattr(house, "ask_house", lambda s, q: {"status": "disabled"})
    assert not HouseStatusTool().execute(question="x").success
    monkeypatch.setattr(
        house,
        "ask_house",
        lambda s, q: {"status": "unavailable", "status_text": "STATUS"},
    )
    assert HouseStatusTool().execute(question="x").content == "STATUS"


# --- log levels ---


def test_logs_show_warning_error_and_critical_fatal_only():
    log = (
        "2026-10-25 10:00:00.1 INFO (MainThread) [homeassistant.core] fine\n"
        "2026-10-25 10:00:01.1 DEBUG (MainThread) [homeassistant.core] noise\n"
        "2026-10-25 10:00:02.1 WARNING (MainThread) [homeassistant.components.mqtt]"
        " w1\n"
        "2026-10-25 10:00:03.1 WARNING (MainThread) [homeassistant.components.mqtt]"
        " w2\n"
        "2026-10-25 10:00:04.1 ERROR (MainThread) [homeassistant.components.mqtt]"
        " e1\n"
        "2026-10-25 10:00:05.1 FATAL (MainThread) [custom_components.hacs] f1\n"
    )
    g = parse_log_groups(log)
    assert set(g) == {"mqtt", "hacs"}
    assert (g["mqtt"]["warning"], g["mqtt"]["error"], g["mqtt"]["critical"]) == (
        2,
        1,
        0,
    )
    assert g["hacs"]["critical"] == 1


def test_overview_lists_log_levels_worst_first(store):
    store.set_facts(
        {
            "log_levels": [
                {
                    "component": "hacs",
                    "critical": 1,
                    "error": 0,
                    "warning": 0,
                    "example": "boom",
                },
                {
                    "component": "mqtt",
                    "critical": 0,
                    "error": 2,
                    "warning": 5,
                    "example": "",
                },
                {
                    "component": "zha",
                    "critical": 0,
                    "error": 0,
                    "warning": 3,
                    "example": "",
                },
            ],
            "log_critical_lines": 1,
        }
    )
    o = planning.overview(store, NOW)
    items = o["sections"]["log_levels"]["items"]
    assert [i["severity"] for i in items] == ["critical", "warning", "info"]
    assert "mqtt: 0 critical, 2 errors, 5 warnings" == items[1]["title"]
    assert (
        o["cards"]["log_levels"]["value"] == 11
        and o["cards"]["log_levels"]["tone"] == "bad"
    )


# --- settings and API ---


def test_qwen_switches(store):
    assert store.settings()["qwen"] == {"house_status": True, "read_aloud": True}
    for bad in ({"house_status": "yes", "read_aloud": True}, {"house_status": True}):
        with pytest.raises(SettingsError):
            store.update_settings({"qwen": bad})


def test_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path / "out"))
    store_mod.reset_store_for_tests()
    seed(store_mod.get_store())
    app = FastAPI()
    app.include_router(ha_agents_router)
    http = TestClient(app)
    hs = http.get("/v1/ha/agents/house-status").json()
    assert hs["text"].startswith("# House status") and hs["path"].endswith(
        "house-status.md"
    )
    assert (
        hs["read_aloud"] is True
        and hs["spoken"] == hs["text"]
        and hs["verified_readout"] is False
    )
    assert http.post("/v1/ha/agents/ask", json={}).status_code == 422
    monkeypatch.setattr(
        house, "ask_house", lambda s, q: {"status": "ok", "answer": "fine"}
    )
    from openjarvis.server import ha_agents_routes as routes

    monkeypatch.setattr(routes, "ask_house", house.ask_house)
    assert (
        http.post("/v1/ha/agents/ask", json={"question": "how?"}).json()["answer"]
        == "fine"
    )
