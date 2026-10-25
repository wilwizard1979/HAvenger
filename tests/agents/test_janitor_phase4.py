"""Phase 4: escalation (Sonnet / Opus), read-only lookups, detailed instruction."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openjarvis.agents import escalation, planning
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.escalation import ReadOnlyHA, validate_instruction
from openjarvis.agents.store import SettingsError, Store
from openjarvis.agents.watchdog import Observation

FAKE_PW = "hunter2-very-secret-pw"

FINAL = {
    "summary": "Re-authenticate the integration",
    "diagnosis": "Token expired",
    "risk": "medium",
    "breaking_change_check": "Read the release notes first",
    "needs_user": False,
    "user_action": "",
    "instruction": {
        "preconditions": ["A fresh backup exists"],
        "steps": [
            {
                "tool": "ha_call_write_tool",
                "args": {
                    "name": "ha_reload_config_entry",
                    "arguments": {"entry_id": "abc"},
                },
                "purpose": "reload the entry",
                "verify": "entry state is loaded",
            },
            {"tool": "rm -rf /", "args": {}},
            {"tool": "ha_set_x", "args": "not a dict"},
        ],
        "rollback_steps": [],
    },
    "verification": ["Entry shows loaded"],
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    return Store(tmp_path / "janitor.db")


def seed(store, title="Integration fritz: setup_retry"):
    o = Observation("integration", "e1", title, "warning", {"domain": "fritz"})
    store.upsert_issue(
        o.fingerprint,
        category="integration",
        source_id="e1",
        title=title,
        meta={"domain": "fritz"},
        now=1.0,
    )


def usage():
    return SimpleNamespace(
        input_tokens=2000,
        output_tokens=300,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


class ScriptSDK:
    """Answers with the scripted content blocks, one entry per call."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(json.loads(json.dumps(req, default=str)))
        blocks = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        return SimpleNamespace(usage=usage(), content=blocks, stop_reason="end_turn")


def text(t):
    return [SimpleNamespace(type="text", text=t)]


def tool(name, **inp):
    return [SimpleNamespace(type="tool_use", id=f"tu_{name}", name=name, input=inp)]


class FakeHA:
    def __init__(self, reply="{}"):
        self.calls, self.reply, self.closed = [], reply, False

    def call(self, name, args):
        self.calls.append((name, args))
        return self.reply

    def close(self):
        self.closed = True


def live(store, sdk):
    store.update_settings({"dry_run": False})
    return CloudClient(store, sdk_client=sdk)


# --- read-only wall --------------------------------------------------------------


def test_only_allow_listed_reads_reach_home_assistant():
    boom = SimpleNamespace(_client=SimpleNamespace(call_tool=lambda *a: 1 / 0))
    ro = ReadOnlyHA(boom)
    for bad in (
        "ha_call_write_tool",
        "ha_call_delete_tool",
        "ha_eval_template",
        "ha_set_entity",
    ):
        assert "not allowed" in ro.call(bad, {})
    assert set(escalation.DIAG_TOOLS) == {
        "ha_search_tools",
        "ha_get_state",
        "ha_search",
        "ha_get_integration",
        "ha_get_logs",
        "ha_get_automation_traces",
    }
    # an allowed tool that fails is reported to the model, not raised
    assert "error" in ro.call("ha_get_state", {"entity_id": ["x"]})


def test_tool_discovery_is_called_directly_and_read_only():
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return {"content": [{"text": '[{"name": "ha_reload_config_entry"}]'}]}

    ro = ReadOnlyHA(SimpleNamespace(_client=SimpleNamespace(call_tool=call_tool)))
    assert "ha_reload_config_entry" in ro.call("ha_search_tools", {"query": "reload"})
    assert seen == [("ha_search_tools", {"query": "reload"})]
    assert "ha_search_tools" in escalation.SYSTEM and "never guess" in escalation.SYSTEM


