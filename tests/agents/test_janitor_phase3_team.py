"""Phase 3d: Qwen read-out job, team overview, nothing escalates by itself."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import readout, reporting
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.store import Store
from openjarvis.agents.team import team_overview
from openjarvis.agents.watchdog import Observation, WatchdogService

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
REPORT = (
    "Janitor Sun 25 Oct: ATTENTION\n"
    "Open issues 41 (critical 0, warning 41, info 0) in 8 clusters\n"
    "Budget: $0.00 of $20.00 this month"
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    s = Store(tmp_path / "janitor.db")
    s.add_report("attention", REPORT, {}, ts=NOW)
    return s


def gen(text):
    def _g(model, system, source):
        return {"text": text, "tokens_in": 100, "tokens_out": 40}

    return _g


def test_guard_accepts_faithful_and_rejects_invented_numbers():
    assert readout.guard("There are 41 open issues in 8 clusters.", REPORT)
    assert readout.guard("Budget is 0.00 of 20.00 dollars.", REPORT)
    assert not readout.guard("There are 14 open issues.", REPORT)
    # spelled-out numbers count too: "three" is not in the report, "forty one" is 41
    assert not readout.guard("You have three quick wins.", REPORT)
    assert readout.guard("Forty one issues in eight clusters.", REPORT)
    assert readout.guard("The budget is twenty dollars.", REPORT)
    assert not readout.guard("Eighty open issues.", REPORT)
    assert not readout.guard("", REPORT)
    assert not readout.guard("x" * 700, REPORT)


def test_readout_stores_spoken_text_and_a_free_run(store):
    out = readout.run_readout(
        store, generate=gen("Forty one issues in 8 clusters, 41 warnings."), now=NOW
    )
    assert out["status"] == "ok"
    r = store.latest_readout()
    assert r["guard_ok"] == 1 and r["text"].startswith("Forty one")
    run = store.runs_by_layer("L1", 1)[0]
    assert run["cost_usd"] == 0 and run["input_tokens"] == 100


def test_readout_falls_back_to_the_report_when_qwen_invents_numbers(store):
    out = readout.run_readout(store, generate=gen("There are 99 issues."), now=NOW)
    assert out["status"] == "guard_failed"
    r = store.latest_readout()
    assert r["guard_ok"] == 0 and r["text"] == REPORT and "invented" in r["note"]


def test_readout_survives_qwen_being_down_or_no_report(tmp_path, store):
    def boom(*a):
        raise OSError("connection refused")

    assert readout.run_readout(store, generate=boom)["status"] == "unavailable"
    assert (
        readout.run_readout(Store(tmp_path / "empty.db"), generate=gen("x"))["status"]
        == "no_report"
    )


def test_nothing_escalates_by_default(store):
    s = store.settings()
    assert s["auto_escalation"] is False and s["dry_run"] is True


class FakeSDK:
    def __init__(self, reply):
        self.reply, self.requests = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        return SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=100,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
        )


def test_scheduled_report_never_calls_sonnet_or_opus(store):
    store.update_settings({"dry_run": False})
    o = Observation("repair", "x/1", "t", "warning", {"domain": "x"})
    store.upsert_issue(
        o.fingerprint,
        category="repair",
        source_id="x/1",
        title="t",
        meta={"domain": "x"},
        now=NOW - 3600,
    )
    sdk = FakeSDK(
        json.dumps(
            {
                "clusters": [
                    {
                        "id": "integration:x",
                        "note": "n",
                        "cause": "c",
                        "effort": "quick",
                    }
                ]
            }
        )
    )
    client = CloudClient(store, sdk_client=sdk)
    reporting.generate_report(store, client, now=NOW)
    assert [r["model"] for r in sdk.requests] == ["claude-haiku-5-5"]
    assert store.runs_by_layer("diagnose") == [] and store.runs_by_layer("deep") == []


def test_service_runs_readout_after_the_digest(tmp_path, monkeypatch):
    s = Store(tmp_path / "svc.db")
    called = []
    monkeypatch.setattr(
        readout,
        "ollama_generate",
        lambda m, sy, so: (
            called.append(so) or {"text": "41 issues", "tokens_in": 1, "tokens_out": 1}
        ),
    )
    monkeypatch.setattr(
        readout, "run_readout", lambda store, **k: called.append("readout")
    )
    svc = WatchdogService(s, SimpleNamespace(collect=lambda x: {}), clock=lambda: NOW)
    svc._maybe_report()
    assert called == ["readout"] and s.latest_report() is not None


def test_team_overview_shows_every_layer(store):
    o = Observation("repair", "x/1", "Repair x", "warning", {"domain": "x"})
    store.upsert_issue(
        o.fingerprint,
        category="repair",
        source_id="x/1",
        title="Repair x",
        meta={"domain": "x"},
        now=NOW - 3600,
    )
    store.put_triage(
        "integration:x",
        "h",
        effort="quick",
        note="Check auth",
        cause="token",
        model="claude-haiku-5-5",
    )
    store.put_plan_proposal(
        "integration:x",
        "h",
        {"risk": "low", "summary": "Re-auth"},
        model="claude-sonnet-5-5",
        cost_usd=0.02,
    )
    readout.run_readout(store, generate=gen("41 issues"), now=NOW)
    t = team_overview(
        store,
        {
            "interval_min": 15,
            "next_run": NOW + 60,
            "last_tick": {"ok": True},
            "issues": {"active": 1},
        },
    )
    assert t["dry_run"] is True
    assert t["l0"]["report"]["text"] == REPORT and t["l0"]["interval_min"] == 15
    assert t["l1"]["latest"]["text"] == "41 issues"
    assert t["l2"]["model"] == "claude-haiku-5-5" and t["l2"]["mode"].startswith(
        "dry-run"
    )
    assert t["l2"]["reasoning"][0]["note"] == "Check auth"
    assert t["l3"]["auto_escalation"] is False
    assert (
        t["l3"]["escalated"][0]["risk"] == "low"
        and t["l3"]["escalated"][0]["cost_usd"] == 0.02
    )
    assert t["l4"]["runs"] == []


def test_team_and_readout_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server import ha_agents_routes as routes

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    store_mod.reset_store_for_tests()
    store_mod.get_store().add_report("ok", REPORT, {}, ts=NOW)
    monkeypatch.setattr(readout, "ollama_generate", gen("41 issues"))
    monkeypatch.setattr(
        routes,
        "run_readout",
        lambda st: readout.run_readout(st, generate=gen("41 issues")),
    )
    app = FastAPI()
    app.include_router(routes.ha_agents_router)
    http = TestClient(app)
    assert http.post("/v1/ha/agents/readout/run").json()["status"] == "ok"
    team = http.get("/v1/ha/agents/team").json()
    assert (
        team["l1"]["latest"]["text"] == "41 issues" and team["l0"]["report"]["id"] == 1
    )


def test_readout_gets_one_correction_round_before_falling_back(store):
    calls = []

    def flaky(model, system, source):
        calls.append(source)
        text = (
            "You have three quick wins." if len(calls) == 1 else "There are 41 issues."
        )
        return {"text": text, "tokens_in": 100, "tokens_out": 20}

    out = readout.run_readout(store, generate=flaky, now=NOW)
    assert out["status"] == "ok" and len(calls) == 2
    assert "not in the report" in calls[1] and "3" in calls[1]
    r = store.latest_readout()
    assert r["text"] == "There are 41 issues." and r["tokens_in"] == 200


def test_team_overview_carries_full_proposals_and_the_pilot_switch(store):
    o = Observation("repair", "x/1", "Repair x", "warning", {"domain": "x"})
    store.upsert_issue(
        o.fingerprint,
        category="repair",
        source_id="x/1",
        title="Repair x",
        meta={"domain": "x"},
        now=NOW - 3600,
    )
    from openjarvis.agents.clusters import build_clusters

    h = build_clusters(store.issues(), store.settings())[0]["members_hash"]
    body = {
        "risk": "low",
        "summary": "s",
        "instruction": {"steps": [], "preconditions": [], "rollback_steps": []},
    }
    store.put_plan_proposal(
        "integration:x", h, body, model="claude-sonnet-5-5", cost_usd=0.1
    )
    t = team_overview(store, {"enabled": False, "interval_min": 1440})
    e = t["l3"]["escalated"][0]
    assert e["proposal"]["instruction"]["steps"] == [] and e["current"] is True
    assert t["l0"]["enabled"] is False and t["l0"]["interval_min"] == 1440
    store.upsert_issue(
        o.fingerprint,
        category="repair",
        source_id="x/2",
        title="n",
        meta={"domain": "x"},
        now=NOW,
    )
    o2 = Observation("repair", "x/2", "Repair x2", "warning", {"domain": "x"})
    store.upsert_issue(
        o2.fingerprint,
        category="repair",
        source_id="x/2",
        title="n",
        meta={"domain": "x"},
        now=NOW,
    )
    assert team_overview(store, {})["l3"]["escalated"][0]["current"] is False


def test_a_disabled_watchdog_never_runs_by_itself(store, monkeypatch):
    import asyncio

    monkeypatch.setenv("JARVIS_WATCHDOG", "1")
    store.update_settings(
        {
            "watchdog": {
                "enabled": False,
                "interval_min": 1440,
                "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            }
        }
    )
    calls = []
    svc = WatchdogService(
        store, SimpleNamespace(collect=lambda s: calls.append(1) or {})
    )

    async def go():
        svc.start(first_delay=0.01)
        await asyncio.sleep(0.1)
        await svc.stop()

    asyncio.run(go())
    assert calls == [] and store.latest_report()["id"] == 1  # nothing new built
