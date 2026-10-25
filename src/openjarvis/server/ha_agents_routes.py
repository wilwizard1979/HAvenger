"""Jarvis the Janitor: settings and status (``/v1/ha/agents``).

Phase 1 exposes only what the UI needs to show and edit the configuration and
the month's spend. Approvals are deliberately NOT available over HTTP.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from openjarvis.agents.cloud_client import KEYCHAIN_SERVICE, CloudClient
from openjarvis.agents.store import SettingsError, get_store

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
