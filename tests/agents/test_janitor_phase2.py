"""Phase 2: L0 watchdog (observe, lifecycle, quiet hours, scheduler, API)."""

from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openjarvis.agents import watchdog as wd
from openjarvis.agents.ha_collector import HaCollector, parse_log_groups
from openjarvis.agents.store import SettingsError, Store
from openjarvis.agents.watchdog import (
    Observation,
    WatchdogService,
    observe,
    run_tick,
)

NOW = 1_800_000_000.0
MIN = 60


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "janitor.db")


def snap(**sections):
    base = {
        "repairs": [],
        "domain_totals": {},
        "integrations": [],
        "unavailable": {},
        "critical": {},
        "motion": {},
        "batteries": [],
        "backup": None,
        "logs": {},
        "errors": {},
    }
    base.update(sections)
    return base


class FakeCollector:
    def __init__(self, *snaps):
        self.snaps = list(snaps)
        self.calls = 0

    def collect(self, settings):
        self.calls += 1
        s = self.snaps[min(self.calls - 1, len(self.snaps) - 1)]
        if isinstance(s, Exception):
            raise s
        return copy.deepcopy(s)


def iso(ts):
    return datetime.fromtimestamp(ts, ZoneInfo("UTC")).isoformat()


def cats(obs):
    return sorted((o.category, o.source_id) for o in obs)


# --- observe ---------------------------------------------------------------


def test_repairs_no_baseline_and_no_placeholders(store):
    repair = {
        "domain": "smtp",
        "issue_id": "auth_failed",
        "translation_key": "auth_failed",
        "severity": "error",
        "ignored": False,
        "translation_placeholders": {"password": "hunter2-secret-value"},
    }
    ignored = {**repair, "issue_id": "x", "ignored": True}
    obs, ok = observe(snap(repairs=[repair, ignored]), store.settings(), NOW)
    assert cats(obs) == [("repair", "smtp/auth_failed")]
    assert obs[0].severity == "warning" and obs[0].persist_min == 0
    assert "repair" in ok
    assert "hunter2" not in repr(obs[0])


def test_failed_integration_needs_persist(store):
    entries = [
        {"entry_id": "a", "domain": "fritz", "title": "Box", "state": "setup_retry"},
        {"entry_id": "b", "domain": "x", "title": "t", "state": "loaded"},
        {
            "entry_id": "c",
            "domain": "y",
            "title": "t",
            "state": "not_loaded",
            "disabled_by": "user",
        },
    ]
    obs, _ = observe(snap(integrations=entries), store.settings(), NOW)
    assert cats(obs) == [("integration", "a")]
    assert obs[0].persist_min == 10


def test_entity_groups_thresholds_ignore_and_ownership(store):
    s = store.settings()
    ents = {
        "sensor": [{"entity_id": f"sensor.s{i}", "name": ""} for i in range(30)],
        "light": [{"entity_id": "light.a", "name": ""}],
        "device_tracker": [{"entity_id": "device_tracker.p", "name": ""}],
        "binary_sensor": [{"entity_id": "binary_sensor.motion1", "name": ""}],
    }
    totals = {
        "sensor": {"count": 100},
        "light": {"count": 100},
        "device_tracker": {"count": 1},
        "binary_sensor": {"count": 10},
    }
    motion = {
        "binary_sensor.motion1": {"state": "unavailable", "last_changed": iso(NOW)}
    }
    obs, _ = observe(
        snap(unavailable=ents, domain_totals=totals, motion=motion), s, NOW
    )
    got = {(o.category, o.source_id): o for o in obs}
    # 30% >= red 10% -> critical; light 1% < amber 2% -> nothing;
    # device_tracker ignored by pattern; binary_sensor owned by the motion issue.
    assert got[("entity_group", "sensor")].severity == "critical"
    assert ("entity_group", "light") not in got
    assert ("entity_group", "device_tracker") not in got
    assert ("entity_group", "binary_sensor") not in got
    assert ("motion", "binary_sensor.motion1") in got


def test_entity_group_needs_overview_totals(store):
    obs, ok = observe(
        snap(unavailable={"sensor": []}, domain_totals=None), store.settings(), NOW
    )
    assert "entity_group" not in ok and obs == []


