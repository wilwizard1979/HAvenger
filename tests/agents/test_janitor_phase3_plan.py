"""Phase 3b: overview totals, plan tasks, estimates, on-demand proposals."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import planning
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.store import Store
from openjarvis.agents.watchdog import Observation

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
FAKE_PW = "hunter2-very-secret-pw"


@pytest.fixture()
def store(tmp_path, monkeypatch):
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
    return o.fingerprint


def seed(store):
    add(store, "repair", "backup/a", "Repair: backup / a", domain="backup")
    add(store, "backup", "age", "Last successful backup is 40 h old", age_hours=40)
    add(store, "integration", "e1", "Integration fritz: setup_retry", domain="fritz")
    add(store, "integration", "e2", "Integration x: not_loaded", domain="x")
    add(store, "repair", "automation/1", "Repair: automation / 1", domain="automation")
    add(store, "repair", "fritz/dep", "dep", domain="fritz", breaks_in="2026.11.0")
    add(
        store,
        "log",
        "hacs:critical",
        "Log: 1 CRITICAL",
        "critical",
        component="hacs",
        example="boom",
    )
    add(store, "log", "x:errors", "Log: 4 errors from x", component="x")
    add(store, "battery", "sensor.b", "Battery 5%: b", level=5)
    store.set_facts(
        {
            "updates_pending": 3,
            "updates_list": [{"entity_id": "update.core", "name": "Core"}],
            "backup_last_success": "2026-10-24T02:54:22+00:00",
            "log_critical_lines": 2,
            "checked_at": NOW,
            "ha_version": "2026.10.1",
        }
    )


def test_overview_totals_and_sections(store):
    seed(store)
    o = planning.overview(store, NOW)
    c = o["cards"]
    assert c["backup_failed"]["value"] == 2  # backup repair + backup age
    assert c["integrations_failed"]["value"] == 2
    assert c["automation_errors"]["value"] == 1
    assert c["updates_pending"]["value"] == 3
    assert c["deprecations"] == {
        "value": 1,
        "next_version": "2026.11.0",
        "tone": "warn",
    }
    assert c["log_critical"]["value"] == 2 and c["log_critical"]["tone"] == "bad"
    assert c["last_backup"]["value"] == pytest.approx(
        29.1, abs=0.1
    )  # 02:54Z on the 24th vs 09:00 CET (08:00Z) on the 25th
    assert c["last_backup"]["tone"] == "ok"
    sec = o["sections"]
    assert sec["backup_failed"]["total"] == 2
    assert sec["updates_pending"]["items"][0]["title"] == "Core"
    assert sec["log_critical"]["items"][0]["detail"] == "boom"
    assert sec["log_critical"]["total"] == 1  # the plain error group is not "critical"
    assert o["ha_version"] == "2026.10.1"


def test_overview_empty_store_is_safe(store):
    o = planning.overview(store, NOW)
    assert o["cards"]["updates_pending"]["value"] is None
    assert o["cards"]["last_backup"]["tone"] == "idle"
    assert o["cards"]["backup_failed"]["value"] == 0


def test_sections_are_capped_at_ten_worst_first(store):
    for n in range(14):
        sev = "critical" if n == 13 else "warning"
        add(store, "integration", f"e{n}", f"Integration {n}", sev, domain=f"d{n}")
    sec = planning.overview(store, NOW)["sections"]["integrations_failed"]
    assert sec["total"] == 14 and len(sec["items"]) == 10
    assert sec["items"][0]["severity"] == "critical"


def test_old_backup_is_flagged_red(store):
    store.set_facts({"backup_last_success": "2026-10-20T02:54:22+00:00"})
    assert planning.overview(store, NOW)["cards"]["last_backup"]["tone"] == "bad"


def test_plan_tasks_selection_and_estimates(store):
    seed(store)
    client = CloudClient(store)
    plan = planning.build_plan(store, client)
    ids = [t["id"] for t in plan["tasks"]]
    assert ids[0] == "integration:fritz"  # deadline first
    assert plan["lanes"]["quick_win"] == ["chore:batteries"]
    t = plan["tasks"][0]
    assert t["selected"] is False and t["proposal"] is None
    assert t["estimate"]["tokens_in"] > 3000 and t["estimate"]["usd"] > 0
    est = plan["estimates"]
    assert est["proposals_all_pending"]["tasks"] == len(ids)
    assert est["proposals_selected_pending"]["tasks"] == 0
    assert est["budget"] == {"limit_usd": 20.0, "spent_usd": 0.0, "remaining_usd": 20.0}
    assert est["model"] == "claude-sonnet-5-5" and plan["dry_run"] is True
    assert any("Qwen" in a for a in est["assumptions"])

    planning.select_task(store, "chore:batteries", True)
    again = planning.build_plan(store, client)
    assert next(x for x in again["tasks"] if x["id"] == "chore:batteries")["selected"]
    assert again["estimates"]["proposals_selected_pending"]["tasks"] == 1
    planning.select_task(store, "chore:batteries", False)
    assert not planning.build_plan(store, client)["estimates"][
        "proposals_selected_pending"
    ]["tasks"]


def test_hard_tasks_cost_more_than_quick_ones(store):
    add(store, "entity_group", "light", "light: 9 of 20", domain="light")
    add(store, "battery", "sensor.b", "Battery 5%", level=5)
    tasks = {t["id"]: t for t in planning.build_plan(store)["tasks"]}
    assert (
        tasks["entities:light"]["estimate"]["usd"]
        > tasks["chore:batteries"]["estimate"]["usd"]
    )


class FakeSDK:
    def __init__(self, reply):
        self.reply, self.requests = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=3000,
                output_tokens=700,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
        )


GOOD = {
    "summary": "Re-authenticate the integration",
    "steps": ["Open the integration", "Re-enter credentials"],
    "risk": "low",
    "breaking_change_check": "Read the 2026.11 release notes",
    "rollback": "Restore the previous config entry",
    "verification": ["Entry shows loaded"],
    "needs_user": True,
    "user_action": "Enter the password yourself",
}


def live(store, reply):
    store.update_settings({"dry_run": False})
    sdk = FakeSDK(json.dumps(GOOD) if reply is None else reply)
    return CloudClient(store, sdk_client=sdk), sdk


def test_proposal_only_on_request_and_dry_run_is_free(store):
    seed(store)
    sdk = FakeSDK("{}")
    client = CloudClient(store, sdk_client=sdk, key_resolver=lambda: 1 / 0)
    out = planning.generate_proposal(store, client, "integration:fritz")
    assert out["status"] == "dry_run" and out["estimate"]["usd"] > 0
    assert sdk.requests == [] and store.plan_proposals() == {}
    assert planning.generate_proposal(store, client, "nope")["status"] == "unknown_task"


def test_proposal_live_is_stored_validated_and_redacted(store):
    add(store, "repair", "smtp/a", f"smtp password: {FAKE_PW}", domain="smtp")
    add(store, "repair", "smtp/b", "mail to jane.doe@example.com failed", domain="smtp")
    client, sdk = live(store, None)
    out = planning.generate_proposal(store, client, "integration:smtp")
    assert out["status"] == "ok" and out["proposal"]["risk"] == "low"
    sent = json.dumps(sdk.requests[0])
    assert FAKE_PW not in sent and "jane.doe@example.com" not in sent
    assert sdk.requests[0]["model"] == "claude-sonnet-5-5"
    plan = planning.build_plan(store, client)
    task = next(t for t in plan["tasks"] if t["id"] == "integration:smtp")
    assert task["proposal"]["steps"][0] == "Open the integration"
    assert plan["estimates"]["proposals_all_pending"]["tasks"] == 0
    # a new issue in the cluster makes the proposal stale
    add(store, "repair", "smtp/c", "another", domain="smtp")
    task = next(
        t
        for t in planning.build_plan(store, client)["tasks"]
        if t["id"] == "integration:smtp"
    )
    assert task["proposal"] is None and task["proposal_stale"] is True


@pytest.mark.parametrize(
    "reply", ["nope", json.dumps({**GOOD, "risk": "extreme"}), "[]"]
)
def test_bad_proposals_are_rejected(store, reply):
    seed(store)
    client, _ = live(store, reply)
    out = planning.generate_proposal(store, client, "integration:fritz")
    assert out["status"] == "bad_reply" and store.plan_proposals() == {}


def test_manual_proposal_still_allowed_past_the_75_percent_pause(store):
    seed(store)
    store.add_usage(
        "claude-sonnet-5-5",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=16.0,
    )
    import time

    client, _ = live(store, None)
    client._clock = time.time
    out = planning.generate_proposal(store, client, "integration:fritz")
    assert out["status"] == "ok"


def test_plan_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    store_mod.reset_store_for_tests()
    seed(store_mod.get_store())
    app = FastAPI()
    app.include_router(ha_agents_router)
    http = TestClient(app)

    assert (
        http.get("/v1/ha/agents/overview").json()["cards"]["backup_failed"]["value"]
        == 2
    )
    plan = http.get("/v1/ha/agents/plan").json()
    assert plan["tasks"][0]["id"] == "integration:fritz"
    sel = http.put(
        "/v1/ha/agents/plan/select", json={"id": "chore:batteries", "selected": True}
    )
    assert next(t for t in sel.json()["tasks"] if t["id"] == "chore:batteries")[
        "selected"
    ]
    assert (
        http.put(
            "/v1/ha/agents/plan/select", json={"id": "x", "selected": "yes"}
        ).status_code
        == 422
    )
    prop = http.post("/v1/ha/agents/plan/proposal", json={"id": "integration:fritz"})
    assert prop.json()["status"] == "dry_run"
    assert (
        http.post("/v1/ha/agents/plan/proposal", json={"id": "nope"}).status_code == 404
    )
    assert http.post("/v1/ha/agents/plan/proposal", json={}).status_code == 422
    # still no way to apply anything
    for path in ("apply", "approve", "execute"):
        assert http.post(f"/v1/ha/agents/plan/{path}", json={}).status_code in (
            404,
            405,
        )
