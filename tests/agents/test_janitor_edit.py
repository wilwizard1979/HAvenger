# ruff: noqa: F811
"""Edit-with-diff: Python reads the automation, applies small edits, shows the diff."""

from __future__ import annotations

import copy
import json

import pytest

from openjarvis.agents import config_edit as ce
from openjarvis.agents import janitor_executor as executor
from openjarvis.agents import planning
from openjarvis.agents.store import Store
from openjarvis.agents.telegram_bot import approval_text
from openjarvis.agents.watchdog import Observation

AUTOMATION = {
    "alias": "Birthdays Announcement",
    "triggers": [{"trigger": "time", "at": "08:00:00"}],
    "actions": [
        {"action": "calendar.get_events", "target": {"entity_id": "calendar.old"}},
        {"action": "notify.alexa_old", "data": {"message": "hi"}},
    ],
}
IDENT = "automation.birthdays_announcement"
STEP = {
    "tool": ce.EDIT_TOOL,
    "args": {
        "identifier": IDENT,
        "edits": [
            {"path": "actions.1.action", "set": "notify.alexa_new"},
            {"path": "actions.0.target.entity_id", "set": "calendar.birthdays"},
        ],
    },
    "purpose": "point to the new notify service and calendar",
    "verify": "config shows the new service",
}


class FakeHA:
    """Pretends to be Home Assistant for reads and writes of one automation."""

    def __init__(self, config=None):
        self.config = copy.deepcopy(config or AUTOMATION)
        self.writes, self.closed = [], False

    def call(self, tool, args):
        if tool == ce.READ_TOOL:
            return json.dumps({"success": True, "config": self.config})
        if tool == ce.WRITE_TOOL:
            self.writes.append(args)
            self.config = copy.deepcopy(args["config"])
            return json.dumps({"success": True})
        raise AssertionError(tool)

    def close(self):
        self.closed = True


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    s = Store(tmp_path / "janitor.db")
    o = Observation(
        "repair",
        "r1",
        "Birthdays Announcement uses an unknown action",
        "warning",
        {"domain": "automation"},
    )
    s.upsert_issue(
        o.fingerprint,
        category="repair",
        source_id="r1",
        title=o.title,
        meta={"domain": "automation"},
        now=1.0,
    )
    cl = planning.plan_clusters(s)[0]
    s.put_plan_proposal(
        cl["id"],
        cl["members_hash"],
        {
            "risk": "low",
            "summary": "Fix the unknown action",
            "instruction": {"preconditions": [], "steps": [STEP], "rollback_steps": []},
        },
        model="claude-sonnet-5-5",
        cost_usd=0.1,
    )
    s.cid = cl["id"]
    return s


def enable(store, **extra):
    cfg = store.settings()["executor"]
    store.update_settings(
        {
            "executor": {
                **cfg,
                "allowed_tools": ["ha_call_write_tool", ce.WRITE_TOOL],
                **extra,
            }
        }
    )


def test_apply_edits_and_diff():
    after = ce.apply_edits(AUTOMATION, STEP["args"]["edits"])
    assert after["actions"][1]["action"] == "notify.alexa_new"
    assert AUTOMATION["actions"][1]["action"] == "notify.alexa_old"  # input untouched
    diff = "\n".join(ce.diff_lines(AUTOMATION, after))
    assert "-" in diff and "notify.alexa_new" in diff and "calendar.birthdays" in diff
    deleted = ce.apply_edits(AUTOMATION, [{"path": "actions.1", "delete": True}])
    assert len(deleted["actions"]) == 1


@pytest.mark.parametrize(
    "edit",
    [
        {"path": "actions.9.action", "set": "x"},
        {"path": "nothing.here", "set": "x"},
        {"path": "actions.1.action", "set": "x", "delete": True},
        {"path": "bad path!", "set": "x"},
        {"path": "alias"},
    ],
)
def test_bad_edits_are_refused(edit):
    with pytest.raises(ce.EditError):
        ce.apply_edits(AUTOMATION, [edit])


def test_editing_is_off_until_the_owner_allows_the_write_tool(store):
    out = executor.request_approval(store, store.cid, read=FakeHA().call)
    assert out["status"] == "not_executable"
    assert "ha_config_set_automation" in " ".join(out["problems"])


def test_approval_shows_the_exact_diff_and_a_restore_step(store):
    enable(store)
    out = executor.request_approval(store, store.cid, read=FakeHA().call)
    assert out["status"] == "awaiting_approval"
    e = store.execution(out["id"])
    prev = e["instruction"]["steps"][0]["preview"]
    assert prev["before_hash"] != prev["after_hash"]
    restore = e["instruction"]["rollback_steps"][0]
    assert restore["args"]["restore_config"] == AUTOMATION
    text = approval_text(e, live=False)
    assert "EDIT automation.birthdays_announcement" in text and "alexa_new" in text


def test_unreadable_config_means_no_approval_request(store):
    enable(store)

    def broken(tool, args):
        return json.dumps({"success": False, "error": "not found"})

    out = executor.request_approval(store, store.cid, read=broken)
    assert out["status"] == "not_executable" and "not found" in out["problems"][0]


def approved(store):
    out = executor.request_approval(store, store.cid, read=FakeHA().call)
    e = store.execution(out["id"])
    executor.decide(store, out["id"], e["instruction_hash"][:8], True)
    return out["id"]


def test_simulation_writes_nothing(store):
    enable(store)
    made = []
    out = executor.run_execution(
        store, approved(store), ha_factory=lambda t: made.append(t) or FakeHA()
    )
    assert out["status"] == "simulated" and made == []


def test_live_edit_is_written_once_and_verified(store):
    enable(store, live=True, operator="python", require_backup=False)
    ha = FakeHA()
    out = executor.run_execution(store, approved(store), ha_factory=lambda t: ha)
    assert out["status"] == "done", out
    assert len(ha.writes) == 1
    assert ha.config["actions"][1]["action"] == "notify.alexa_new"


def test_live_refuses_when_the_automation_changed_after_approval(store):
    enable(store, live=True, operator="python", require_backup=False)
    eid = approved(store)
    changed = copy.deepcopy(AUTOMATION)
    changed["alias"] = "Someone edited me"
    ha = FakeHA(changed)
    out = executor.run_execution(store, eid, ha_factory=lambda t: ha)
    assert out["status"] in ("failed", "rolled_back")
    assert ha.writes == [] or ha.config["alias"] == "Someone edited me"
    assert not any(
        w["config"].get("actions", [{}])[1].get("action") == "notify.alexa_new"
        for w in ha.writes
    )
