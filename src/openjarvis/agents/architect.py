"""Read-only map of the Home Assistant automations (Jarvis the Architect, step 1-2).

Python reads every automation once (state + full config, no length cut), works out
which entities each one uses, groups them by name prefix and counts the KPIs. The
result is cached in a JSON file; nothing is ever written to Home Assistant, and no
model is involved. Questions like "which automations use light.x" are answered
from this map.

Disabled automations are kept in the map and shown (lower priority), but they are
never part of the context that a later step sends to Sonnet/Opus.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STALE_DAYS = 365
RECENT_DAYS = 7
ASK_LIMIT = 40

ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
TEMPLATE_ENTITY = re.compile(
    r"(?:states|is_state|state_attr|is_state_attr|expand|has_value)\(\s*"
    r"['\"]([a-z_]+\.[a-z0-9_]+)['\"]"
)
ENTITY_IN_TEXT = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")

# A plain string like "binary_sensor.sleep_time" counts as an entity only for these
# domains, and never under a key that holds a service or trigger type.
ENTITY_DOMAINS = frozenset(
    "alarm_control_panel automation binary_sensor button calendar camera climate "
    "counter cover device_tracker event fan group humidifier image input_boolean "
    "input_datetime input_number input_select input_text light lock media_player "
    "number person scene script select sensor siren sun switch text timer todo "
    "update vacuum water_heater weather zone".split()
)
SERVICE_KEYS = frozenset({"action", "service", "domain", "trigger", "platform", "type"})

# group name -> first word of the alias (lower case, colon removed)
GROUP_PREFIXES: dict[str, tuple[str, ...]] = {
    "Routine": ("routine",),
    "Scene": ("scene",),
    "Monitor": ("monitor",),
    "Alarm": ("alarm",),
    "Notification": ("notification",),
    "Test / AI": ("test", "ai", "ai_assist", "ha-mcp", "new"),
    "Shades": ("rollladen", "tilt", "shutter", "blinds"),
    "Climate": ("comfort", "control", "temp"),
    "Logs / Alerts": ("log", "alert", "counter", "detect", "house", "general"),
}
OTHER = "Other"
GROUP_ORDER = [*GROUP_PREFIXES, OTHER]

_lock = threading.Lock()
_state: dict[str, Any] = {"refreshing": False, "error": "", "started": 0.0}
# A read normally takes under 3 minutes; past this it is treated as hung.
REFRESH_DEAD_S = 480


def area_label(area_id: str) -> str:
    return area_id.replace("_", " ").title() if area_id else ""


def cache_path() -> Path:
    from openjarvis.core.config import DEFAULT_CONFIG_DIR

    return Path(DEFAULT_CONFIG_DIR) / "architect" / "inventory.json"


# -- pure analysis ----------------------------------------------------------------


def group_of(alias: str) -> str:
    first = re.split(r"[\s:]+", alias.strip().lower(), maxsplit=1)[0] if alias else ""
    for group, prefixes in GROUP_PREFIXES.items():
        if first in prefixes:
            return group
    return OTHER


def entities_in(config: Any) -> set[str]:
    """Entity ids an automation config points at (``entity_id`` keys + templates)."""
    found: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            for part in value.split(","):
                part = part.strip()
                if ENTITY_ID.match(part):
                    found.add(part)
        elif isinstance(value, list):
            for v in value:
                add(v)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "entity_id":
                    add(value)
                elif (
                    key not in SERVICE_KEYS
                    and isinstance(value, str)
                    and ENTITY_ID.match(value)
                    and value.split(".")[0] in ENTITY_DOMAINS
                ):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and ("{{" in node or "{%" in node):
            found.update(TEMPLATE_ENTITY.findall(node))

    walk(config)
    return found


def _device_refs(config: Any) -> int:
    """Device triggers/actions store registry ids, not entity names: count them."""
    count = 0
    if isinstance(config, dict):
        if "device_id" in config:
            count += 1
        for v in config.values():
            count += _device_refs(v)
    elif isinstance(config, list):
        for v in config:
            count += _device_refs(v)
    return count


CONTAINERS = ("scene.", "group.")


def containers_from(
    states: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    """Scene/group id -> {name, members}, from the members listed in its state."""
    out: dict[str, dict[str, Any]] = {}
    for eid, st in states.items():
        if not st or not eid.startswith(CONTAINERS):
            continue
        attrs = st.get("attributes") or {}
        members = attrs.get("entity_id")
        if isinstance(members, str):
            members = [members]
        out[eid] = {
            "name": str(attrs.get("friendly_name") or ""),
            "members": [m for m in (members or []) if isinstance(m, str)],
        }
    return out


def link_scenes(
    items: list[dict[str, Any]], containers: dict[str, dict[str, Any]]
) -> None:
    """Scenes/groups an automation uses, and the entities they bring in (``via``)."""
    for it in items:
        used = sorted(e for e in it["entities"] if e in containers)
        via: dict[str, list[str]] = {}
        for c in used:
            for member in containers[c]["members"]:
                if member not in it["entities"]:
                    via.setdefault(member, []).append(c)
        it["scenes"] = used
        it["via"] = via


def all_entities(it: dict[str, Any]) -> set[str]:
    """Entities an automation touches directly or through a scene/group."""
    return set(it["entities"]) | set(it.get("via", {}))


def _age_days(ts: str | None, now: float) -> float | None:
    if not ts:
        return None
    from datetime import datetime

    try:
        return (now - datetime.fromisoformat(ts).timestamp()) / 86400
    except ValueError:
        return None


def build_item(
    entity_id: str, state: dict[str, Any] | None, config: dict[str, Any] | None
) -> dict[str, Any]:
    attrs = (state or {}).get("attributes") or (state or {}).get("attrs") or {}
    alias = (config or {}).get("alias") or attrs.get("friendly_name") or entity_id
    return {
        "entity_id": entity_id,
        "alias": alias,
        "group": group_of(alias),
        "state": (state or {}).get("state", "unknown"),
        "last_triggered": attrs.get("last_triggered"),
        "mode": attrs.get("mode") or (config or {}).get("mode"),
        "description": (config or {}).get("description", ""),
        "config": config,
        "entities": sorted(entities_in(config)) if config else [],
        "device_refs": _device_refs(config) if config else 0,
        "readable": config is not None,
    }


def analyse(
    items: list[dict[str, Any]],
    ref_states: dict[str, dict[str, Any] | None],
    now: float | None = None,
) -> dict[str, Any]:
    """Add reference health + age to every item and compute the KPIs."""
    now = now if now is not None else time.time()
    stale = recent = broken = off = unavailable = unreadable = 0
    for it in items:
        missing = [e for e in it["entities"] if ref_states.get(e) is None]
        gone = [
            e
            for e in it["entities"]
            if ref_states.get(e) is not None
            and str(ref_states[e].get("state")) in ("unavailable", "unknown")
            and not e.startswith(("automation.", "script.", "scene."))
        ]
        it["missing"] = missing
        it["offline"] = gone
        via = it.get("via", {})
        it["missing_via"] = [
            f"{via[e][0]} -> {e}" for e in sorted(via) if ref_states.get(e) is None
        ]
        it["offline_via"] = [
            f"{via[e][0]} -> {e}"
            for e in sorted(via)
            if ref_states.get(e) is not None
            and str(ref_states[e].get("state")) in ("unavailable", "unknown")
        ]
        it["broken"] = bool(missing or gone or it["missing_via"] or it["offline_via"])
        age = _age_days(it.get("last_triggered"), now)
        it["age_days"] = None if age is None else round(age, 1)
        it["stale"] = age is None or age > STALE_DAYS
        it["recent"] = age is not None and age <= RECENT_DAYS
        it["disabled"] = it["state"] == "off"
        off += it["disabled"]
        unavailable += it["state"] == "unavailable"
        unreadable += not it["readable"]
        broken += it["broken"]
        stale += it["stale"]
        recent += it["recent"]
    groups: dict[str, dict[str, int]] = {
        g: {"count": 0, "broken": 0, "stale": 0, "disabled": 0} for g in GROUP_ORDER
    }
    for it in items:
        g = groups[it["group"]]
        g["count"] += 1
        g["broken"] += it["broken"]
        g["stale"] += it["stale"]
        g["disabled"] += it["disabled"]
    return {
        "kpis": {
            "total": len(items),
            "on": len(items) - off - unavailable,
            "off": off,
            "unavailable": unavailable,
            "unreadable": unreadable,
            "broken": broken,
            "stale": stale,
            "recent": recent,
        },
        "groups": [{"name": n, **groups[n]} for n in GROUP_ORDER if groups[n]["count"]],
    }


# -- reading from Home Assistant ----------------------------------------------------


def _config_of(client: Any, entity_id: str) -> dict[str, Any] | None:
    try:
        raw = client.call_tool("ha_config_get_automation", {"identifier": entity_id})
        data = json.loads(raw["content"][0]["text"])
    except Exception as exc:  # noqa: BLE001 - one unreadable automation is not fatal
        logger.info("Architect: cannot read %s: %s", entity_id, exc)
        return None
    cfg = data.get("config") if isinstance(data, dict) else None
    return cfg if isinstance(cfg, dict) else None


def entity_areas(ha: Any) -> dict[str, str]:
    """entity_id -> area id, one search per area (read-only)."""
    try:
        overview = ha.read("ha_get_overview")
        areas = [
            a
            for a, info in (overview.get("area_analysis") or {}).items()
            if (info or {}).get("count")
        ]
    except Exception as exc:  # noqa: BLE001 - areas are a bonus, not required
        logger.info("Architect: no area list: %s", exc)
        return {}
    out: dict[str, str] = {}
    for area in areas:
        offset = 0
        while True:
            try:
                r = ha.read(
                    "ha_search",
                    {"query": "", "area_filter": area, "limit": 500, "offset": offset},
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("Architect: area %s failed: %s", area, exc)
                break
            for e in r.get("entities", []):
                out.setdefault(e["entity_id"], area)
            nxt = r.get("entity_next_offset")
            if not r.get("entity_has_more") or not nxt or nxt <= offset:
                break
            offset = int(nxt)
    return out


def entity_info(
    ids: list[str],
    states: dict[str, dict[str, Any] | None],
    area_of: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Name, state and area of every entity the automations use (for the context)."""
    out: dict[str, dict[str, str]] = {}
    for eid in ids:
        st = states.get(eid) or {}
        attrs = st.get("attributes") or {}
        out[eid] = {
            "name": str(attrs.get("friendly_name") or ""),
            "state": str(st.get("state", "missing" if not st else "")),
            "area": area_label(area_of.get(eid, "")),
        }
    return out


