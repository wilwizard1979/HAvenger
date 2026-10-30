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


def _with_links(case: dict[str, Any], inv: dict[str, Any] | None) -> dict[str, Any]:
    links = architect.answer_links(case.get("answer", ""), inv) if inv else []
    return {**case, "links": links}


@architect_router.get("/overview")
def get_overview() -> dict[str, Any]:
    from openjarvis.agents.store import get_store

    out = architect.overview(architect.load_inventory())
    store = get_store()
    out["spent_usd"] = round(architect_recommend.spend_usd(store), 4)
    out["limit_usd"] = architect_recommend.limit_usd(store)
    out["ha_url"] = architect.ha_base()
    return out


class HaUrlBody(BaseModel):
    url: str


@architect_router.post("/export")
def export_files() -> dict[str, Any]:
    """Write automations.yaml, scenes.yaml and kpis.md for Claude web (local only)."""
    from openjarvis.agents import architect_export

    inv = architect.load_inventory()
    if not inv:
        return {"error": "No automation map yet. Press Refresh first."}
    return architect_export.export(inv)


@architect_router.post("/ha-url")
def post_ha_url(body: HaUrlBody) -> dict[str, Any]:
    """Where the edit links point to (your Home Assistant address)."""
    try:
        return {"ha_url": architect.set_ha_base(body.url)}
    except ValueError as exc:
        return {"error": str(exc)}


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
    return {"case": _with_links(case, inv)}


@architect_router.get("/cases")
def get_cases() -> dict[str, Any]:
    inv = architect.load_inventory()
    cases = list(reversed(architect_recommend.load_cases()))[:30]
    return {"cases": [_with_links(c, inv) for c in cases]}


@architect_router.get("/relations")
def get_relations(id: str = "") -> dict[str, Any]:
    inv = architect.load_inventory()
    if not inv or not id:
        return {"error": "No inventory yet. Press Refresh first."}
    return architect.relations(inv, id)


@architect_router.post("/cases/{case_id}/request-approval")
def post_request_approval(case_id: str) -> dict[str, Any]:
    """Ask for the Telegram approval of a case's edit. Approving happens only there."""
    from openjarvis.agents import architect_apply, janitor_executor
    from openjarvis.agents.store import get_store
    from openjarvis.agents.telegram_bot import TelegramError, get_bot_service

    case = next(
        (c for c in architect_recommend.load_cases() if c["id"] == case_id), None
    )
    inv = architect.load_inventory()
    if case is None or not inv:
        return {"error": "Unknown case or no inventory yet."}
    store = get_store()
    out = architect_apply.request_approval(store, case, inv)
    if out["status"] != "awaiting_approval":
        return out
    try:
        get_bot_service().bot.request_approval(out["id"])
    except TelegramError as exc:
        janitor_executor.cancel(store, out["id"])
        return {"error": f"Could not reach Telegram: {exc}"}
    architect_recommend.update_case(case_id, execution_id=out["id"])
    return out
