"""Jarvis the Janitor: settings, status and the L0 watchdog (``/v1/ha/agents``).

Everything here is read-only towards Home Assistant. Issue state changes
(acknowledge / ignore / reopen) only touch the Janitor's own database. Approvals
are deliberately NOT available over HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from openjarvis.agents.cloud_client import KEYCHAIN_SERVICE, CloudClient
from openjarvis.agents.store import ISSUE_STATES, SettingsError, get_store
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