def assign_areas(items: list[dict[str, Any]], area_of: dict[str, str]) -> None:
    """An automation belongs to the areas of the entities it uses, most used first."""
    for it in items:
        counts: dict[str, int] = {}
        for e in all_entities(it):
            a = area_of.get(e)
            if a:
                counts[a] = counts.get(a, 0) + 1
        ranked = sorted(counts, key=lambda a: (-counts[a], a))
        it["areas"] = ranked[:3]
        it["area"] = ranked[0] if ranked else ""


def collect(ha: Any | None = None, now: float | None = None) -> dict[str, Any]:
    """Read all automations from ha-mcp (read-only) and build the inventory."""
    from openjarvis.agents.ha_collector import HaCollector

    own = ha is None
    if own:
        from openjarvis.server.ha_dashboard_routes import _HA

        ha = _HA()
    try:
        listed = HaCollector._list_domain(ha, "automation")
        ids = sorted({e["entity_id"] for e in listed if e.get("entity_id")})
        states = HaCollector._states(ha, ids)
        items = [build_item(i, states.get(i), _config_of(ha._client, i)) for i in ids]
        refs = sorted({e for it in items for e in it["entities"]} - set(ids))
        ref_states = HaCollector._states(ha, refs)
        ref_states.update({i: states.get(i) for i in ids})
        containers = containers_from(ref_states)
        link_scenes(items, containers)
        extra = sorted(
            {m for c in containers.values() for m in c["members"]} - set(ref_states)
        )
        ref_states.update(HaCollector._states(ha, extra))
        area_of = entity_areas(ha)
        assign_areas(items, area_of)
        info = entity_info(sorted(set(refs) | set(extra)), ref_states, area_of)
    finally:
        if own:
            ha.close()
    result = analyse(items, ref_states, now)
    result["items"] = items
    result["entity_info"] = info
    result["scenes"] = containers
    result["fetched_at"] = now if now is not None else time.time()
    return result


