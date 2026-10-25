"""Phase 3c: entities unavailable for long, Home Assistant restart detection."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import planning
from openjarvis.agents.ha_collector import HaCollector
from openjarvis.agents.store import Store
from openjarvis.agents.watchdog import run_tick

DAY = 86400
NOW = datetime(2026, 12, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "janitor.db")


def rows(**ages):
    """entity -> {name, since} where the value is its age in days."""
    return {k: {"name": k.upper(), "since": NOW - d * DAY} for k, d in ages.items()}


def test_track_keeps_oldest_since_and_drops_recovered(store):
    store.track_unavailable(rows(**{"sensor.a": 40, "sensor.b": 5}), NOW)
    # next tick: a is still down, b recovered, c is new
    store.track_unavailable(rows(**{"sensor.a": 1, "sensor.c": 2}), NOW + 900)
    out = store.unavailable_longer_than(30, NOW + 900)
    assert [r["entity_id"] for r in out["items"]] == ["sensor.a"]
    assert out["tracked"] == 2 and out["count"] == 1
    assert out["counting_since"] == pytest.approx(NOW - 40 * DAY)


def test_entities_without_a_hint_start_now(store):
    store.track_unavailable({"sensor.x": {"name": "x", "since": None}}, NOW)
    assert store.unavailable_longer_than(30, NOW)["count"] == 0
    assert store.unavailable_longer_than(30, NOW + 31 * DAY)["count"] == 1


def test_overview_cards_for_stale_and_restart(store):
    store.track_unavailable(
        rows(**{"sensor.old": 45, "sensor.older": 90, "sensor.new": 3}), NOW
    )
    store.record_boot(NOW - 5 * 3600, NOW, False)
    o = planning.overview(store, NOW)
    assert o["cards"]["stale_unavailable"] == {"value": 2, "days": 10, "tone": "warn"}
    assert o["cards"]["last_restart"]["value"] == 5.0
    sec = o["sections"]["stale_unavailable"]
    assert [i["title"] for i in sec["items"]] == ["SENSOR.OLDER", "SENSOR.OLD"]
    assert "unavailable about 90 days" in sec["items"][0]["detail"]
    assert sec["reliable_from"] == pytest.approx(NOW - 90 * DAY + 10 * DAY)
    assert o["sections"]["last_restart"]["total"] == 1


def test_overview_without_data_is_safe(store):
    o = planning.overview(store, NOW)
    assert o["cards"]["stale_unavailable"]["value"] == 0
    assert o["cards"]["last_restart"]["value"] is None
    assert o["cards"]["last_restart"]["tone"] == "idle"


def test_boot_is_recorded_once_even_when_the_estimate_jitters(store):
    boot = NOW - 3 * DAY
    assert store.record_boot(boot, NOW, False) is True
    assert store.record_boot(boot + 20, NOW + 900, False) is False  # same start
    assert store.record_boot(NOW - 600, NOW, True) is True  # a new start
    got = store.boots()
    assert [b["after_outage"] for b in got] == [1, 0]


class Snap:
    def __init__(self, *snaps):
        self.snaps = list(snaps)
        self.n = 0

    def collect(self, settings):
        s = self.snaps[min(self.n, len(self.snaps) - 1)]
        self.n += 1
        if isinstance(s, Exception):
            raise s
        return s


def snap(boot=None, since=None):
    base = {
        k: None
        for k in (
            "repairs",
            "integrations",
            "unavailable",
            "critical",
            "motion",
            "batteries",
            "backup",
            "logs",
            "domain_totals",
        )
    }
    base.update({"errors": {}, "unavailable_since": since, "facts": {}})
    if boot:
        base["facts"]["boot_estimate"] = boot
    return base


def test_tick_tracks_and_flags_a_restart_after_an_outage(store):
    col = Snap(
        snap(NOW - 10 * 3600, rows(**{"sensor.a": 35})),
        RuntimeError("HA unreachable"),
        snap(NOW - 600, rows(**{"sensor.a": 35})),
    )
    run_tick(store, col, now=NOW)
    assert store.unavailable_longer_than(30, NOW)["count"] == 1
    assert len(store.boots()) == 1 and store.boots()[0]["after_outage"] == 0
    run_tick(store, col, now=NOW + 900)  # outage
    run_tick(store, col, now=NOW + 1800)  # HA is back, new start time
    boots = store.boots()
    assert len(boots) == 2 and boots[0]["after_outage"] == 1
    o = planning.overview(store, NOW + 1800)
    assert o["sections"]["last_restart"]["items"][0]["severity"] == "warning"


class StubHA:
    def __init__(self, changed):
        self.changed = changed
        self.asked = []

    def read(self, name, args=None):
        assert name == "ha_get_state"
        self.asked.append(list(args["entity_id"]))
        states = {
            e: {"state": "unavailable", "last_changed": self.changed[e]}
            for e in args["entity_id"]
            if e in self.changed
        }
        return {"data": {"states": states}}


def test_collector_since_uses_last_changed_and_estimates_the_boot(store):
    s = store.settings()
    ha = StubHA(
        {
            "sensor.a": "2026-10-10T01:35:30+02:00",
            "light.b": "2026-10-10T01:35:31+02:00",
            "sun.sun": "2026-10-10T01:35:12+02:00",
            "zone.home": "2026-10-10T01:35:20+02:00",
            "device_tracker.phone": "2026-10-10T01:35:30+02:00",
        }
    )
    snapshot = {
        "unavailable": {
            "sensor": [{"entity_id": "sensor.a", "name": "A"}],
            "light": [{"entity_id": "light.b", "name": "B"}],
            "device_tracker": [{"entity_id": "device_tracker.phone", "name": "P"}],
        },
        "facts": {},
    }
    out = HaCollector(lambda: ha)._since(ha, snapshot, s)
    assert set(out) == {"sensor.a", "light.b"}  # device_tracker.* is ignored by default
    assert out["sensor.a"]["since"] < out["light.b"]["since"]
    boot = datetime(
        2026, 10, 10, 1, 35, 12, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()
    assert snapshot["facts"]["boot_estimate"] == pytest.approx(boot)
    assert "sun.sun" in ha.asked[0] and "zone.home" in ha.asked[0]


def test_markdown_export_is_rewritten_and_drops_recovered_entities(
    store, tmp_path, monkeypatch
):
    from openjarvis.agents.watchdog import export_unavailable_md

    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path / "out"))
    store.track_unavailable(
        rows(**{"sensor.a": 40, "light.b": 12, "sensor.fresh": 2}), NOW
    )
    path = export_unavailable_md(store, NOW)
    text = path.read_text()
    assert path.name == "unavailable-long.md"
    assert "more than 10 days" in text and "2 of 3" in text
    assert "| sensor.a | SENSOR.A |" in text and "| 40 |" in text
    assert "sensor.fresh" not in text
    # sensor.a comes back: next export no longer lists it
    store.track_unavailable(rows(**{"light.b": 12}), NOW + 900)
    assert "sensor.a" not in export_unavailable_md(store, NOW + 900).read_text()


def test_threshold_is_a_setting(store):
    th = {**store.settings()["thresholds"], "stale_unavailable_days": 20}
    assert (
        store.update_settings({"thresholds": th})["thresholds"][
            "stale_unavailable_days"
        ]
        == 20
    )
    store.track_unavailable(rows(**{"sensor.a": 15}), NOW)
    assert planning.overview(store, NOW)["cards"]["stale_unavailable"]["value"] == 0
