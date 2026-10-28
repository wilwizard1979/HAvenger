"""Qwen gets a lean index and only the detail file a question needs."""
# ruff: noqa: F811

from __future__ import annotations

from openjarvis.agents import house, qwen_brief
from tests.agents.test_janitor_phase3_plan import add, store  # noqa: F401


def test_keywords_pick_the_topic():
    assert qwen_brief.pick_topics("which open issues do I have?")[0] == "open-tasks"
    assert qwen_brief.pick_topics("list the motion sensors with low battery")[0] == (
        "devices"
    )
    assert qwen_brief.pick_topics("how did we fix it, solutions found")[0] == (
        "solutions"
    )
    assert qwen_brief.pick_topics("what projects are in progress")[0] == "projects"
    # a weaker second match is dropped, so Qwen reads one small file
    assert qwen_brief.pick_topics("list the motion sensors with battery failed") == [
        "devices"
    ]
    assert qwen_brief.pick_topics("zebra") == ["status"]  # nothing matches


def test_index_is_lean_and_every_topic_has_a_file(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TASKBOOK_DIR", str(tmp_path))
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    files = qwen_brief.write_briefs(store)
    index = files[qwen_brief.INDEX_FILE]
    for fname, title, _ in qwen_brief.TOPICS.values():
        assert fname in index and fname in files
        assert (tmp_path / "qwen" / fname).exists()
    assert "Ollama" not in index  # the index holds headlines, not details
    assert "ollama" in files["qwen-open-tasks.md"].lower()
    assert "findings" not in files["qwen-open-tasks.md"].lower()


def test_ask_house_reads_only_the_matching_file(store, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TASKBOOK_DIR", str(tmp_path))
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    seen = {}

    def fake(model, system, prompt):
        seen["prompt"] = prompt
        return {"text": "ok", "tokens_in": 1, "tokens_out": 1}

    house.ask_house(store, "which open issues do I have?", generate=fake)
    assert "# Open tasks" in seen["prompt"]
    assert "# Projects" not in seen["prompt"]