# -- cache + background refresh -----------------------------------------------------


def load_inventory() -> dict[str, Any] | None:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_inventory(inv: dict[str, Any]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(inv), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Architect: could not save inventory: %s", exc)


def refresh_async() -> bool:
    """Start a read in the background. False when one is already running."""
    with _lock:
        if _state["refreshing"] and time.time() - _state["started"] < REFRESH_DEAD_S:
            return False
        _state.update(refreshing=True, error="", started=time.time())

    def run() -> None:
        try:
            save_inventory(collect())
        except Exception as exc:  # noqa: BLE001 - show it on the page
            logger.warning("Architect refresh failed: %s", exc)
            _state["error"] = str(exc)[:200]
        finally:
            _state["refreshing"] = False

    threading.Thread(target=run, name="architect-refresh", daemon=True).start()
    return True


def status() -> dict[str, Any]:
    st = dict(_state)
    if st["refreshing"] and time.time() - st["started"] >= REFRESH_DEAD_S:
        st.update(
            refreshing=False, error="The last read did not finish. Press Refresh."
        )
    return st


# -- views for the page --------------------------------------------------------------


def summary_row(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": it["entity_id"],
        "alias": it["alias"],
        "group": it["group"],
        "state": it["state"],
        "disabled": it["disabled"],
        "broken": it["broken"],
        "stale": it["stale"],
        "age_days": it["age_days"],
        "last_triggered": it["last_triggered"],
        "entities": len(it["entities"]),
        "area": area_label(it.get("area", "")),
        "scenes": len(it.get("scenes", [])),
        "missing_via": it.get("missing_via", [])[:5],
        "offline_via": it.get("offline_via", [])[:5],
        "missing": it["missing"][:5],
        "offline": it["offline"][:5],
        "readable": it["readable"],
    }


def _order(it: dict[str, Any]) -> tuple[int, str]:
    # active first, disabled last
    return (1 if it["disabled"] else 0, it["alias"].lower())


def list_automations(
    inv: dict[str, Any], group: str = "", flt: str = "", q: str = "", area: str = ""
) -> list[dict[str, Any]]:
    rows = list(inv.get("items", []))
    if group:
        rows = [r for r in rows if r["group"] == group]
    if area:
        rows = [r for r in rows if area in r.get("areas", [])]
    if flt == "disabled":
        rows = [r for r in rows if r["disabled"]]
    elif flt == "broken":
        rows = [r for r in rows if r["broken"]]
    elif flt == "stale":
        rows = [r for r in rows if r["stale"]]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in r["alias"].lower() or needle in r["entity_id"].lower()
        ]
    return [summary_row(r) for r in sorted(rows, key=_order)]


