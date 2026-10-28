from openjarvis.agents import architect as a


def _item(eid, alias, cfg, state="on", last="2026-10-01T10:00:00+00:00"):
    st = {
        "state": state,
        "attributes": {"last_triggered": last, "friendly_name": alias},
    }
    return a.build_item(eid, st, cfg)


def test_entities_in_finds_keys_and_templates():
    cfg = {
        "triggers": [{"trigger": "state", "entity_id": ["light.a", "light.b"]}],
        "actions": [
            {"target": {"entity_id": "switch.c"}},
            {
                "condition": "template",
                "value_template": "{{ is_state('sensor.d', 'on') }}",
            },
        ],
    }
    assert a.entities_in(cfg) == {"light.a", "light.b", "switch.c", "sensor.d"}


def test_groups_by_prefix():
    assert a.group_of("Routine: Morning") == "Routine"
    assert a.group_of("Scene Kino") == "Scene"
    assert a.group_of("AI_Assist foo") == "Test / AI"
    assert a.group_of("Rollladen up") == "Shades"
    assert a.group_of("Whatever x") == "Other"


def test_analyse_counts_and_ask_puts_disabled_last():
    items = [
        _item("automation.x", "Routine: X", {"actions": [{"entity_id": "light.a"}]}),
        _item(
            "automation.y", "Monitor: Y", {"actions": [{"entity_id": "light.a"}]}, "off"
        ),
        _item(
            "automation.z",
            "Scene Z",
            {"actions": [{"entity_id": "light.gone"}]},
            last=None,
        ),
    ]
    refs = {"light.a": {"state": "on"}, "light.gone": None}
    inv = a.analyse(items, refs, now=1_800_000_000.0)
    inv["items"] = items
    k = inv["kpis"]
    assert (k["total"], k["off"], k["broken"]) == (3, 1, 1)
    assert k["stale"] >= 1
    res = a.ask(inv, "which automations use light.a?")
    assert [r["entity_id"] for r in res["rows"]] == ["automation.x", "automation.y"]
    assert (res["active"], res["disabled"]) == (1, 1)
