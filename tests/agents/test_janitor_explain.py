"""Plain-language issue details, the 10-entity list, and one-click resolve."""

from __future__ import annotations

import json

import pytest

from openjarvis.agents import explain
from openjarvis.agents.store import Store
from openjarvis.agents.watchdog import (
    Observation,
    TickResult,
    apply_observations,
)

NOW = 1_800_000_000.0


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openjarvis.agents.architect.cache_path", lambda: tmp_path / "none.json"
    )
    return Store(tmp_path / "j.db")


def repair(source):
    return {
        "category": "repair",
        "source_id": source,
        "title": "Repair: automation / x",
    }


def test_service_not_found_names_the_automation_and_the_service(store):
    name, why = explain.explain_repair(
        repair(
            "automation/automation.alarm_low_temperature_service_not_found_"
            "notify.alexa_media_echo_show_8"
        )
    )
    assert name == "Alarm Low Temperature"
    assert "notify.alexa_media_echo_show_8" in why and "does not exist" in why


def test_validation_failed_says_which_part(store):
    name, why = explain.explain_repair(
        repair("automation/automation.automation_65_validation_failed_triggers")
    )
    assert name == "Automation 65" and "triggers are invalid" in why
    assert explain.explain_repair(repair("backup/automatic_backup_failed")) is None


def test_entity_list_is_limited_to_ten_with_a_count_of_the_rest(store):
    rows = {
        f"sensor.s{i:02d}": {"name": f"Sensor {i}", "since": NOW} for i in range(25)
    }
    store.track_unavailable(rows, NOW)
    issue = {
        "category": "entity_group",
        "title": "sensor: 25 of 100",
        "meta": json.dumps(
            {"domain": "sensor", "count": 25, "examples": ["sensor.s00"]}
        ),
    }
    shown, total = explain.affected(store, [issue])
    assert len(shown) == 10 and total == 25
    assert shown[0] == {"name": "Sensor 0", "detail": "sensor.s00"}


def _seen_then_gone(store, resolve_after):
    obs = Observation("repair", "backup/x", "Repair: backup / x", "warning", {})
    apply_observations(store, [obs], {"repair"}, NOW, TickResult(ok=True, started=NOW))
    result = TickResult(ok=True, started=NOW + 60)
    apply_observations(store, [], {"repair"}, NOW + 60, result, resolve_after)
    return result, store.issue(obs.fingerprint)


def test_manual_check_resolves_on_the_first_clean_read(store):
    result, issue = _seen_then_gone(store, 1)
    assert len(result.resolved) == 1 and issue["state"] == "resolved"


def test_scheduled_check_still_waits_for_two_clean_reads(store):
    result, issue = _seen_then_gone(store, 2)
    assert result.resolved == [] and issue["state"] == "open"
