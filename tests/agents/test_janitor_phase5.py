"""Phase 5: Telegram bot, approval, executor (simulation, live, Haiku operator)."""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest

from openjarvis.agents import janitor_executor as executor
from openjarvis.agents import planning
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import SettingsError, Store
from openjarvis.agents.telegram_bot import BotService, JanitorBot, TelegramError
from openjarvis.agents.watchdog import Observation

OWNER, STRANGER = 8259290562, 12345
FAKE_PW = "hunter2-very-secret-pw"
BACKUP_FRESH = "2099-01-01T00:00:00+00:00"

INSTRUCTION = {
    "preconditions": ["A fresh backup exists"],
    "steps": [
        {
            "tool": "ha_call_write_tool",
            "args": {
                "name": "ha_reload_config_entry",
                "arguments": {"entry_id": "abc"},
            },
            "purpose": "reload the entry",
            "verify": "state is loaded",
        },
        {
            "tool": "ha_call_write_tool",
            "args": {
                "name": "ha_set_entity",
                "arguments": {"entity_id": "sensor.x", "name": "X"},
            },
            "purpose": "rename",
            "verify": "",
        },
    ],
    "rollback_steps": [
        {
            "tool": "ha_call_write_tool",
            "args": {
                "name": "ha_set_entity",
                "arguments": {"entity_id": "sensor.x", "name": "old"},
            },
            "purpose": "undo rename",
            "verify": "",
        }
    ],
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    s = Store(tmp_path / "janitor.db")
    o = Observation(
        "integration",
        "e1",
        "Integration fritz: setup_retry",
        "warning",
        {"domain": "fritz"},
    )
    s.upsert_issue(
        o.fingerprint,
        category="integration",
        source_id="e1",
        title=o.title,
        meta={"domain": "fritz"},
        now=1.0,
    )
    cl = planning.plan_clusters(s)[0]
    body = {
        "risk": "medium",
        "summary": "Reload and rename",
        "instruction": copy.deepcopy(INSTRUCTION),
    }
    s.put_plan_proposal(
        cl["id"], cl["members_hash"], body, model="claude-sonnet-5-5", cost_usd=0.1
    )
    return s


def set_exec(store, **patch):
    cfg = {**store.settings()["executor"], **patch}
    store.update_settings({"executor": cfg})


def approved(store, cid="integration:fritz"):
    out = executor.request_approval(store, cid)
    assert out["status"] == "awaiting_approval", out
    e = store.execution(out["id"])
    assert (
        executor.decide(store, out["id"], e["instruction_hash"][:8], True)["status"]
        == "approved"
    )
    return out["id"]


class FakeHA:
    def __init__(self, fail_on=None):
        self.calls, self.closed, self.fail_on = [], False, fail_on

    def call(self, tool, args):
        self.calls.append((tool, args))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError(f"boom password: {FAKE_PW}")
        return f"ok {args.get('name')}"

    def close(self):
        self.closed = True


# --- validation and approval ---


def test_instruction_rules(store):
    cfg = store.settings()["executor"]
    assert executor.execution_problems(INSTRUCTION, cfg) == []
    assert executor.execution_problems({"steps": []}, cfg) == [
        "the proposal has no executable steps"
    ]
    bad = copy.deepcopy(INSTRUCTION)
    bad["steps"][0]["args"]["name"] = "ha_delete_automation"
    bad["steps"][1]["tool"] = "ha_call_delete_tool"
    bad["rollback_steps"][0]["args"]["name"] = "ha_restart_core"
    msgs = " ".join(executor.execution_problems(bad, cfg))
    assert "deny list" in msgs and "not allowed" in msgs and "rollback_steps[0]" in msgs


def test_request_needs_a_current_executable_proposal(store):
    assert executor.request_approval(store, "nope")["status"] == "no_proposal"
    out = executor.request_approval(store, "integration:fritz")
    assert out["status"] == "awaiting_approval"
    assert (
        executor.request_approval(store, "integration:fritz")["status"]
        == "already_active"
    )
    e = store.execution(out["id"])
    assert e["instruction"] == INSTRUCTION and e["status"] == "awaiting_approval"
    # a new issue in the cluster makes the proposal stale
    o = Observation(
        "integration", "e2", "Integration fritz 2", "warning", {"domain": "fritz"}
    )
    store.upsert_issue(
        o.fingerprint,
        category="integration",
        source_id="e2",
        title="t",
        meta={"domain": "fritz"},
        now=2.0,
    )
    executor.cancel(store, out["id"])
    assert (
        executor.request_approval(store, "integration:fritz")["status"]
        == "stale_proposal"
    )


def test_non_executable_proposals_are_refused_before_asking(store):
    cl = planning.plan_clusters(store)[0]
    body = {
        "risk": "low",
        "summary": "s",
        "instruction": {"steps": [], "rollback_steps": [], "preconditions": []},
    }
    store.put_plan_proposal(cl["id"], cl["members_hash"], body, model="m", cost_usd=0)
    assert executor.request_approval(store, cl["id"])["problems"] == [
        "the proposal has no executable steps"
    ]


def test_decide_rules(store):
    eid = executor.request_approval(store, "integration:fritz")["id"]
    h = store.execution(eid)["instruction_hash"][:8]
    assert executor.decide(store, eid, "deadbeef", True)["status"] == "hash_mismatch"
    assert executor.decide(store, 999, h, True)["status"] == "unknown"
    late = time.time() + executor.APPROVAL_TTL_S + 5
    assert executor.decide(store, eid, h, True, now=late)["status"] == "expired"
    assert executor.decide(store, eid, h, True)["status"] == "not_waiting"
    eid2 = store.create_execution(
        "c",
        label="l",
        members_hash="h",
        instruction=INSTRUCTION,
        instruction_hash="aabbccdd11223344",
        risk="low",
        summary="",
        live=False,
    )
    assert executor.decide(store, eid2, "aabbccdd", False)["status"] == "rejected"
    assert store.execution(eid2)["status"] == "rejected"


# --- running ---


def test_simulation_is_the_default_and_touches_nothing(store):
    eid = approved(store)
    made = []
    out = executor.run_execution(
        store, eid, ha_factory=lambda t: made.append(t) or FakeHA()
    )
    assert out["status"] == "simulated" and made == []
    e = store.execution(eid)
    assert e["live"] == 0 and any("simulated step 1" in x["event"] for x in e["log"])


def test_live_python_operator_runs_the_approved_steps_once_and_in_order(store):
    set_exec(store, live=True, operator="python")
    store.set_facts({"backup_last_success": BACKUP_FRESH})
    ha = FakeHA()
    seen_timeout = []
    out = executor.run_execution(
        store, approved(store), ha_factory=lambda t: seen_timeout.append(t) or ha
    )
    assert out["status"] == "done" and ha.closed
    assert [c[1]["name"] for c in ha.calls] == [
        "ha_reload_config_entry",
        "ha_set_entity",
    ]
    assert seen_timeout == [660.0]


def test_live_needs_a_fresh_backup(store):
    set_exec(store, live=True, operator="python")
    eid = approved(store)
    out = executor.run_execution(store, eid, ha_factory=lambda t: FakeHA())
    assert out["status"] == "failed" and "backup" in out["detail"]
    set_exec(store, live=True, operator="python", require_backup=False)
    eid2 = approved(store)
    ha = FakeHA()
    assert (
        executor.run_execution(store, eid2, ha_factory=lambda t: ha)["status"] == "done"
    )


def test_failed_step_stops_and_rolls_back_without_leaking_secrets(store):
    set_exec(store, live=True, operator="python")
    store.set_facts({"backup_last_success": BACKUP_FRESH})
    ha = FakeHA(fail_on=2)
    out = executor.run_execution(store, approved(store), ha_factory=lambda t: ha)
    assert out["status"] == "rolled_back"
    assert [c[1]["name"] for c in ha.calls] == [
        "ha_reload_config_entry",
        "ha_set_entity",
        "ha_set_entity",
    ]
    assert ha.calls[2][1]["arguments"]["name"] == "old"
    assert FAKE_PW not in json.dumps(store.executions(1)[0]["log"])


def test_tampering_after_approval_is_caught(store):
    set_exec(store, live=True, operator="python")
    store.set_facts({"backup_last_success": BACKUP_FRESH})
    eid = approved(store)
    tampered = copy.deepcopy(INSTRUCTION)
    tampered["steps"][0]["args"]["arguments"]["entry_id"] = "evil"
    store._run(
        "UPDATE executions SET instruction=? WHERE id=?", (json.dumps(tampered), eid)
    )
    ha = FakeHA()
    out = executor.run_execution(store, eid, ha_factory=lambda t: ha)
    assert out["status"] == "failed" and "changed" in out["detail"] and ha.calls == []


def test_only_approved_executions_run(store):
    eid = executor.request_approval(store, "integration:fritz")["id"]
    assert executor.run_execution(store, eid)["status"] == "not_approved"


# --- the Haiku operator ---


def usage():
    return SimpleNamespace(
        input_tokens=500,
        output_tokens=60,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


class Script:
    def __init__(self, *calls):
        self.calls, self.requests = list(calls), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(json.loads(json.dumps(req, default=str)))
        n = min(len(self.requests) - 1, len(self.calls) - 1)
        name, inp = self.calls[n]
        block = SimpleNamespace(type="tool_use", id=f"t{n}", name=name, input=inp)
        return SimpleNamespace(usage=usage(), content=[block], stop_reason="tool_use")


def haiku_run(store, script, ha=None):
    set_exec(store, live=True, operator="haiku")
    store.update_settings({"dry_run": False})
    store.set_facts({"backup_last_success": BACKUP_FRESH})
    ha = ha or FakeHA()
    eid = approved(store)
    out = executor.run_execution(
        store,
        eid,
        client=CloudClient(store, sdk_client=script),
        ha_factory=lambda t: ha,
    )
    return out, ha, eid


def test_haiku_operates_but_python_supplies_tool_and_arguments(store):
    script = Script(
        (
            "run_step",
            {"index": 0, "tool": "ha_call_delete_tool", "args": {"name": "everything"}},
        ),
        ("run_step", {"index": 1}),
        ("finish", {"ok": True, "report": "Reloaded and renamed."}),
    )
    out, ha, _ = haiku_run(store, script)
    assert out["status"] == "done" and out["detail"] == "Reloaded and renamed."
    assert [c[0] for c in ha.calls] == ["ha_call_write_tool", "ha_call_write_tool"]
    assert ha.calls[0][1] == INSTRUCTION["steps"][0]["args"]  # not what the model typed
    assert all(r["model"] == "claude-haiku-5-5" for r in script.requests)


def test_haiku_cannot_skip_ahead_and_three_refusals_abort(store):
    script = Script(("run_step", {"index": 1}))
    out, ha, eid = haiku_run(store, script)
    assert ha.calls == [] and out["status"] == "failed"
    assert "out of order" in out["detail"]
    assert any(x["event"] == "abort" for x in store.execution(eid)["log"])


def test_haiku_saying_ok_does_not_make_an_incomplete_run_done(store):
    script = Script(
        ("run_step", {"index": 0}), ("finish", {"ok": True, "report": "All good"})
    )
    out, ha, _ = haiku_run(store, script)
    assert len(ha.calls) == 1 and out["status"] == "failed"


def test_haiku_failure_path_rolls_back_once(store):
    script = Script(
        ("run_step", {"index": 0}),
        ("run_step", {"index": 1}),
        ("rollback", {}),
        ("finish", {"ok": False, "report": "undone"}),
    )
    out, ha, _ = haiku_run(store, script, FakeHA(fail_on=2))
    assert out["status"] == "rolled_back"
    assert len([c for c in ha.calls if c[1]["arguments"].get("name") == "old"]) == 1


def test_live_with_cloud_dry_run_cannot_use_haiku(store):
    set_exec(store, live=True, operator="haiku")
    store.set_facts({"backup_last_success": BACKUP_FRESH})
    eid = approved(store)
    ha = FakeHA()
    out = executor.run_execution(
        store,
        eid,
        client=CloudClient(store, sdk_client=Script(("finish", {}))),
        ha_factory=lambda t: ha,
    )
    assert out["status"] == "failed" and "dry-run" in out["detail"] and ha.calls == []


# --- the bot ---


class FakeApi:
    def __init__(self):
        self.sent, self.calls, self.queue = [], [], []
        self.fail = False

    def call(self, method, params, timeout=30):
        self.calls.append((method, params))
        if self.fail:
            raise TelegramError(f"{method}: HTTP 403 blocked")
        if method == "sendMessage":
            self.sent.append(params)
            return {"message_id": 100 + len(self.sent)}
        if method == "getUpdates":
            ups, self.queue = self.queue, []
            return ups
        if method == "getMe":
            return {"username": "jarvis_the_janitor_bot"}
        return True


def msg(uid, text, uid_n=1):
    return {"update_id": uid_n, "message": {"from": {"id": uid}, "text": text}}


def press(uid, data, n=2):
    return {
        "update_id": n,
        "callback_query": {
            "id": "cb",
            "from": {"id": uid},
            "data": data,
            "message": {"message_id": 101},
        },
    }


def bot_for(store):
    api = FakeApi()
    return JanitorBot(store, api, run_in_background=lambda fn: fn()), api


def test_only_the_owner_gets_answers(store):
    bot, api = bot_for(store)
    store.add_report("ok", "Janitor: OK", {})
    api.queue = [
        msg(STRANGER, "/status", 1),
        msg(OWNER, "/status", 2),
        msg(OWNER, "/whatever", 3),
    ]
    assert bot.poll_once(0) == 3
    assert [m["text"] for m in api.sent] == ["Janitor: OK"]
    assert store.facts()["tg_offset"] == 4
    api.queue = [msg(OWNER, "/start", 4), msg(OWNER, "/plan", 5)]
    bot.poll_once(0)
    assert (
        "nothing else can approve" in api.sent[1]["text"]
        and "fritz" in api.sent[2]["text"].lower()
    )


def test_approval_flow_end_to_end_in_telegram(store):
    bot, api = bot_for(store)
    eid = executor.request_approval(store, "integration:fritz")["id"]
    bot.request_approval(eid)
    sent = api.sent[0]
    assert "Approval needed" in sent["text"] and "SIMULATION" in sent["text"]
    assert "ha_reload_config_entry" in sent["text"]
    buttons = sent["reply_markup"]["inline_keyboard"][0]
    approve = buttons[0]["callback_data"]
    assert approve.startswith(f"ap:{eid}:")
    # a stranger pressing the button does nothing
    api.queue = [press(STRANGER, approve)]
    bot.poll_once(0)
    assert store.execution(eid)["status"] == "awaiting_approval"
    # the owner approves: simulation runs and the result comes back
    api.queue = [press(OWNER, approve)]
    bot.poll_once(0)
    assert store.execution(eid)["status"] == "simulated"
    assert "simulation complete" in api.sent[-1]["text"]
    # pressing again does nothing
    api.queue = [press(OWNER, approve, 3)]
    bot.poll_once(0)
    assert any(
        c[0] == "answerCallbackQuery" and "Already" in c[1]["text"] for c in api.calls
    )


def test_reject_and_garbage_buttons(store):
    bot, api = bot_for(store)
    eid = executor.request_approval(store, "integration:fritz")["id"]
    h = store.execution(eid)["instruction_hash"][:8]
    api.queue = [press(OWNER, "zz:1:2"), press(OWNER, f"ap:{eid}:badhash", 3)]
    bot.poll_once(0)
    assert store.execution(eid)["status"] == "awaiting_approval"
    api.queue = [press(OWNER, f"rj:{eid}:{h}", 4)]
    bot.poll_once(0)
    assert store.execution(eid)["status"] == "rejected"


def test_messages_hide_secrets_but_keep_entity_ids(store):
    bot, api = bot_for(store)
    ins = copy.deepcopy(INSTRUCTION)
    ins["steps"][0]["args"]["arguments"]["password"] = FAKE_PW
    eid = store.create_execution(
        "c",
        label="L",
        members_hash="h",
        instruction=ins,
        instruction_hash="aabbccdd11223344",
        risk="low",
        summary="",
        live=False,
    )
    bot.request_approval(eid)
    text = api.sent[0]["text"]
    assert FAKE_PW not in text and "sensor.x" in text
    assert "light.kitchen" in Redactor.redact_secrets("light.kitchen token=abcdefghijk")


def test_unsolicited_messages_respect_quiet_hours_always(store):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    bot, api = bot_for(store)
    night = datetime(2026, 10, 25, 23, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    noon = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    assert (
        bot.send_unsolicited("CRITICAL: lock down", night) is False and api.sent == []
    )
    assert bot.send_unsolicited("digest", noon) is True and len(api.sent) == 1


def test_digest_adds_the_verified_readout(store):
    bot, api = bot_for(store)
    assert bot.send_digest()["status"] == "no_report"
    rid = store.add_report("ok", "Janitor: OK", {})
    store.add_readout(
        report_id=rid,
        model="q",
        tokens_in=1,
        tokens_out=1,
        seconds=1,
        text="All is fine.",
        guard_ok=True,
        note="",
    )
    assert bot.send_digest()["status"] == "sent"
    assert api.sent[0]["text"] == "Janitor: OK\n\nAll is fine."


def test_service_does_not_start_without_a_token_and_never_crashes(store):
    import asyncio

    bot, api = bot_for(store)
    api.fail = True
    svc = BotService(bot)

    async def go(monkeypatch_env):
        import os

        os.environ["JARVIS_TELEGRAM"] = "1"
        try:
            assert svc.start() is True
            await asyncio.sleep(0.05)
            assert svc.running is False and "403" in (bot.last_error or "")
        finally:
            os.environ["JARVIS_TELEGRAM"] = "0"
            await svc.stop()

    asyncio.run(go(None))


# --- settings and API ---


def test_defaults_are_safe_and_validated(store):
    s = store.settings()
    assert s["executor"]["live"] is False and s["executor"]["operator"] == "haiku"
    assert s["executor"]["require_backup"] is True and s["executor"][
        "allowed_tools"
    ] == ["ha_call_write_tool"]
    assert (
        s["telegram"]["alerts"] is False and s["telegram"]["allowed_user_id"] == OWNER
    )
    good = s["executor"]
    for bad in (
        {**good, "operator": "gpt"},
        {**good, "allowed_tools": ["rm"]},
        {**good, "deny_names_regex": "("},
        {**good, "step_timeout_s": 1},
        {"live": True},
    ):
        with pytest.raises(SettingsError):
            store.update_settings({"executor": bad})
    for bad in (
        {**s["telegram"], "allowed_user_id": -1},
        {**s["telegram"], "digest": "yes"},
        {"enabled": True},
    ):
        with pytest.raises(SettingsError):
            store.update_settings({"telegram": bad})


def test_api_has_no_way_to_approve(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server import ha_agents_routes as routes

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    store_mod.reset_store_for_tests()
    st = store_mod.get_store()
    o = Observation(
        "integration", "e1", "Integration fritz", "warning", {"domain": "fritz"}
    )
    st.upsert_issue(
        o.fingerprint,
        category="integration",
        source_id="e1",
        title="t",
        meta={"domain": "fritz"},
        now=1.0,
    )
    cl = planning.plan_clusters(st)[0]
    st.put_plan_proposal(
        cl["id"],
        cl["members_hash"],
        {"risk": "low", "summary": "s", "instruction": copy.deepcopy(INSTRUCTION)},
        model="m",
        cost_usd=0,
    )
    bot, api = bot_for(st)
    monkeypatch.setattr(
        routes,
        "get_bot_service",
        lambda: SimpleNamespace(bot=bot, status=lambda: {"running": True}),
    )
    app = FastAPI()
    app.include_router(routes.ha_agents_router)
    http = TestClient(app)

    out = http.post(
        "/v1/ha/agents/executions/request", json={"cluster_id": "integration:fritz"}
    ).json()
    assert out["status"] == "awaiting_approval" and len(api.sent) == 1
    ex = http.get("/v1/ha/agents/executions").json()
    assert (
        ex["executions"][0]["status"] == "awaiting_approval"
        and ex["executor"]["live"] is False
    )
    for path in ("approve", "decide", "run", "execute"):
        assert http.post(
            f"/v1/ha/agents/executions/{out['id']}/{path}", json={}
        ).status_code in (404, 405)
    assert http.post("/v1/ha/agents/executions/request", json={}).status_code == 422
    assert (
        http.post(f"/v1/ha/agents/executions/{out['id']}/cancel").json()["status"]
        == "cancelled"
    )
    assert http.post("/v1/ha/agents/telegram/test").json()["status"] == "sent"
    # Telegram down: the request is withdrawn, never left dangling
    api.fail = True
    http.post(
        "/v1/ha/agents/executions/request", json={"cluster_id": "integration:fritz"}
    )
    assert st.executions(1)[0]["status"] == "cancelled"
