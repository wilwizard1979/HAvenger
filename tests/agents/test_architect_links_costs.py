"""Edit links in answers, the HA address setting, and the real-cost counter."""

from __future__ import annotations

import time

import pytest

from openjarvis.agents import architect as a
from openjarvis.agents import costs
from openjarvis.agents.store import Store


def _inv():
    items = [
        a.build_item(
            "automation.front",
            {"state": "on", "attributes": {}},
            {"id": "1674913624861", "alias": "Monitor: Front Light"},
        ),
        a.build_item(
            "automation.noid", {"state": "on", "attributes": {}}, {"alias": "No Id"}
        ),
    ]
    return {"items": items}


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "config_path", lambda: tmp_path / "config.json")


def test_default_address_and_edit_link():
    inv = _inv()
    assert a.ha_base() == "http://192.168.176.200:8123"
    assert (
        a.edit_url(inv["items"][0])
        == "http://192.168.176.200:8123/config/automation/edit/1674913624861"
    )
    assert a.edit_url(inv["items"][1]).endswith("/config/automation/dashboard")


def test_set_address_validates():
    assert a.set_ha_base("10.0.0.5:8123/") == "http://10.0.0.5:8123"
    assert a.ha_base() == "http://10.0.0.5:8123"
    with pytest.raises(ValueError):
        a.set_ha_base("http://bad host/<x>")


def test_answer_links_by_id_and_alias():
    links = a.answer_links(
        "Change automation.front (Monitor: Front Light) and nothing else.", _inv()
    )
    assert {link["text"] for link in links} == {
        "automation.front",
        "Monitor: Front Light",
    }
    assert all("1674913624861" in link["url"] for link in links)
    assert a.answer_links("nothing here", _inv()) == []


def _run(store, cost, purpose, dry=0, ts=None):
    store._run(  # noqa: SLF001
        "INSERT INTO runs(layer,model,started,cost_usd,purpose,dry_run) "
        "VALUES(?,?,?,?,?,?)",
        ("cloud", "m", ts or time.time(), cost, purpose, dry),
    )


def test_costs_split_dry_runs_and_reset(tmp_path):
    s = Store(tmp_path / "j.db")
    _run(s, 0.5, "escalate:1")
    _run(s, 0.25, "architect:A_1")
    _run(s, 9.0, "architect:A_2", dry=1)
    out = costs.summary(s)
    assert (out["total_usd"], out["janitor_usd"], out["architect_usd"]) == (
        0.75,
        0.5,
        0.25,
    )
    assert out["reset"] is False and out["counted_from"]
    after = costs.reset(s, now=time.time() + 5)
    assert after["total_usd"] == 0 and after["reset"] is True
    _run(s, 0.1, "escalate:2", ts=time.time() + 10)
    assert costs.summary(s)["total_usd"] == 0.1