def test_critical_entities_missing_and_unavailable(store):
    crit = {
        "lock.front": {"state": "unavailable", "last_changed": iso(NOW - 3600)},
        "binary_sensor.smoke": {"state": "off", "last_changed": iso(NOW)},
        "lock.gone": None,
    }
    obs, _ = observe(snap(critical=crit), store.settings(), NOW)
    assert cats(obs) == [
        ("critical_entity", "lock.front"),
        ("critical_entity", "lock.gone"),
    ]
    assert {o.severity for o in obs} == {"critical"}
    front = next(o for o in obs if o.source_id == "lock.front")
    assert front.since == pytest.approx(NOW - 3600, abs=1)


def test_battery_threshold_and_binary_low(store):
    bats = [
        {"entity_id": "sensor.a_battery", "level": 12.0, "low": False},
        {"entity_id": "sensor.b_battery", "level": 55.0, "low": False},
        {"entity_id": "binary_sensor.c_battery", "level": None, "low": True},
        {"entity_id": "sensor.edge_battery", "level": 20.0, "low": False},
    ]
    obs, _ = observe(snap(batteries=bats), store.settings(), NOW)
    assert [o.source_id for o in obs] == [
        "sensor.a_battery",
        "binary_sensor.c_battery",
        "sensor.edge_battery",
    ]


def test_backup_age_36h_and_double_is_critical(store):
    s = store.settings()
    fresh, _ = observe(snap(backup={"last_success": iso(NOW - 10 * 3600)}), s, NOW)
    old, _ = observe(snap(backup={"last_success": iso(NOW - 40 * 3600)}), s, NOW)
    very, _ = observe(snap(backup={"last_success": iso(NOW - 80 * 3600)}), s, NOW)
    unknown, _ = observe(snap(backup={"last_success": None}), s, NOW)
    assert fresh == []
    assert old[0].severity == "warning" and very[0].severity == "critical"
    assert unknown[0].title.endswith("unknown")


def test_log_groups(store):
    log = (
        "2026-10-25 10:00:00.123 ERROR (MainThread) [homeassistant.components.fritz."
        "x] Failed password: hunter2-secret-value\n"
        "2026-10-25 10:00:01.123 ERROR (MainThread) [homeassistant.components.fritz]"
        " b\n"
        "2026-10-25 10:00:02.123 ERROR (MainThread) [homeassistant.components.fritz]"
        " c\n"
        "2026-10-25 10:00:03.123 CRITICAL (MainThread) [custom_components.hacs] boom\n"
        "2026-10-25 10:00:04.123 ERROR (MainThread) [homeassistant.core] one\n"
        "2026-10-25 10:00:05.123 WARNING (MainThread) [homeassistant.core] w\n"
    )
    groups = parse_log_groups(log)
    assert groups["fritz"]["error"] == 3 and groups["hacs"]["critical"] == 1
    assert "hunter2" not in repr(groups)
    obs, _ = observe(snap(logs=groups), store.settings(), NOW)
    assert cats(obs) == [("log", "fritz:errors"), ("log", "hacs:critical")]


def test_fingerprint_is_stable_and_distinct():
    a = Observation("repair", "x/y", "t1")
    b = Observation("repair", "x/y", "different title", "critical")
    c = Observation("repair", "x/z", "t1")
    assert a.fingerprint == b.fingerprint != c.fingerprint


# --- lifecycle ---------------------------------------------------------------


def lock_down(since):
    return snap(
        critical={"lock.front": {"state": "unavailable", "last_changed": iso(since)}}
    )


def test_debounce_then_issue_then_resolve(store):
    col = FakeCollector(lock_down(NOW), lock_down(NOW), snap(), snap(), snap())
    r1 = run_tick(store, col, now=NOW)
    assert r1.pending == 1 and not r1.new and store.issues() == []
    r2 = run_tick(store, col, now=NOW + 11 * MIN)
    assert len(r2.new) == 1 and r2.new[0]["severity"] == "critical"
    assert len(store.issues("open")) == 1
    # clean tick 1: not resolved yet (flap protection); clean tick 2: resolved
    r3 = run_tick(store, col, now=NOW + 26 * MIN)
    assert not r3.resolved and store.issues("open")
    r4 = run_tick(store, col, now=NOW + 41 * MIN)
    assert len(r4.resolved) == 1 and store.issues("open") == []
    assert store.watch_states() == {}


def test_hass_last_changed_skips_debounce(store):
    col = FakeCollector(lock_down(NOW - 3600))
    r = run_tick(store, col, now=NOW)
    assert len(r.new) == 1 and r.pending == 0


