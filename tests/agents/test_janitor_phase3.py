"""Phase 3: clusters, severity x effort matrix, Haiku triage, digest, schedule."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import reporting
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.clusters import (
    build_clusters,
    cluster_of,
    matrix_counts,
    quadrant,
)
from openjarvis.agents.store import SettingsError, Store

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
FAKE_PW = "hunter2-very-secret-pw"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for var in ("JARVIS_JANITOR_DRY_RUN", "JARVIS_TRIAGE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return Store(tmp_path / "janitor.db")


def add(store, category, source, title, severity="warning", **meta):
    from openjarvis.agents.watchdog import Observation

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
    return o.fingerprint


def clusters(store):
    return build_clusters(
        store.issues(), store.settings(), store.overrides(), store.triage_notes()
    )


def by_id(cl):
    return {c["id"]: c for c in cl}


# --- clustering ---


def test_same_integration_collapses_repair_integration_and_log(store):
    add(store, "repair", "fritz/a", "Repair: fritz / a", domain="fritz")
    add(store, "repair", "fritz/b", "Repair: fritz / b", domain="fritz")
    add(store, "integration", "e1", "Integration fritz: setup_retry", domain="fritz")
    add(store, "log", "fritz:errors", "Log: 5 errors from fritz", component="fritz")
    add(store, "repair", "smtp/x", "Repair: smtp / x", domain="smtp")
    cl = by_id(clusters(store))
    assert cl["integration:fritz"]["count"] == 4
    assert cl["integration:smtp"]["count"] == 1
    assert cluster_of({"category": "motion", "meta": "{}"})[0] == "devices:motion"
    assert cluster_of({"category": "weird", "meta": "{}"})[0] == "other:weird"


def test_only_active_issues_are_clustered(store):
    a = add(store, "repair", "x/1", "t", domain="x")
    b = add(store, "repair", "x/2", "t", domain="x")
    c = add(store, "repair", "x/3", "t", domain="x")
    store.set_issue_state(a, "resolved")
    store.set_issue_state(b, "ignored")
    store.set_issue_state(c, "acknowledged")
    assert by_id(clusters(store))["integration:x"]["count"] == 1


# --- effort, kind, priority ------------------------------------------------------


def test_effort_rules_chore_and_default(store):
    add(store, "battery", "sensor.a_battery", "Battery 10%: a", level=10)
    add(store, "entity_group", "light", "light: 9 of 20", domain="light")
    add(store, "repair", "x/1", "t", domain="x")
    cl = by_id(clusters(store))
    assert cl["chore:batteries"]["effort"] == "quick"
    assert cl["chore:batteries"]["kind"] == "chore"
    assert cl["chore:batteries"]["needs_user"] is False
    assert cl["entities:light"]["effort"] == "hard"
    assert cl["entities:light"]["kind"] == "change"
    # repair is "auto": nothing rated yet -> default_effort
    assert cl["integration:x"]["effort"] == "medium"
    assert cl["integration:x"]["effort_source"] == "default"


def test_override_beats_rule_and_can_be_cleared(store):
    add(store, "entity_group", "light", "t", domain="light")
    store.set_override("entities:light", "quick", "chore")
    c = by_id(clusters(store))["entities:light"]
    assert (c["effort"], c["effort_source"], c["kind"]) == (
        "quick",
        "override",
        "chore",
    )
    store.set_override("entities:light", None, None)
    assert by_id(clusters(store))["entities:light"]["effort"] == "hard"
    with pytest.raises(ValueError):
        store.set_override("x", "huge", None)


def test_priority_critical_first_then_quick_and_deadline_wins(store):
    add(store, "critical_entity", "lock.a", "lock down", "critical", entity_id="lock.a")
    add(store, "battery", "sensor.b", "low", level=5)
    add(store, "entity_group", "sensor", "many", domain="sensor")
    add(store, "repair", "fritz/x", "deprecated", domain="fritz", breaks_in="2026.11.0")
    order = [c["id"] for c in clusters(store)]
    assert order[0] == "integration:fritz"  # deadline beats everything
    assert order.index("devices:critical") < order.index("chore:batteries")
    assert order.index("chore:batteries") < order.index("entities:sensor")
    assert clusters(store)[0]["deadline"] == "2026.11.0"


def test_quadrants_and_matrix_counts(store):
    assert quadrant("warning", "quick") == "quick_win"
    assert quadrant("info", "quick") == "fill_in"
    assert quadrant("critical", "hard") == "major_project"
    assert quadrant("warning", "hard") == "defer"
    assert quadrant("critical", "medium") == "scheduled"
    add(store, "battery", "sensor.b", "low")
    add(store, "battery", "sensor.c", "low")
    add(store, "entity_group", "sensor", "many", "critical", domain="sensor")
    m = matrix_counts(clusters(store))
    assert m["cells"]["warning"]["quick"] == {"clusters": 1, "issues": 2}
    assert m["cells"]["critical"]["hard"] == {"clusters": 1, "issues": 1}
    assert m["quick_wins"] == ["chore:batteries"]
    assert m["chores"] == ["chore:batteries"] and m["needs_user"] == ["entities:sensor"]


def test_matrix_settings_validation_and_defaults(store):
    s = store.settings()["matrix"]
    assert "batch_size" not in s and s["chore_categories"] == ["battery"]
    good = {**s, "chore_categories": ["battery", "motion"]}
    assert store.update_settings({"matrix": good})["matrix"]["chore_categories"] == [
        "battery",
        "motion",
    ]
    for bad in (
        {**s, "batch_size": 5},
        {**s, "default_effort": "auto"},
        {**s, "effort_defaults": {"repair": "easy"}},
        {**s, "chore_categories": "battery"},
        {"effort_defaults": {}},
    ):
        with pytest.raises(SettingsError):
            store.update_settings({"matrix": bad})


# --- triage ---


class FakeSDK:
    def __init__(self, reply="", fail=False):
        self.reply, self.requests = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=300,
                output_tokens=80,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
        )


def live_client(store, reply):
    store.update_settings({"dry_run": False})
    sdk = FakeSDK(reply)
    return CloudClient(store, sdk_client=sdk), sdk


def test_triage_dry_run_sends_nothing_and_saves_nothing(store):
    add(store, "repair", "x/1", "t", domain="x")
    sdk = FakeSDK("{}")
    client = CloudClient(store, sdk_client=sdk, key_resolver=lambda: 1 / 0)
    out = reporting.run_triage(store, client, clusters(store), manual=False)
    assert out["status"] == "dry_run" and out["clusters"] == 1
    assert sdk.requests == [] and store.triage_notes() == {}


def test_triage_live_saves_notes_and_rates_only_auto_clusters(store):
    add(store, "repair", "x/1", "t", domain="x")
    add(store, "entity_group", "light", "t", domain="light")
    reply = json.dumps(
        {
            "clusters": [
                {
                    "id": "integration:x",
                    "note": "Check auth",
                    "cause": "token",
                    "effort": "quick",
                },
                {"id": "entities:light", "note": "n", "cause": "c", "effort": "quick"},
                {"id": "unknown:id", "note": "x", "cause": "x", "effort": "quick"},
            ]
        }
    )
    client, sdk = live_client(store, "```json\n" + reply + "\n```")
    out = reporting.run_triage(store, client, clusters(store), manual=False)
    assert out["status"] == "ok" and out["saved"] == 2 and out["cost_usd"] > 0
    cl = by_id(clusters(store))
    # repair cluster is "auto": Haiku's rating is used; light is rule-fixed: kept hard
    assert (cl["integration:x"]["effort"], cl["integration:x"]["effort_source"]) == (
        "quick",
        "haiku",
    )
    assert cl["entities:light"]["effort"] == "hard"
    assert cl["integration:x"]["note"] == "Check auth"
    assert sdk.requests[0]["model"] == "claude-haiku-5-5"
    # nothing new -> no second call; new member -> re-triage that cluster
    assert (
        reporting.run_triage(store, client, clusters(store), manual=False)["status"]
        == "nothing_to_do"
    )
    add(store, "repair", "x/2", "t2", domain="x")
    again = reporting.run_triage(store, client, clusters(store), manual=False)
    assert again["clusters"] == 1 and len(sdk.requests) == 2


def test_triage_request_is_redacted(store):
    add(
        store, "repair", "smtp/auth", f"Repair: smtp password: {FAKE_PW}", domain="smtp"
    )
    add(store, "repair", "smtp/b", "mail to jane.doe@example.com failed", domain="smtp")
    client, sdk = live_client(store, '{"clusters": []}')
    reporting.run_triage(store, client, clusters(store), manual=False)
    sent = json.dumps(sdk.requests[0])
    assert FAKE_PW not in sent and "jane.doe@example.com" not in sent


def test_triage_failure_modes_never_raise(store):
    add(store, "repair", "x/1", "t", domain="x")
    client, _ = live_client(store, "not json at all")
    assert (
        reporting.run_triage(store, client, clusters(store), manual=False)["status"]
        == "bad_reply"
    )

    def no_key():
        from openjarvis.agents.cloud_client import CloudUnavailable

        raise CloudUnavailable("no key")

    store.update_settings({"dry_run": False})
    c2 = CloudClient(store, key_resolver=no_key)
    assert (
        reporting.run_triage(store, c2, clusters(store), manual=False)["status"]
        == "skipped_no_key"
    )

    store.update_settings(
        {
            "budget": {
                "monthly_limit_usd": 0.0001,
                "pause_auto_escalation_pct": 75,
                "hard_stop_pct": 100,
            }
        }
    )
    c3 = CloudClient(store, sdk_client=FakeSDK("{}"))
    assert (
        reporting.run_triage(store, c3, clusters(store), manual=False)["status"]
        == "skipped_budget"
    )


# --- schedule ---


def local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


def test_last_scheduled_daily_and_weekly(store):
    daily = store.settings()["schedule"]  # 07:30 daily
    assert reporting.last_scheduled(local(2026, 10, 25, 9), daily) == local(
        2026, 10, 25, 7, 30
    )
    assert reporting.last_scheduled(local(2026, 10, 25, 6), daily) == local(
        2026, 10, 24, 7, 30
    )
    weekly = {**daily, "frequency": "weekly", "weekday": "fri"}
    # 2026-10-25 is a Sunday: last Friday is the 23rd
    assert reporting.last_scheduled(local(2026, 10, 25, 9), weekly) == local(
        2026, 10, 23, 7, 30
    )
    assert reporting.last_scheduled(local(2026, 10, 23, 7, 0), weekly) == local(
        2026, 10, 16, 7, 30
    )


def test_report_due_once_per_slot(store):
    assert reporting.report_due(store, NOW)
    reporting.generate_report(store, now=NOW)
    assert not reporting.report_due(store, NOW + 600)
    nxt = local(2026, 10, 26, 7, 31)
    assert reporting.report_due(store, nxt)


# --- digest ---


def test_digest_status_rules_and_shape(store):
    status, text, body = reporting.build_digest(store, clusters(store), NOW)
    assert status == "ok" and "Open issues 0" in text

    add(store, "entity_group", "sensor", "many", "critical", domain="sensor")
    status, text, _ = reporting.build_digest(store, clusters(store), NOW)
    assert status == "attention"  # a big group is not a "critical" status by itself

    add(store, "critical_entity", "lock.a", "lock down", "critical", entity_id="lock.a")
    add(store, "repair", "fritz/x", "dep", domain="fritz", breaks_in="2026.11.0")
    add(store, "battery", "sensor.b", "low")
    status, text, body = reporting.build_digest(store, clusters(store), NOW)
    assert status == "critical"
    lines = text.splitlines()
    assert len(lines) <= 10 and lines[0].startswith("Janitor Sun 25 Oct: CRITICAL")
    assert any("Deadline: breaking changes in HA 2026.11.0" in ln for ln in lines)
    assert any("Critical devices DOWN: 1" in ln for ln in lines)
    assert any(ln.startswith("Quick wins: 1") for ln in lines)
    assert any("Budget: $0.00 of $20.00" in ln for ln in lines)
    assert body["matrix"]["quick_wins"] == ["chore:batteries"]


def test_generate_report_stores_digest_even_if_triage_breaks(store, monkeypatch):
    add(store, "repair", "x/1", "t", domain="x")
    out = reporting.generate_report(store, now=NOW)  # dry-run default
    assert out["triage"]["status"] == "dry_run" and out["id"] == 1
    rep = store.latest_report()
    assert rep["status"] == "attention" and rep["summary_text"] == out["text"]

    def boom(*a, **k):
        raise RuntimeError("haiku down")

    monkeypatch.setattr(reporting, "run_triage", boom)
    out2 = reporting.generate_report(store, now=NOW + 5)
    assert out2["triage"]["status"] == "error" and out2["id"] == 2


def test_service_generates_one_report_per_slot(store):
    from openjarvis.agents.watchdog import WatchdogService

    svc = WatchdogService(
        store, SimpleNamespace(collect=lambda s: {}), clock=lambda: NOW
    )
    svc._maybe_report()
    svc._maybe_report()
    assert store.latest_report()["id"] == 1


# --- API ---


def test_cluster_and_report_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    store_mod.reset_store_for_tests()
    st = store_mod.get_store()
    add(st, "battery", "sensor.a", "Battery 5%", level=5)
    add(st, "repair", "x/1", "t", domain="x")
    app = FastAPI()
    app.include_router(ha_agents_router)
    http = TestClient(app)

    data = http.get("/v1/ha/agents/clusters").json()
    assert {c["id"] for c in data["clusters"]} == {"chore:batteries", "integration:x"}
    assert data["matrix"]["chores"] == ["chore:batteries"]

    ov = http.put(
        "/v1/ha/agents/clusters/override",
        json={"id": "integration:x", "effort": "hard"},
    )
    assert by_id(ov.json()["clusters"])["integration:x"]["effort"] == "hard"
    assert (
        http.put(
            "/v1/ha/agents/clusters/override",
            json={"id": "integration:x", "effort": "huge"},
        ).status_code
        == 422
    )
    assert (
        http.put("/v1/ha/agents/clusters/override", json={"effort": "hard"}).status_code
        == 422
    )
    cleared = http.put("/v1/ha/agents/clusters/override", json={"id": "integration:x"})
    assert (
        by_id(cleared.json()["clusters"])["integration:x"]["effort_source"] == "default"
    )

    assert http.get("/v1/ha/agents/reports/latest").json()["report"] is None
    run = http.post("/v1/ha/agents/reports/run").json()  # dry-run: no cloud call
    assert run["triage"]["status"] == "dry_run" and "Open issues 2" in run["text"]
    assert http.get("/v1/ha/agents/reports/latest").json()["report"]["id"] == run["id"]
    assert http.post("/v1/ha/agents/triage/run").json()["status"] == "dry_run"
