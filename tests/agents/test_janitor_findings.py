"""The owner's own diagnosis ("My findings") is saved per task and goes to the model."""
# ruff: noqa: F811

from __future__ import annotations

import json

from tests.agents.test_janitor_phase3_plan import (  # noqa: F401
    FAKE_PW,
    add,
    live,
    planning,
    store,
)


def task(store, client, tid):
    return next(
        t for t in planning.build_plan(store, client)["tasks"] if t["id"] == tid
    )


def test_findings_are_saved_cleared_and_sent_as_fact(store):
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, sdk = live(store, None)
    store.set_findings(
        "integration:ollama", f"IP changed after wifi to cable. password: {FAKE_PW}"
    )
    assert task(store, client, "integration:ollama")["findings"].startswith(
        "IP changed"
    )

    out = planning.generate_proposal(store, client, "integration:ollama")
    assert out["status"] == "ok"
    sent = sdk.requests[0]
    body = json.dumps(sent)
    user = sent["messages"][0]["content"]
    assert "owner_findings" in user and "IP changed after wifi to cable" in user
    assert "established fact" in json.dumps(sent["system"])
    assert FAKE_PW not in body  # redaction applies to the notes too
    t = task(store, client, "integration:ollama")
    assert t["proposal"]["findings_used"].startswith("IP changed")
    assert t["findings_changed"] is False

    store.set_findings("integration:ollama", "Different finding")
    assert task(store, client, "integration:ollama")["findings_changed"] is True

    store.set_findings("integration:ollama", "   ")
    assert store.findings() == {}


def test_no_findings_means_nothing_extra_is_sent(store):
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, sdk = live(store, None)
    planning.generate_proposal(store, client, "integration:ollama")
    assert "owner_findings" not in sdk.requests[0]["messages"][0]["content"]


def test_taskbook_collects_findings_and_analysis(store, tmp_path, monkeypatch):
    from openjarvis.agents import taskbook

    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path))
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, _ = live(store, None)
    store.set_findings("integration:ollama", "IP changed after the cable switch")
    planning.generate_proposal(store, client, "integration:ollama")
    path = taskbook.write_taskbook(store)
    text = path.read_text()
    assert path.name == "tasks.md" and "IP changed after the cable switch" in text
    assert "Based on your findings" in text and "Re-authenticate" in text