def test_blip_that_clears_never_becomes_an_issue(store):
    col = FakeCollector(lock_down(NOW), snap(), snap())
    run_tick(store, col, now=NOW)
    run_tick(store, col, now=NOW + 5 * MIN)
    assert store.watch_states() == {}
    run_tick(store, col, now=NOW + 20 * MIN)
    assert store.issues() == []


def test_failed_category_never_resolves_issues(store):
    col = FakeCollector(
        lock_down(NOW - 3600),
        snap(critical=None, errors={"critical": "boom"}),
        snap(critical=None, errors={"critical": "boom"}),
        snap(critical=None, errors={"critical": "boom"}),
    )
    run_tick(store, col, now=NOW)
    for i in range(1, 4):
        r = run_tick(store, col, now=NOW + i * 20 * MIN)
        assert r.errors == {"critical": "boom"} and not r.resolved
    assert len(store.issues("open")) == 1


def test_acknowledged_and_ignored_keep_state_resolved_reopens(store):
    col = FakeCollector(lock_down(NOW - 3600))
    run_tick(store, col, now=NOW)
    fp = store.issues()[0]["fingerprint"]
    store.set_issue_state(fp, "acknowledged")
    run_tick(store, col, now=NOW + 15 * MIN)
    assert store.issue(fp)["state"] == "acknowledged"
    store.set_issue_state(fp, "ignored")
    run_tick(store, col, now=NOW + 30 * MIN)
    assert store.issue(fp)["state"] == "ignored"

    col2 = FakeCollector(lock_down(NOW - 3600), snap(), snap(), lock_down(NOW))
    store2 = Store(store.path.parent / "second.db")
    run_tick(store2, col2, now=NOW)
    run_tick(store2, col2, now=NOW + 15 * MIN)
    run_tick(store2, col2, now=NOW + 30 * MIN)
    assert store2.issues("resolved")
    r = run_tick(store2, col2, now=NOW + 45 * MIN)
    assert len(r.reopened) == 1 or r.pending == 1  # reopened, or debouncing again


def test_severity_escalation_is_reported(store):
    a = snap(
        domain_totals={"sensor": {"count": 100}},
        unavailable={
            "sensor": [{"entity_id": f"sensor.s{i}", "name": ""} for i in range(5)]
        },
    )
    b = snap(
        domain_totals={"sensor": {"count": 100}},
        unavailable={
            "sensor": [{"entity_id": f"sensor.s{i}", "name": ""} for i in range(40)]
        },
    )
    col = FakeCollector(a, a, b)
    run_tick(store, col, now=NOW)
    run_tick(store, col, now=NOW + 11 * MIN)
    r = run_tick(store, col, now=NOW + 22 * MIN)
    assert [e["severity"] for e in r.escalated] == ["critical"]


def test_tick_survives_collector_failure_and_logs_a_run(store):
    col = FakeCollector(RuntimeError("HA down"))
    r = run_tick(store, col, now=NOW)
    assert not r.ok and "HA down" in r.errors["tick"]
    run = store.runs(1)[0]
    assert run["layer"] == "L0" and run["outcome"] == "error" and run["cost_usd"] == 0


# --- quiet hours ---------------------------------------------------------------


