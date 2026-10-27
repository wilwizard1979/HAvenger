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


def test_task_ids_close_and_seen_before(store, tmp_path, monkeypatch):
    import time

    from openjarvis.agents import taskbook

    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path))
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    add(store, "repair", "smtp/a", "smtp failed", domain="smtp")
    client, sdk = live(store, None)
    day = time.strftime("%Y_%m_%d")
    ids = {t["id"]: t["task_id"] for t in planning.build_plan(store, client)["tasks"]}
    assert sorted(ids.values()) == [f"{day}_001", f"{day}_002"]
    assert planning.build_plan(store, client)["tasks"][0]["task_id"] in ids.values()

    oid = ids["integration:ollama"]
    assert taskbook.close_task(store, oid)["status"] == "needs_solution"
    store.set_findings("integration:ollama", "IP changed")
    assert store.set_solution(oid, "Updated the URL to the cable IP")
    assert taskbook.close_task(store, oid)["status"] == "ok"
    store.set_facts({"checked_at": time.time() + 10})  # a check ran after closing
    assert store.findings() == {}  # working data is gone, the record keeps the story

    # the issue is gone, so the closed task stays closed and a new one is not made
    # until the problem returns: then a NEW id remembers the old one
    add(store, "repair", "ollama/b", "Ollama unreachable again", domain="ollama")
    t = next(
        x
        for x in planning.build_plan(store, client)["tasks"]
        if x["id"] == "integration:ollama"
    )
    assert t["task_id"] == f"{day}_003" and t["seen_before"] == oid
    planning.generate_proposal(store, client, "integration:ollama")
    user = sdk.requests[-1]["messages"][0]["content"]
    assert "seen_before" in user and "cable IP" in user

    taskbook.write_taskbook(store)
    resolved = (tmp_path / "resolved.md").read_text()
    assert f"## {oid}" in resolved and "Updated the URL to the cable IP" in resolved
    tasks_md = (tmp_path / "tasks.md").read_text()
    assert f"seen before: {oid}" in tasks_md and f"## {day}_003" in tasks_md


def test_vanished_cluster_is_cleared_and_needs_a_solution(store):
    fp = add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, _ = live(store, None)
    tid = planning.build_plan(store, client)["tasks"][0]["task_id"]
    store.set_issue_state(fp, "resolved")
    plan = planning.build_plan(store, client)
    assert plan["tasks"] == [] and plan["cleared"][0]["task_id"] == tid
    # it comes back before anyone closed it: same task, open again
    store.set_issue_state(fp, "open")
    plan = planning.build_plan(store, client)
    assert plan["cleared"] == [] and plan["tasks"][0]["task_id"] == tid


def test_closed_task_stays_gone_until_the_next_check(store):
    fp = add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, _ = live(store, None)
    store.set_facts({"checked_at": 1000.0})
    oid = planning.build_plan(store, client)["tasks"][0]["task_id"]
    store.set_solution(oid, "fixed in HA")
    from openjarvis.agents import taskbook

    taskbook.close_task(store, oid, now=2000.0)
    # the issue is still in the database but no check has run since closing
    plan = planning.build_plan(store, client)
    assert plan["tasks"] == [] and store.awaiting_check(1000.0) == {oid}
    # next check: the issue is still there -> a new task remembering the old one
    store.set_facts({"checked_at": 3000.0})
    plan = planning.build_plan(store, client)
    assert plan["tasks"][0]["seen_before"] == oid
    assert plan["tasks"][0]["task_id"] != oid
    # ids never collide after a task row is removed
    del fp


def test_overwritten_findings_are_kept_in_a_log(store):
    store.set_findings("integration:ollama", "first")
    store.set_findings("integration:ollama", "")
    rows = store._all("SELECT text FROM findings_log")
    assert [r["text"] for r in rows] == ["first"]


def test_no_check_yet_means_a_closed_task_stays_gone(store):
    add(store, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    client, _ = live(store, None)
    oid = planning.build_plan(store, client)["tasks"][0]["task_id"]
    store.set_solution(oid, "fixed")
    from openjarvis.agents import taskbook

    taskbook.close_task(store, oid)
    assert planning.build_plan(store, client)["tasks"] == []


def test_api_findings_solution_close_library(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("JARVIS_JANITOR_DIR", str(tmp_path))
    store_mod.reset_store_for_tests()
    s = store_mod.get_store()
    add(s, "repair", "ollama/a", "Ollama unreachable", domain="ollama")
    app = FastAPI()
    app.include_router(ha_agents_router)
    http = TestClient(app)
    base = "/v1/ha/agents"

    assert (
        http.put(f"{base}/plan/findings", json={"id": "nope", "text": "x"}).status_code
        == 404
    )
    assert http.put(f"{base}/plan/findings", json={"id": 5}).status_code == 422
    r = http.put(
        f"{base}/plan/findings", json={"id": "integration:ollama", "text": "IP"}
    )
    task = r.json()["tasks"][0]
    assert task["findings"] == "IP" and (tmp_path / "tasks.md").exists()

    tid = task["task_id"]
    assert http.post(f"{base}/plan/close", json={"task_id": tid}).status_code == 409
    assert http.post(f"{base}/plan/close", json={"task_id": "x"}).status_code == 404
    assert (
        http.put(
            f"{base}/plan/solution", json={"task_id": "x", "text": "a"}
        ).status_code
        == 404
    )
    http.put(f"{base}/plan/solution", json={"task_id": tid, "text": "new url"})
    out = http.post(f"{base}/plan/close", json={"task_id": tid})
    assert out.status_code == 200 and out.json()["tasks"] == []
    items = http.get(f"{base}/library").json()["items"]
    assert items[0]["task_id"] == tid and items[0]["solution"] == "new url"
    assert items[0]["awaiting_check"] is True
    assert "new url" in (tmp_path / "resolved.md").read_text()
    # closing twice is refused
    assert http.post(f"{base}/plan/close", json={"task_id": tid}).status_code == 404
