from openjarvis.agents import architect_export


def _inv():
    return {
        "kpis": {
            "total": 2,
            "on": 1,
            "off": 1,
            "unavailable": 0,
            "unreadable": 1,
            "broken": 1,
            "stale": 0,
            "recent": 1,
        },
        "groups": [
            {"name": "Routine", "count": 2, "broken": 1, "stale": 0, "disabled": 1}
        ],
        "scenes": {"scene.a": {"name": "A", "members": ["light.x"]}},
        "items": [
            {
                "entity_id": "automation.a",
                "alias": "A",
                "broken": True,
                "missing": ["light.gone"],
                "offline": [],
                "config": {"id": "1", "alias": "A", "password": "hunter2hunter2"},
            },
            {"entity_id": "automation.b", "alias": "B", "config": None},
        ],
    }


def test_export_writes_three_files_and_redacts(tmp_path):
    res = architect_export.export(_inv(), tmp_path)
    assert set(res["files"]) == {"automations.yaml", "scenes.yaml", "kpis.md"}
    text = (tmp_path / "automations.yaml").read_text()
    assert "hunter2hunter2" not in text
    assert "automation.b" in text.splitlines()[1]  # listed as not readable
    assert "light.x" in (tmp_path / "scenes.yaml").read_text()
    assert "light.gone" in (tmp_path / "kpis.md").read_text()
