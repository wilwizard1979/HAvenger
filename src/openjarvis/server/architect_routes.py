"""Read-only routes for the Automations page (Jarvis the Architect).

Everything here reads the local cache built by ``openjarvis.agents.architect``;
``POST /refresh`` reads Home Assistant again (read-only) in the background.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from openjarvis.agents import architect

architect_router = APIRouter(prefix="/v1/architect", tags=["architect"])


@architect_router.get("/overview")
def get_overview() -> dict[str, Any]:
    return architect.overview(architect.load_inventory())


@architect_router.post("/refresh")
def post_refresh() -> dict[str, Any]:
    return {"started": architect.refresh_async()}


@architect_router.get("/automations")
def get_automations(group: str = "", filter: str = "", q: str = "") -> dict[str, Any]:
    inv = architect.load_inventory() or {}
    return {"rows": architect.list_automations(inv, group, filter, q)}


@architect_router.get("/ask")
def get_ask(q: str = "") -> dict[str, Any]:
    inv = architect.load_inventory()
    if not inv:
        return {"error": "No inventory yet. Press Refresh first."}
    return architect.ask(inv, q)
