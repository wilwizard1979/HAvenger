"""Architect step 4: the edits block becomes a checked Telegram approval request."""

from __future__ import annotations

import copy
import json

import pytest

from openjarvis.agents import architect_apply as ap
from openjarvis.agents import config_edit as ce
from openjarvis.agents import janitor_executor as executor
from openjarvis.agents.store import Store
from tests.agents.test_architect_context import _inv

CONFIG = {
    "alias": "Monitor: Front Light",
    "triggers": [
        {"trigger": "state", "entity_id": "light.lamp_entrada", "for": {"minutes": 30}}
    ],
    "actions": [
        {"action": "light.turn_off", "target": {"entity_id": "light.lamp_entrada"}}
    ],
}
ANSWER = """1. Understood.
```edits
{"identifier": "automation.front", "risk": "low", "edits": [
 {"path": "triggers.0.for.minutes", "old": 30, "set": 15}]}
```"""


def _case(**over):
    case = {
        "id": "A_2026_10_29_001",
        "question": "extend the front light time to 15 min",
        "model": "sonnet",
        "targets": [{"entity_id": "automation.front", "alias": "Monitor: Front Light"}],
        "proposal": ap.parse_edits(ANSWER),
    }
    case.update(over)
    return case


def _read(config=None):
    cfg = copy.deepcopy(config or CONFIG)

    def read(tool, args):
        assert tool == ce.READ_TOOL
        return json.dumps({"success": True, "config": cfg})

    return read


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "j.db")
    cfg = s.settings()["executor"]
    s.update_settings(
        {"executor": {**cfg, "allowed_tools": [*cfg["allowed_tools"], ce.WRITE_TOOL]}}
    )
    return s


def test_parse_edits():
    p = ap.parse_edits(ANSWER)
    assert p["identifier"] == "automation.front" and p["risk"] == "low"
    assert p["edits"][0]["set"] == 15
    assert ap.parse_edits("no block here") is None
    assert ap.parse_edits("```edits\nnot json\n```") is None
    assert ap.parse_edits('```edits\n{"identifier": "x"}\n```') is None


def test_problems_only_sent_automation_and_known_entities():
    inv = _inv()
    assert ap.proposal_problems(_case()["proposal"], _case(), inv) == []
    other = {**_case()["proposal"], "identifier": "automation.night"}
    assert any("not one of" in p for p in ap.proposal_problems(other, _case(), inv))
    off = {**_case()["proposal"], "identifier": "automation.old"}
    case = _case(targets=[{"entity_id": "automation.old", "alias": "x"}])
    assert any("is off" in p for p in ap.proposal_problems(off, case, inv))
    bad = {
        **_case()["proposal"],
        "edits": [{"path": "actions.0.action", "set": "light.nope_x"}],
    }
    assert any("unknown entity" in p for p in ap.proposal_problems(bad, _case(), inv))
    assert ap.proposal_problems(None, _case(), inv)


def test_request_approval_builds_diff_and_restore(store):
    out = ap.request_approval(store, _case(), _inv(), read=_read())
    assert out["status"] == "awaiting_approval"
    e = store.execution(out["id"])
    ins = e["instruction"]
    assert any("15" in d for d in ins["steps"][0]["preview"]["diff"])
    assert ins["rollback_steps"][0]["args"]["restore_config"] == CONFIG
    assert e["cluster_id"] == "architect:A_2026_10_29_001" and e["risk"] == "low"
    assert (
        ap.request_approval(store, _case(), _inv(), read=_read())["status"]
        == "already_active"
    )


def test_old_value_guard_blocks_shifted_index(store):
    changed = copy.deepcopy(CONFIG)
    changed["triggers"][0]["for"]["minutes"] = 45
    out = ap.request_approval(store, _case(), _inv(), read=_read(changed))
    assert out["status"] == "not_executable"
    assert "not what the review saw" in out["problems"][0]


def test_write_tool_must_be_enabled(tmp_path):
    s = Store(tmp_path / "j2.db")
    out = ap.request_approval(s, _case(), _inv(), read=_read())
    assert out["status"] == "not_executable"
    assert any("not enabled" in p for p in out["problems"])


def test_simulation_runs_after_approval(store):
    out = ap.request_approval(store, _case(), _inv(), read=_read())
    h = store.execution(out["id"])["instruction_hash"][:8]
    assert executor.decide(store, out["id"], h, True)["status"] == "approved"
    assert executor.run_execution(store, out["id"])["status"] == "simulated"
