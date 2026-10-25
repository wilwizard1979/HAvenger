"""Phase 6: the Settings tab saves whole groups; every group must round-trip."""

from __future__ import annotations

import pytest


@pytest.fixture()
def http(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    store_mod.reset_store_for_tests()
    app = FastAPI()
    app.include_router(ha_agents_router)
    return TestClient(app)


EDITABLE = [
    "schedule",
    "budget",
    "models",
    "dry_run",
    "auto_escalation",
    "thresholds",
    "watchdog",
    "matrix",
    "escalation",
    "qwen",
    "telegram",
    "executor",
    "critical_entities",
    "motion_sensors",
    "ignore_patterns",
    "extra_redaction_terms",
]


def test_every_group_the_screen_shows_round_trips(http):
    current = http.get("/v1/ha/agents/settings").json()
    for key in EDITABLE:
        r = http.put("/v1/ha/agents/settings", json={key: current[key]})
        assert r.status_code == 200, (key, r.text)
        assert r.json()[key] == current[key]
    # all at once (what four cards sharing a group can send)
    assert (
        http.put(
            "/v1/ha/agents/settings", json={k: current[k] for k in EDITABLE}
        ).status_code
        == 200
    )


def test_the_screen_changes_survive_and_bad_values_say_why(http):
    cur = http.get("/v1/ha/agents/settings").json()
    th = {**cur["thresholds"], "stale_unavailable_days": 14, "battery_warn_pct": 40}
    out = http.put(
        "/v1/ha/agents/settings",
        json={
            "thresholds": th,
            "extra_redaction_terms": ["Mustermann", "Hauptstrasse"],
        },
    ).json()
    assert out["thresholds"]["battery_warn_pct"] == 40
    assert http.get("/v1/ha/agents/settings").json()["extra_redaction_terms"] == [
        "Mustermann",
        "Hauptstrasse",
    ]
    bad = http.put(
        "/v1/ha/agents/settings", json={"thresholds": {**th, "persist_min": None}}
    )
    assert bad.status_code == 422 and "persist_min" in bad.json()["detail"]
    bad = http.put(
        "/v1/ha/agents/settings",
        json={"budget": {**cur["budget"], "hard_stop_pct": 50}},
    )
    assert bad.status_code == 422
    bad = http.put(
        "/v1/ha/agents/settings",
        json={"schedule": {**cur["schedule"], "time": "25:99"}},
    )
    assert bad.status_code == 422
    # a rejected save changes nothing
    assert http.get("/v1/ha/agents/settings").json()["budget"] == cur["budget"]


def test_unknown_keys_and_partial_groups_are_refused(http):
    assert http.put("/v1/ha/agents/settings", json={"nonsense": 1}).status_code == 422
    assert (
        http.put(
            "/v1/ha/agents/settings", json={"telegram": {"enabled": True}}
        ).status_code
        == 422
    )


def test_redaction_terms_are_used_by_the_cloud_client(http):
    from openjarvis.agents.cloud_client import CloudClient
    from openjarvis.agents.store import get_store

    http.put("/v1/ha/agents/settings", json={"extra_redaction_terms": ["Mustermann"]})
    res = CloudClient(get_store()).complete(
        "triage",
        system="s",
        messages=[{"role": "user", "content": "Mustermann lock is down"}],
    )
    sent = str(res.request["messages"])
    assert "Mustermann" not in sent and "<name_1>" in sent


def test_hidden_words_ignore_case_accept_commas_and_work_inside_entity_ids():
    from openjarvis.agents.redaction import Redactor, split_terms

    assert split_terms(["Anna, Bob", " carl ", "ANNA", ""]) == ["Anna", "Bob", "carl"]
    r = Redactor(["Anna, Bob", "Main Street"])
    for text in (
        "Anna lock",
        "ANNA lock",
        "anna lock",
        "bob and ANNA",
        "main street 5",
    ):
        out = r.redact(text)
        assert "nna" not in out.lower().replace("<name", "") or "<name_" in out
        assert not any(w in out.lower() for w in ("anna", "bob", "main street"))
    eid = r.redact("binary_sensor.smoke_alarm_anna_alarm_status and sensor.Bob_battery")
    assert (
        "anna" not in eid.lower() and "bob" not in eid.lower() and "smoke_alarm_" in eid
    )
    # digits and letters still bound a word: no half-matches inside other words
    assert r.redact("Annabelle and bob2") == "Annabelle and bob2"
    # the answer is restored locally
    assert r.restore(r.redact("Anna called")) == "Anna called"


def test_comma_separated_words_are_split_when_saved(http):
    out = http.put(
        "/v1/ha/agents/settings",
        json={"extra_redaction_terms": ["Anna, Bob", "Main Street, Town"]},
    ).json()
    assert out["extra_redaction_terms"] == ["Anna", "Bob", "Main Street", "Town"]