def test_reads_go_through_the_read_proxy_and_are_truncated():
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return {"content": [{"text": "x" * 20000}]}

    ro = ReadOnlyHA(SimpleNamespace(_client=SimpleNamespace(call_tool=call_tool)))
    out = ro.call("ha_search", {"query": "fritz"})
    assert seen == [
        ("ha_call_read_tool", {"name": "ha_search", "arguments": {"query": "fritz"}})
    ]
    assert len(out) == escalation.TOOL_RESULT_CHARS


def test_instruction_is_only_shape_checked_and_junk_is_dropped():
    out = validate_instruction(FINAL["instruction"])
    assert [s["tool"] for s in out["steps"]] == ["ha_call_write_tool"]
    assert out["preconditions"] == ["A fresh backup exists"]
    assert validate_instruction("junk") == {
        "preconditions": [],
        "steps": [],
        "rollback_steps": [],
    }
    many = {"steps": [{"tool": "ha_x", "args": {}} for _ in range(40)]}
    assert len(validate_instruction(many)["steps"]) == escalation.MAX_STEPS


# --- the loop ----------------------------------------------------------------------


def test_dry_run_escalation_sends_nothing_and_returns_an_estimate(store):
    seed(store)
    sdk = ScriptSDK(text("{}"))
    client = CloudClient(store, sdk_client=sdk, key_resolver=lambda: 1 / 0)
    out = planning.generate_proposal(
        store, client, "integration:fritz", "investigate", FakeHA()
    )
    assert out["status"] == "dry_run" and out["estimate"]["usd"] > 0
    assert sdk.requests == [] and store.plan_proposals() == {}


