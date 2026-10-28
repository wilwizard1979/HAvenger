"""Read-only routes for the Automations page (Jarvis the Architect).

Everything here reads the local cache built by ``openjarvis.agents.architect``;
``POST /refresh`` reads Home Assistant again (read-only) in the background.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from openjarvis.agents import architect, architect_context, architect_recommend

architect_router = APIRouter(prefix="/v1/architect", tags=["architect"])


@architect_router.get("/overview")
def get_overview() -> dict[str, Any]:
    from openjarvis.agents.store import get_store

    out = architect.overview(architect.load_inventory())
    store = get_store()
    out["spent_usd"] = round(architect_recommend.spend_usd(store), 4)
    out["limit_usd"] = architect_recommend.limit_usd(store)
    return out


@architect_router.post("/refresh")
def post_refresh() -> dict[str, Any]:
    return {"started": architect.refresh_async()}


@architect_router.get("/automations")
def get_automations(
    group: str = "", filter: str = "", q: str = "", area: str = ""
) -> dict[str, Any]:
    inv = architect.load_inventory() or {}
    return {"rows": architect.list_automations(inv, group, filter, q, area)}


@architect_router.get("/ask")
def get_ask(q: str = "") -> dict[str, Any]:
    inv = architect.load_inventory()
    if not inv:
        return {"error": "No inventory yet. Press Refresh first."}
    return architect.ask(inv, q)


def _picked(pick: str) -> list[str]:
    return [p for p in pick.split(",") if p][:3]


@architect_router.get("/query")
def get_query(q: str = "", pick: str = "", force: str = "") -> dict[str, Any]:
    """One box: a lookup is answered here, a change/create gets a preview."""
    inv = architect.load_inventory()
    if not inv:
        return {"error": "No inventory yet. Press Refresh first."}
    if not q.strip():
        return {"error": "Type a question first."}
    intent = (
        force
        if force in ("lookup", "change", "create")
        else (architect_context.classify(q))
    )
    if intent == "lookup":
        return {"intent": "lookup", "lookup": architect.ask(inv, q)}
    return {
        "intent": intent,
        "preview": architect_recommend.preview(inv, q, _picked(pick), intent=intent),
    }


class RecommendBody(BaseModel):
    q: str
    model: str
    pick: list[str] = []
    intent: str = ""


@architect_router.post("/recommend")
def post_recommend(body: RecommendBody) -> dict[str, Any]:
    """The only route that can reach Sonnet/Opus. The page calls it on a click."""
    inv = architect.load_inventory()
    if not inv:
        return {"error": "No inventory yet. Press Refresh first."}
    try:
        case = architect_recommend.recommend(
            inv, body.q, body.model, body.pick[:3], intent=body.intent
        )
    except architect_recommend.ArchitectRefused as exc:
        return {"error": str(exc)}
    return {"case": case}


@architect_router.get("/cases")
def get_cases() -> dict[str, Any]:
    return {"cases": list(reversed(architect_recommend.load_cases()))[:30]}