def overview(inv: dict[str, Any] | None) -> dict[str, Any]:
    st = status()
    if not inv:
        return {"ready": False, "refreshing": st["refreshing"], "error": st["error"]}
    return {
        "ready": True,
        "refreshing": st["refreshing"],
        "error": st["error"],
        "fetched_at": inv.get("fetched_at"),
        "kpis": inv["kpis"],
        "groups": inv["groups"],
        "areas": area_counts(inv),
    }


def area_counts(inv: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for it in inv.get("items", []):
        for a in it.get("areas", []):
            counts[a] = counts.get(a, 0) + 1
    return [
        {"id": a, "name": area_label(a), "count": n}
        for a, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def ask(inv: dict[str, Any], question: str) -> dict[str, Any]:
    """Answer 'which automations use X' from the map, without a model.

    X is an entity id in the question, or words that match an entity id, an
    automation name or the entities it uses. Active automations come first;
    disabled ones are listed after them.
    """
    ids = [m for m in ENTITY_IN_TEXT.findall(question.lower()) if "." in m]
    ids = [i for i in ids if i.split(".")[0] in _domains(inv)]
    words = [
        w for w in re.findall(r"[a-z0-9_]{3,}", question.lower()) if w not in STOP_WORDS
    ]
    hits: list[tuple[dict[str, Any], list[str], bool]] = []
    for it in inv.get("items", []):
        via_map = it.get("via", {})
        if ids:
            direct = [e for e in it["entities"] if e in ids]
            indirect = [
                f"{m} via {c[0]}"
                for m, c in via_map.items()
                if m in ids and m not in it["entities"]
            ]
        else:
            direct = [e for e in it["entities"] if words and all(w in e for w in words)]
            indirect = [
                f"{m} via {c[0]}"
                for m, c in via_map.items()
                if words and all(w in m for w in words)
            ]
            if words and all(w in it["alias"].lower() for w in words):
                direct = direct or [it["alias"]]
        if direct or indirect:
            hits.append((it, direct + indirect, not direct))
    # active first; then direct before indirect; then by name
    hits.sort(key=lambda h: (h[0]["disabled"], h[2], h[0]["alias"].lower()))
    rows = []
    for it, via, indirect in hits[:ASK_LIMIT]:
        row = summary_row(it)
        row["via"] = via[:5]
        row["indirect"] = indirect
        rows.append(row)
    active = sum(1 for it, _, _ in hits if not it["disabled"])
    return {
        "question": question,
        "matched_on": ids or words,
        "count": len(hits),
        "active": active,
        "disabled": len(hits) - active,
        "indirect": sum(1 for _, _, ind in hits if ind),
        "rows": rows,
    }


def relations(inv: dict[str, Any], ident: str) -> dict[str, Any]:
    """A plain-text tree: automation -> scenes -> entities, or entity -> who uses it."""
    info = inv.get("entity_info", {})
    scenes = inv.get("scenes", {})
    by_id = {it["entity_id"]: it for it in inv.get("items", [])}

    def ent(e: str) -> str:
        i = info.get(e, {})
        bits = [b for b in (i.get("name"), i.get("area"), i.get("state")) if b]
        return e + (f" ({', '.join(bits)})" if bits else "")

    lines: list[str] = []
    it = by_id.get(ident)
    if it:
        title = f"{it['alias']} ({it['state']})"
        lines.append(f"uses directly ({len(it['entities'])}):")
        lines += [f"  {ent(e)}" for e in it["entities"][:15]]
        if len(it["entities"]) > 15:
            lines.append(f"  (and {len(it['entities']) - 15} more...)")
        for sc in it.get("scenes", [])[:10]:
            members = scenes.get(sc, {}).get("members", [])
            lines.append(f"scene {sc} ({scenes.get(sc, {}).get('name', '')}) sets:")
            lines += [f"  {ent(e)}" for e in members[:8]]
            if len(members) > 8:
                lines.append(f"  (and {len(members) - 8} more...)")
        for label, key in (("missing", "missing_via"), ("offline", "offline_via")):
            for x in it.get(key, [])[:5]:
                lines.append(f"PROBLEM {label} through a scene: {x}")
    else:
        title = ent(ident)
        direct = [x for x in by_id.values() if ident in x["entities"]]
        in_scenes = sorted(sc for sc, c in scenes.items() if ident in c["members"])
        lines.append(f"used directly by {len(direct)} automation(s):")
        lines += [
            f"  {x['alias']}{' (off)' if x['disabled'] else ''}" for x in direct[:15]
        ]
        for sc in in_scenes[:10]:
            users = [x for x in by_id.values() if sc in x.get("scenes", [])]
            lines.append(f"in scene {sc}, used by {len(users)} automation(s):")
            lines += [
                f"  {x['alias']}{' (off)' if x['disabled'] else ''}" for x in users[:10]
            ]
    return {"id": ident, "title": title, "lines": lines}


STOP_WORDS = {
    "which",
    "what",
    "automation",
    "automations",
    "use",
    "uses",
    "using",
    "related",
    "entity",
    "tell",
    "find",
    "the",
    "that",
    "this",
    "for",
    "with",
    "are",
    "show",
    "list",
    "all",
    "any",
    "and",
    "from",
}


def _domains(inv: dict[str, Any]) -> set[str]:
    out = inv.get("_domains")
    if out is None:
        out = {e.split(".")[0] for it in inv.get("items", []) for e in all_entities(it)}
        inv["_domains"] = out
    return out