def test_investigate_looks_around_then_stores_a_proposal_with_instruction(store):
    seed(store, f"Integration fritz: password: {FAKE_PW}")
    sdk = ScriptSDK(
        tool("ha_get_integration", domain="fritz"),
        text(json.dumps(FINAL)),
    )
    ha = FakeHA(f'{{"entries": [], "note": "password: {FAKE_PW}"}}')
    out = planning.generate_proposal(
        store, live(store, sdk), "integration:fritz", "investigate", ha
    )
    assert out["status"] == "ok" and out["tool_calls"] == 1
    assert ha.calls == [("ha_get_integration", {"domain": "fritz"})] and ha.closed
    p = store.plan_proposals()["integration:fritz"]
    assert p["body"]["mode"] == "investigate" and p["model"] == "claude-sonnet-5-5"
    assert p["body"]["instruction"]["steps"][0]["args"]["arguments"] == {
        "entry_id": "abc"
    }
    assert p["cost_usd"] > 0
    # the secret never left the machine, neither in the question nor in the tool result
    assert FAKE_PW not in json.dumps(sdk.requests)
    second = sdk.requests[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    # the next plan view shows the proposal and the investigate / deep estimates
    task = next(
        t
        for t in planning.build_plan(store, live(store, sdk))["tasks"]
        if t["id"] == "integration:fritz"
    )
    assert task["proposal"]["risk"] == "medium"
    assert task["estimate_deep"]["usd"] > task["estimate_investigate"]["usd"]


def test_placeholders_in_tool_inputs_are_restored_before_the_lookup(store):
    seed(store, "mail to jane.doe@example.com failed")
    sdk = ScriptSDK(tool("ha_search", query="<email_1>"), text(json.dumps(FINAL)))
    ha = FakeHA()
    planning.generate_proposal(
        store, live(store, sdk), "integration:fritz", "investigate", ha
    )
    assert "jane.doe@example.com" not in json.dumps(sdk.requests)
    assert ha.calls[0][1]["query"] == "jane.doe@example.com"


def test_rounds_are_capped_and_the_last_call_has_no_tools(store):
    seed(store)
    store.update_settings(
        {"escalation": {"max_rounds": 2, "max_usd": 5.0, "opus_max_usd": 5.0}}
    )
    sdk = ScriptSDK(
        tool("ha_get_logs"),
        tool("ha_get_logs"),
        tool("ha_get_logs"),
        text(json.dumps(FINAL)),
    )
    out = planning.generate_proposal(
        store, live(store, sdk), "integration:fritz", "investigate", FakeHA()
    )
    assert len(sdk.requests) == 3 and "tools" not in sdk.requests[-1]
    assert "tools" in sdk.requests[0]
    # the model still asked for a tool on the last call: no proposal could be read
    assert out["status"] == "bad_reply"


def test_cost_cap_forces_the_answer(store):
    seed(store)
    store.update_settings(
        {"escalation": {"max_rounds": 6, "max_usd": 0.0001, "opus_max_usd": 5.0}}
    )
    sdk = ScriptSDK(tool("ha_get_logs"), text(json.dumps(FINAL)))
    out = planning.generate_proposal(
        store, live(store, sdk), "integration:fritz", "investigate", FakeHA()
    )
    assert (
        out["status"] == "ok"
        and len(sdk.requests) == 2
        and "tools" not in sdk.requests[1]
    )


def test_deep_uses_opus(store):
    seed(store)
    sdk = ScriptSDK(text(json.dumps(FINAL)))
    planning.generate_proposal(
        store, live(store, sdk), "integration:fritz", "deep", FakeHA()
    )
    assert sdk.requests[0]["model"] == "claude-opus-5-5"
    assert store.plan_proposals()["integration:fritz"]["body"]["mode"] == "deep"


def test_bad_reply_budget_and_no_key_are_reported_not_raised(store):
    seed(store)
    assert (
        planning.generate_proposal(
            store,
            live(store, ScriptSDK(text("no json"))),
            "integration:fritz",
            "investigate",
            FakeHA(),
        )["status"]
        == "bad_reply"
    )
    assert store.plan_proposals() == {}
    store.update_settings(
        {
            "budget": {
                "monthly_limit_usd": 0.0001,
                "pause_auto_escalation_pct": 75,
                "hard_stop_pct": 100,
            }
        }
    )
    out = planning.generate_proposal(
        store,
        CloudClient(store, sdk_client=ScriptSDK(text("{}"))),
        "integration:fritz",
        "investigate",
        FakeHA(),
    )
    assert out["status"] == "skipped_budget"


def test_unknown_mode_and_task(store):
    seed(store)
    c = CloudClient(store)
    assert (
        planning.generate_proposal(store, c, "integration:fritz", "wild")["status"]
        == "bad_mode"
    )
    assert (
        planning.generate_proposal(store, c, "nope", "investigate")["status"]
        == "unknown_task"
    )


def test_escalation_settings_validation(store):
    good = {"max_rounds": 4, "max_usd": 1.0, "opus_max_usd": 3.0}
    assert store.update_settings({"escalation": good})["escalation"] == good
    for bad in ({**good, "max_rounds": 99}, {**good, "max_usd": -1}, {"max_rounds": 1}):
        with pytest.raises(SettingsError):
            store.update_settings({"escalation": bad})


def test_nothing_schedules_an_escalation(store):
    seed(store)
    from openjarvis.agents import reporting

    sdk = ScriptSDK(text(json.dumps({"clusters": []})))
    reporting.generate_report(store, live(store, sdk), now=5.0)
    models = {r["model"] for r in sdk.requests}
    assert models <= {"claude-haiku-5-5"}


def test_api_modes(tmp_path, monkeypatch):
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
    ok = http.post(
        "/v1/ha/agents/plan/proposal",
        json={"id": "integration:fritz", "mode": "investigate"},
    )
    assert ok.json()["status"] == "dry_run" and ok.json()["estimate"]["rounds"] == 6
    deep = http.post(
        "/v1/ha/agents/plan/proposal", json={"id": "integration:fritz", "mode": "deep"}
    )
    assert deep.json()["estimate"]["usd"] > ok.json()["estimate"]["usd"]
    assert (
        http.post(
            "/v1/ha/agents/plan/proposal", json={"id": "integration:fritz", "mode": "x"}
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/v1/ha/agents/plan/proposal", json={"id": "integration:fritz"}
        ).json()["status"]
        == "dry_run"
    )
