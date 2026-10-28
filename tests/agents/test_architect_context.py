"""Architect steps 2-3: context size, disabled automations, intent, Recommend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjarvis.agents import architect as a
from openjarvis.agents import architect_context as ctx
from openjarvis.agents import architect_recommend as rec
from openjarvis.agents.cloud_client import CloudClient
from openjarvis.agents.store import Store


def _item(eid, alias, cfg, state="on"):
    st = {"state": state, "attributes": {"last_triggered": "2026-10-01T10:00:00+00:00"}}
    return a.build_item(eid, st, cfg)


def _inv():
    lamp = "light.lamp_entrada"
    items = [
        _item(
            "automation.front",
            "Monitor: Front Light",
            {
                "id": "1",
                "alias": "Monitor: Front Light",
                "triggers": [
                    {"trigger": "state", "entity_id": lamp, "for": {"minutes": 30}}
                ],
                "actions": [
                    {"action": "light.turn_off", "target": {"entity_id": lamp}}
                ],
            },
        ),
        _item(
            "automation.night",
            "Routine: Night",
            {
                "alias": "Routine: Night",
                "triggers": [{"trigger": "time", "at": "23:00"}],
                "actions": [
                    {"action": "light.turn_on", "target": {"entity_id": lamp}},
                    {
                        "action": "switch.turn_off",
                        "target": {"entity_id": "switch.other"},
                    },
                ],
            },
        ),
        _item(
            "automation.old",
            "Scene: Old",
            {"alias": "Scene: Old", "actions": [{"target": {"entity_id": lamp}}]},
            state="off",
        ),
    ]
    inv = a.analyse(items, {lamp: {"state": "on"}, "switch.other": {"state": "on"}})
    inv["items"] = items
    inv["entity_info"] = {
        lamp: {"name": "Lamp Entrada", "state": "on", "area": "Entryway"}
    }
    return inv


def test_classify():
    assert ctx.classify("Which automations use light.x?") == "lookup"
    assert ctx.classify("extend the front light time to 15 min") == "change"
    assert ctx.classify("create an automation that turns on the hall light") == "create"
    assert (
        ctx.classify("find the front light automation and propose a change") == "change"
    )


def test_context_has_target_and_related_but_never_disabled():
    inv = _inv()
    c = ctx.build_context(inv, "extend the front light time to 15 min")
    assert [t["entity_id"] for t in c["targets"]] == ["automation.front"]
    assert [r["entity_id"] for r in c["related"]] == ["automation.night"]
    assert (
        "Routine: Night" in c["text"] and "also ACTS on light.lamp_entrada" in c["text"]
    )
    assert "switch.other" not in c["text"]  # cut down to the shared entity
    assert "Scene: Old" not in c["text"]
    assert c["disabled_left_out"] == 1
    assert 0 < c["tokens"] < ctx.TOKEN_CAP


def test_picked_disabled_automation_is_refused():
    c = ctx.build_context(_inv(), "change it", ["automation.old"])
    assert c["targets"] == [] and c["picked_disabled"] == ["automation.old"]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    return Store(tmp_path / "j.db")


class FakeSDK:
    def __init__(self, reply):
        self.reply, self.requests = reply, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **req):
        self.requests.append(req)
        usage = SimpleNamespace(
            input_tokens=2000,
            output_tokens=300,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        content = [SimpleNamespace(type="text", text=self.reply)]
        return SimpleNamespace(usage=usage, content=content, stop_reason="end_turn")


def test_preview_calls_no_model_and_shows_both_costs(store):
    sdk = FakeSDK("x")
    client = CloudClient(store, sdk_client=sdk)
    p = rec.preview(_inv(), "extend the front light time to 15 min", None, client)
    assert sdk.requests == [] and p["can_send"] and p["dry_run"]
    assert 0 < p["estimate_usd"]["sonnet"] < p["estimate_usd"]["opus"]


def test_recommend_dry_run_sends_nothing(store):
    sdk = FakeSDK("x")
    client = CloudClient(store, sdk_client=sdk)
    case = rec.recommend(_inv(), "extend the front light time", "sonnet", None, client)
    assert sdk.requests == [] and case["dry_run"] and case["cost_usd"] == 0


def test_recommend_live_with_fake_sdk_counts_spend_and_flags_unknown(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(rec, "cases_path", lambda: tmp_path / "cases.json")
    store.update_settings({"dry_run": False})
    sdk = FakeSDK("Change light.lamp_entrada and light.ghost_lamp to 15 min")
    client = CloudClient(store, sdk_client=sdk)
    case = rec.recommend(_inv(), "extend the front light time", "sonnet", None, client)
    assert len(sdk.requests) == 1
    req = sdk.requests[0]
    assert "Scene: Old" not in str(req["messages"])
    assert case["id"].endswith("_001") and case["cost_usd"] > 0
    assert case["unknown_entities"] == ["light.ghost_lamp"]
    assert rec.spend_usd(store) == pytest.approx(case["cost_usd"])
    assert rec.load_cases()[0]["id"] == case["id"]


def test_architect_limit_blocks_before_the_call(store):
    store.update_settings({"dry_run": False})
    store.record_run(
        "diagnose", "m", outcome="ok", purpose="architect:A_1", cost_usd=4.999
    )
    sdk = FakeSDK("x")
    client = CloudClient(store, sdk_client=sdk)
    with pytest.raises(rec.ArchitectRefused):
        rec.recommend(_inv(), "extend the front light time", "opus", None, client)
    assert sdk.requests == []
