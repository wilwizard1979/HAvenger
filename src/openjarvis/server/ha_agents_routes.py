"""Jarvis the Janitor: settings, status and the L0 watchdog (``/v1/ha/agents``).

Everything here is read-only towards Home Assistant. Issue state changes
(acknowledge / ignore / reopen) only touch the Janitor's own database. Approvals
are deliberately NOT available over HTTP.
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException

from openjarvis.agents import janitor_executor as executor
from openjarvis.agents.cloud_client import KEYCHAIN_SERVICE, CloudClient
from openjarvis.agents.clusters import build_clusters, matrix_counts
from openjarvis.agents.house import (
    ask_house,
    source_hash,
    spoken_text,
    write_house_status,
)
from openjarvis.agents.planning import (
    build_plan,
    generate_proposal,
    overview,
    plan_clusters,
    select_task,
)
from openjarvis.agents.readout import run_readout
from openjarvis.agents.reporting import generate_report, run_triage
from openjarvis.agents.store import (
    CLUSTER_KINDS,
    EFFORTS,
    ISSUE_STATES,
    SettingsError,
    get_store,
)
from openjarvis.agents.taskbook import close_task, library, write_taskbook
from openjarvis.agents.team import team_overview
from openjarvis.agents.telegram_bot import TelegramError, get_bot_service
from openjarvis.agents.watchdog import get_watchdog

_inflight: set[str] = set()
_inflight_lock = threading.Lock()

ha_agents_router = APIRouter(prefix="/v1/ha/agents", tags=["home-assistant-agents"])


def _status() -> dict[str, Any]:
    store = get_store()
    client = CloudClient(store)
    s = store.settings()
    decision = client.budget_decision("diagnose")
    return {
        "dry_run": client.is_dry_run(),
        "auto_escalation": s["auto_escalation"],
        "keychain_service": KEYCHAIN_SERVICE,
        "budget": {
            "spend_usd": round(decision.spend, 4),
            "limit_usd": decision.limit,
            "pct": round(decision.pct, 1),
            "state": decision.state,
            "by_model": [
                {
                    "model": r["model"],
                    "cost_usd": round(r["cost_usd"], 4),
                    "calls": r["calls"],
                }
                for r in store.month_breakdown()
            ],
        },
        "schedule": s["schedule"],
        "models": s["models"],
        "prices_verified": s["prices_verified"],
    }


@ha_agents_router.get("/status")
def agents_status() -> dict[str, Any]:
    return _status()


@ha_agents_router.get("/settings")
def get_settings() -> dict[str, Any]:
    return get_store().settings()


@ha_agents_router.put("/settings")
def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
    try:
        return get_store().update_settings(patch)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@ha_agents_router.get("/watchdog")
def watchdog_status() -> dict[str, Any]:
    return get_watchdog().status()


@ha_agents_router.post("/watchdog/run")
def watchdog_run() -> dict[str, Any]:
    """Run one read-only check now (no cloud call, no change in Home Assistant)."""
    return get_watchdog().tick(wait=False, manual=True)


@ha_agents_router.get("/issues")
def list_issues(state: str | None = None) -> dict[str, Any]:
    if state and state not in ISSUE_STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of {ISSUE_STATES}"
        )
    return {"issues": get_store().issues(state)}


@ha_agents_router.post("/issues/{fingerprint}/state")
def set_issue_state(fingerprint: str, body: dict[str, Any]) -> dict[str, Any]:
    new = body.get("state")
    if new not in ("open", "acknowledged", "ignored"):
        raise HTTPException(
            status_code=422, detail="state must be open, acknowledged or ignored"
        )
    store = get_store()
    if store.issue(fingerprint) is None:
        raise HTTPException(status_code=404, detail="unknown issue")
    store.set_issue_state(fingerprint, new)
    return store.issue(fingerprint) or {}


@ha_agents_router.get("/suggestions")
def suggestions() -> dict[str, Any]:
    """Critical-entity and motion-sensor lists proposed from HA device classes."""
    from openjarvis.agents.ha_collector import HaCollector

    try:
        return HaCollector().suggest()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from exc


def _clusters() -> list[dict[str, Any]]:
    store = get_store()
    return build_clusters(
        store.issues(), store.settings(), store.overrides(), store.triage_notes()
    )


@ha_agents_router.get("/clusters")
def clusters() -> dict[str, Any]:
    """Prioritised clusters and the severity x effort matrix (read-only)."""
    found = _clusters()
    return {
        "clusters": found,
        "matrix": matrix_counts(found),
    }


@ha_agents_router.put("/clusters/override")
def cluster_override(body: dict[str, Any]) -> dict[str, Any]:
    """Set (or clear, with both null) the user's effort / kind for one cluster."""
    cid, effort, kind = body.get("id"), body.get("effort"), body.get("kind")
    if not isinstance(cid, str) or not cid:
        raise HTTPException(status_code=422, detail="id is required")
    if effort is not None and effort not in EFFORTS:
        raise HTTPException(status_code=422, detail=f"effort must be one of {EFFORTS}")
    if kind is not None and kind not in CLUSTER_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {CLUSTER_KINDS}"
        )
    get_store().set_override(cid, effort, kind)
    return {"clusters": _clusters()}


