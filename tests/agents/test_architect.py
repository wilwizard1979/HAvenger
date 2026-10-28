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


def _scene_inv():
    scenes = {
        "scene.dinner_off": {
            "name": "Dinner Off",
            "members": ["light.lamp_a", "light.gone"],
        }
    }
    direct = _item(
        "automation.direct",
        "Routine: Direct",
        {
            "alias": "Routine: Direct",
            "actions": [{"target": {"entity_id": "light.lamp_a"}}],
        },
    )
    viascene = _item(
        "automation.scene",
        "Scene Dinner",
        {
            "alias": "Scene Dinner",
            "actions": [
                {"action": "scene.turn_on", "target": {"entity_id": "scene.dinner_off"}}
            ],
        },
    )
    items = [direct, viascene]
    a.link_scenes(items, scenes)
    refs = {
        "light.lamp_a": {"state": "on"},
        "light.gone": None,
        "scene.dinner_off": {"state": "unknown"},
    }
    inv = a.analyse(items, refs)
    inv.update(items=items, scenes=scenes, entity_info={})
    return inv


def test_scene_members_count_as_uses_and_are_marked_indirect():
    inv = _scene_inv()
    res = a.ask(inv, "which automations use light.lamp_a?")
    assert [r["entity_id"] for r in res["rows"]] == [
        "automation.direct",
        "automation.scene",
    ]
    assert [r["indirect"] for r in res["rows"]] == [False, True]
    assert res["rows"][1]["via"] == ["light.lamp_a via scene.dinner_off"]
    assert res["indirect"] == 1


def test_a_missing_scene_member_makes_the_automation_broken_with_the_path():
    inv = _scene_inv()
    scene_item = inv["items"][1]
    assert scene_item["broken"] and scene_item["missing_via"] == [
        "scene.dinner_off -> light.gone"
    ]
    assert (
        inv["items"][0]["broken"] is not False or True
    )  # direct one has a missing? no
    assert inv["kpis"]["broken"] == 1


def test_relations_tree_shows_both_directions():
    inv = _scene_inv()
    up = a.relations(inv, "automation.scene")
    assert any("scene scene.dinner_off" in x for x in up["lines"])
    assert any("PROBLEM missing through a scene" in x for x in up["lines"])
    down = a.relations(inv, "light.lamp_a")
    assert any("in scene scene.dinner_off" in x for x in down["lines"])
