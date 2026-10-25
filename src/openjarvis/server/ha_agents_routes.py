"""Jarvis the Janitor: settings, status and the L0 watchdog (``/v1/ha/agents``).

Everything here is read-only towards Home Assistant. Issue state changes
(acknowledge / ignore / reopen) only touch the Janitor's own database. Approvals
are deliberately NOT available over HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from openjarvis.agents.cloud_client import KEYCHAIN_SERVICE, CloudClient
from openjarvis.agents.clusters import build_clusters, matrix_counts
from openjarvis.agents.planning import (
    build_plan,
    generate_proposal,
    overview,
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
from openjarvis.agents.team import team_overview
from openjarvis.agents.watchdog import get_watchdog

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
    return get_watchdog().tick(wait=False)


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
    return build_plan(get_store())


@ha_agents_router.post("/plan/proposal")
def plan_proposal(body: dict[str, Any]) -> dict[str, Any]:
    """Ask for the proposal of one task (a cloud call unless dry-run)."""
    tid = body.get("id")
    if not isinstance(tid, str) or not tid:
        raise HTTPException(status_code=422, detail="id is required")
    store = get_store()
    out = generate_proposal(store, CloudClient(store), tid)
    if out["status"] == "unknown_task":
        raise HTTPException(status_code=404, detail="unknown task")
    return out


@ha_agents_router.get("/team")
def agents_team() -> dict[str, Any]:
    """What each layer of the team did last (read-only)."""
    return team_overview(get_store(), get_watchdog().status())


@ha_agents_router.post("/readout/run")
def readout_run() -> dict[str, Any]:
    """Qwen (local, free) reads the latest report aloud."""
    return run_readout(get_store())