@ha_agents_router.get("/reports/latest")
def latest_report() -> dict[str, Any]:
    return {"report": get_store().latest_report()}


@ha_agents_router.post("/reports/run")
def run_report(triage: bool = True) -> dict[str, Any]:
    """Build the digest now (manual: allowed past the 75 % auto-escalation pause)."""
    return generate_report(get_store(), manual=True, triage=triage)


@ha_agents_router.post("/triage/run")
def run_triage_now() -> dict[str, Any]:
    return run_triage(get_store(), CloudClient(get_store()), _clusters(), manual=True)


def _export_taskbook() -> None:
    try:
        write_taskbook(get_store())
        from openjarvis.agents.qwen_brief import write_briefs

        write_briefs(get_store())
    except Exception:  # noqa: BLE001 - the export must never break the action
        pass


@ha_agents_router.get("/overview")
def agents_overview() -> dict[str, Any]:
    """Totals for the main-page cards and the top 10 per dashboard section."""
    return overview(get_store())


@ha_agents_router.get("/plan")
def agents_plan() -> dict[str, Any]:
    """Tasks, severity x effort matrix and token / dollar estimates (read-only)."""
    return build_plan(get_store())


@ha_agents_router.put("/plan/select")
def plan_select(body: dict[str, Any]) -> dict[str, Any]:
    """Tick or untick a task. A decision about priority, never an approval."""
    tid, selected = body.get("id"), body.get("selected")
    if not isinstance(tid, str) or not isinstance(selected, bool):
        raise HTTPException(status_code=422, detail="id (string) and selected (bool)")
    select_task(get_store(), tid, selected)
    _export_taskbook()
    return build_plan(get_store())


@ha_agents_router.put("/plan/findings")
def plan_findings(body: dict[str, Any]) -> dict[str, Any]:
    """Save the owner's diagnosis of a task (empty clears it). No cloud call."""
    tid, text = body.get("id"), body.get("text", "")
    if not isinstance(tid, str) or not tid or not isinstance(text, str):
        raise HTTPException(status_code=422, detail="id and text (strings)")
    store = get_store()
    if not any(c["id"] == tid for c in plan_clusters(store)):
        raise HTTPException(status_code=404, detail="unknown task")
    store.set_findings(tid, text)
    _export_taskbook()
    return build_plan(store)


@ha_agents_router.get("/library")
def agents_library() -> dict[str, Any]:
    """Closed tasks with their solutions (the knowledge library, read-only)."""
    return {"items": library(get_store())}


@ha_agents_router.put("/plan/solution")
def plan_solution(body: dict[str, Any]) -> dict[str, Any]:
    """Save the closing text of a task (what fixed it). Needed before closing."""
    task_id, text = body.get("task_id"), body.get("text", "")
    if not isinstance(task_id, str) or not isinstance(text, str):
        raise HTTPException(status_code=422, detail="task_id and text (strings)")
    store = get_store()
    if not store.set_solution(task_id, text):
        raise HTTPException(status_code=404, detail="unknown or closed task")
    _export_taskbook()
    return build_plan(store)


@ha_agents_router.post("/plan/close")
def plan_close(body: dict[str, Any]) -> dict[str, Any]:
    """Close a task into resolved.md. Only possible once a solution text is saved."""
    task_id = body.get("task_id")
    if not isinstance(task_id, str):
        raise HTTPException(status_code=422, detail="task_id (string)")
    store = get_store()
    out = close_task(store, task_id)
    if out["status"] == "unknown_task":
        raise HTTPException(status_code=404, detail="unknown or closed task")
    if out["status"] == "needs_solution":
        raise HTTPException(status_code=409, detail="write the solution text first")
    _export_taskbook()
    return build_plan(store)