def local_epoch(hh, mm=0):
    return datetime(2026, 10, 25, hh, mm, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


def test_quiet_hours_wrap_midnight_and_toggle(store):
    s = store.settings()  # 22:00-07:00 Europe/Berlin
    assert wd.is_quiet_hours(local_epoch(23), s)
    assert wd.is_quiet_hours(local_epoch(3), s)
    assert wd.is_quiet_hours(local_epoch(6, 59), s)
    assert not wd.is_quiet_hours(local_epoch(7), s)
    assert not wd.is_quiet_hours(local_epoch(12), s)
    s["watchdog"]["quiet_hours"]["enabled"] = False
    assert not wd.is_quiet_hours(local_epoch(23), s)


def test_quiet_hours_flag_covers_critical_events(store):
    night = local_epoch(23)
    col = FakeCollector(lock_down(night - 3600))
    r = run_tick(store, col, now=night)
    assert r.quiet_hours and r.new[0]["severity"] == "critical"


# --- settings ---------------------------------------------------------------------


def test_watchdog_settings_validation(store):
    good = {
        "enabled": True,
        "interval_min": 30,
        "quiet_hours": {"enabled": True, "start": "23:00", "end": "06:30"},
    }
    assert store.update_settings({"watchdog": good})["watchdog"]["interval_min"] == 30
    for bad in (
        {**good, "interval_min": 0},
        {**good, "enabled": "yes"},
        {**good, "quiet_hours": {**good["quiet_hours"], "start": "25:00"}},
        {**good, "extra": 1},
    ):
        with pytest.raises(SettingsError):
            store.update_settings({"watchdog": bad})


def test_defaults_match_decisions(store):
    s = store.settings()
    assert s["watchdog"]["interval_min"] == 15
    assert s["thresholds"]["persist_min"] == 10
    assert s["thresholds"]["battery_low_pct"] == 20
    assert s["thresholds"]["backup_age_hours"] == 36
    assert s["dry_run"] is True
    assert s["motion_sensors"] == []  # automatic: device_class motion


# --- collector with a fake Home Assistant --------------------------------------------


class FakeHA:
    """Answers the few read calls the collector makes; records them all."""

    def __init__(self):
        self.calls = []
        self.closed = False

    def read(self, name, args=None):
        args = args or {}
        self.calls.append(name)
        if name == "ha_get_overview":
            return {
                "repairs": [{"domain": "d", "issue_id": "i", "severity": "error"}],
                "domain_stats": {
                    "sensor": {"count": 3, "states_summary": {"unavailable": 1}},
                    "light": {"count": 2, "states_summary": {}},
                },
            }
        if name == "ha_get_integration":
            return {"entries": [{"entry_id": "e", "state": "setup_retry"}]}
        if name == "ha_search":
            dom, q = args["domain_filter"], args["query"]
            if dom == "sensor" and q == "battery":
                return {"entities": [{"entity_id": "sensor.k_battery", "state": "9"}]}
            if dom == "sensor":
                return {
                    "entities": [
                        {"entity_id": "sensor.a", "state": "unavailable"},
                        {"entity_id": "sensor.b", "state": "5"},
                    ],
                    "entity_has_more": False,
                }
            if dom == "binary_sensor":
                return {
                    "entities": [
                        {"entity_id": "binary_sensor.pir", "friendly_name": "PIR"},
                        {"entity_id": "binary_sensor.door", "friendly_name": "Door"},
                        {"entity_id": "binary_sensor.smoke", "friendly_name": "S"},
                    ],
                    "entity_has_more": False,
                }
            return {
                "entities": [{"entity_id": f"{dom}.x", "state": "locked"}],
                "entity_has_more": False,
            }
        if name == "ha_get_state":
            ids = args["entity_id"]
            table = {
                "binary_sensor.pir": ("unavailable", "motion"),
                "binary_sensor.door": ("on", "door"),
                "binary_sensor.smoke": ("off", "smoke"),
                "sensor.k_battery": ("9", "battery"),
                "sensor.backup_last_successful_automatic_backup": (
                    "2026-10-25T02:54:22+00:00",
                    "timestamp",
                ),
                "lock.front": ("unavailable", None),
            }
            states = {
                i: {
                    "state": table[i][0],
                    "attributes": {"device_class": table[i][1]},
                    "last_changed": "2026-10-25T01:00:00+00:00",
                }
                for i in ids
                if i in table
            }
            return {"data": {"success": True, "count": len(states), "states": states}}
        if name == "ha_get_logs":
            return {"log": "2026-10-25 10:00:03.1 CRITICAL (MainThread) [x.y] boom\n"}
        raise AssertionError(f"unexpected call {name}")

    def close(self):
        self.closed = True


def test_collector_builds_snapshot_from_read_calls_only(store):
    ha = FakeHA()
    s = store.settings()
    s["critical_entities"] = ["lock.front", "lock.nope"]
    snapshot = HaCollector(lambda: ha).collect(s)
    assert ha.closed and snapshot["errors"] == {}
    assert set(ha.calls) <= {
        "ha_get_overview",
        "ha_get_integration",
        "ha_search",
        "ha_get_state",
        "ha_get_logs",
    }
    assert snapshot["motion"].keys() == {"binary_sensor.pir"}  # device_class only
    assert snapshot["critical"]["lock.nope"] is None
    assert snapshot["batteries"] == [
        {"entity_id": "sensor.k_battery", "level": 9.0, "low": False}
    ]
    assert snapshot["unavailable"] == {
        "sensor": [{"entity_id": "sensor.a", "name": ""}]
    }
    assert snapshot["backup"]["last_success"].startswith("2026-10-25")
    assert snapshot["logs"]["x"]["critical"] == 1
    obs, _ = observe(snapshot, s, 1_793_000_000.0)
    assert {o.category for o in obs} >= {"repair", "integration", "motion", "log"}


def test_collector_section_error_is_isolated(store):
    class Broken(FakeHA):
        def read(self, name, args=None):
            if name == "ha_get_logs":
                raise RuntimeError("log tool down")
            return super().read(name, args)

    snapshot = HaCollector(lambda: Broken()).collect(store.settings())
    assert snapshot["logs"] is None and "logs" in snapshot["errors"]
    assert snapshot["repairs"] is not None


def test_suggestions_by_device_class(store):
    ha = FakeHA()
    out = HaCollector(lambda: ha).suggest()
    kinds = {r["entity_id"]: r["kind"] for r in out["critical"]}
    assert kinds["binary_sensor.smoke"] == "smoke"
    assert kinds["lock.x"] == "lock" and "binary_sensor.door" not in kinds
    assert [m["entity_id"] for m in out["motion"]] == ["binary_sensor.pir"]


# --- scheduler -----------------------------------------------------------------------


def test_service_loop_ticks_and_survives_errors(store, monkeypatch):
    monkeypatch.setenv("JARVIS_WATCHDOG", "1")
    col = FakeCollector(RuntimeError("down"), snap())
    svc = WatchdogService(store, col, notifier=lambda r: None)

    async def go():
        assert svc.start(first_delay=0.01)
        await asyncio.sleep(0.15)
        await svc.stop()

    store.update_settings(
        {
            "watchdog": {
                "enabled": True,
                "interval_min": 1,
                "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            }
        }
    )
    asyncio.run(go())
    assert col.calls >= 1 and svc.last is not None
    assert svc.next_run is None  # stopped


def test_env_switch_disables_autostart(store, monkeypatch):
    monkeypatch.setenv("JARVIS_WATCHDOG", "0")
    svc = WatchdogService(store, FakeCollector(snap()))

    async def go():
        return svc.start(first_delay=0.01)

    assert asyncio.run(go()) is False


def test_manual_tick_reports_busy_instead_of_overlapping(store):
    svc = WatchdogService(store, FakeCollector(snap()), notifier=lambda r: None)
    first = svc.tick()
    assert first["ok"]
    svc._lock.acquire()
    try:
        assert svc.tick(wait=False)["busy"] is True
    finally:
        svc._lock.release()


def test_status_counts_active_issues(store):
    col = FakeCollector(lock_down(NOW - 3600))
    svc = WatchdogService(store, col, notifier=lambda r: None, clock=lambda: NOW)
    svc.tick()
    st = svc.status()
    assert st["issues"]["active"] == 1
    assert st["issues"]["by_severity"] == {"critical": 1}
    assert st["interval_min"] == 15


# --- API ------------------------------------------------------------------------------


def test_watchdog_and_issue_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server import ha_agents_routes as routes

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    store_mod.reset_store_for_tests()
    wd.reset_watchdog_for_tests()
    service = WatchdogService(
        store_mod.get_store(),
        FakeCollector(lock_down(time.time() - 3600)),
        notifier=lambda r: None,
    )
    monkeypatch.setattr(routes, "get_watchdog", lambda: service)
    app = FastAPI()
    app.include_router(routes.ha_agents_router)
    http = TestClient(app)

    assert http.get("/v1/ha/agents/watchdog").json()["issues"]["active"] == 0
    ran = http.post("/v1/ha/agents/watchdog/run").json()
    assert ran["ok"] and len(ran["new"]) == 1
    issues = http.get("/v1/ha/agents/issues?state=open").json()["issues"]
    assert len(issues) == 1
    fp = issues[0]["fingerprint"]
    ack = http.post(f"/v1/ha/agents/issues/{fp}/state", json={"state": "acknowledged"})
    assert ack.json()["state"] == "acknowledged"
    assert (
        http.post(
            f"/v1/ha/agents/issues/{fp}/state", json={"state": "resolved"}
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/v1/ha/agents/issues/nope/state", json={"state": "ignored"}
        ).status_code
        == 404
    )
    assert http.get("/v1/ha/agents/issues?state=bogus").status_code == 422
    # no approval endpoint exists
    assert http.post("/v1/ha/agents/approve", json={}).status_code == 404