@ha_agents_router.post("/plan/proposal")
def plan_proposal(body: dict[str, Any]) -> dict[str, Any]:
    """Escalate one task: a proposal (quick), with read-only lookups (investigate)
    or with Opus (deep). Always started by the user; a cloud call unless dry-run."""
    tid, mode = body.get("id"), body.get("mode", "quick")
    if not isinstance(tid, str) or not tid:
        raise HTTPException(status_code=422, detail="id is required")
    if mode not in ("quick", "investigate", "deep"):
        raise HTTPException(
            status_code=422, detail="mode must be quick, investigate or deep"
        )
    store = get_store()
    # one escalation per task at a time: a second click must never pay twice
    with _inflight_lock:
        if tid in _inflight:
            return {"status": "already_running"}
        _inflight.add(tid)
    try:
        focus = body.get("focus")
        out = generate_proposal(
            store,
            CloudClient(store),
            tid,
            mode,
            focus=focus if isinstance(focus, str) and focus.strip() else None,
        )
    finally:
        with _inflight_lock:
            _inflight.discard(tid)
    if out["status"] == "unknown_task":
        raise HTTPException(status_code=404, detail="unknown task")
    if out["status"] == "ok":
        _export_taskbook()
    return out


@ha_agents_router.get("/team")
def agents_team() -> dict[str, Any]:
    """What each layer of the team did last (read-only)."""
    return team_overview(get_store(), get_watchdog().status())


@ha_agents_router.post("/readout/run")
def readout_run() -> dict[str, Any]:
    """Qwen (local, free) reads the latest report aloud."""
    return run_readout(get_store())


@ha_agents_router.get("/telegram/status")
def telegram_status() -> dict[str, Any]:
    return get_bot_service().status()


@ha_agents_router.post("/telegram/test")
def telegram_test() -> dict[str, Any]:
    try:
        get_bot_service().bot.send(
            "Test from Jarvis the Janitor. The bot is connected."
        )
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "sent"}


@ha_agents_router.post("/telegram/digest")
def telegram_digest() -> dict[str, Any]:
    try:
        return get_bot_service().bot.send_digest()
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@ha_agents_router.get("/executions")
def list_executions() -> dict[str, Any]:
    store = get_store()
    ex = store.settings()["executor"]
    return {
        "executions": store.executions(30),
        "executor": {"live": ex["live"], "operator": ex["operator"]},
    }


@ha_agents_router.post("/executions/request")
def request_execution(body: dict[str, Any]) -> dict[str, Any]:
    """Ask for approval of one proposal. The approval itself happens in Telegram."""
    cid = body.get("cluster_id")
    if not isinstance(cid, str) or not cid:
        raise HTTPException(status_code=422, detail="cluster_id is required")
    store = get_store()
    out = executor.request_approval(store, cid)
    if out["status"] != "awaiting_approval":
        return out
    try:
        get_bot_service().bot.request_approval(out["id"])
    except TelegramError as exc:
        executor.cancel(store, out["id"])
        raise HTTPException(
            status_code=502, detail=f"Could not reach Telegram: {exc}"
        ) from exc
    return out


@ha_agents_router.post("/executions/{exec_id}/cancel")
def cancel_execution(exec_id: int) -> dict[str, Any]:
    """Withdraw a request that is still waiting. This can never approve anything."""
    return executor.cancel(get_store(), exec_id)


@ha_agents_router.get("/house-status")
def house_status() -> dict[str, Any]:
    """The plain-English status (rebuilt now) and the text to read aloud."""
    store = get_store()
    cfg = store.settings()["qwen"]
    text, path = write_house_status(store)
    return {
        "read_aloud": cfg["read_aloud"],
        "house_status": cfg["house_status"],
        "text": text,
        "spoken": spoken_text(store, text),
        "verified_readout": spoken_text(store, text) != text,
        "hash": source_hash(text),
        "path": str(path),
    }


@ha_agents_router.post("/ask")
def ask(body: dict[str, Any]) -> dict[str, Any]:
    """Ask Qwen (local) a question about the house; answered from the status only."""
    q = body.get("question")
    if not isinstance(q, str) or not q.strip():
        raise HTTPException(status_code=422, detail="question is required")
    return ask_house(get_store(), q)
