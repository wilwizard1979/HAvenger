"""Read-only Home Assistant maintenance dashboard (``GET /v1/ha/dashboard``).

Collects health numbers from ha-mcp and returns one small JSON document for the
GUI cards. Safety rules:

* Only the read calls in ``_READ_CALLS`` are ever sent to ha-mcp, so the
  dashboard can never change anything in Home Assistant.
* Repair placeholders and raw config are never returned (they can contain
  secrets); only ids, domains and severities are.
* Log lines are ANSI-stripped, secret-redacted and truncated.
* Results are cached; ``?refresh=1`` forces a new collection.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

CACHE_TTL_S = 120
COLLECT_TIMEOUT_S = 90
LOG_LINES = 400
MOTION_WORDS = ("motion", "presence", "occupancy", "pir", "bewegung")
FAILED_ENTRY_STATES = ("setup_error", "setup_retry", "not_loaded", "migration_error")

# Every ha-mcp call the dashboard may make: (direct tool, proxy tool name).
_READ_CALLS = frozenset(
    {
        "ha_get_overview",
        "ha_get_integration",
        "ha_get_logs",
        "ha_search",
        "ha_get_state",
    }
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SECRET = re.compile(
    r"(?i)((?:password|passwd|token|secret|api[_-]?key|authorization|bearer)[\"'\s:=]+)[^\s\"',;]+"
)
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)? (?P<level>[A-Z]+) "
    r"\((?P<thread>[^)]*)\) \[(?P<logger>[^\]]*)\] (?P<msg>.*)$"
)

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-dashboard")


def _load_server() -> dict[str, Any]:
    from openjarvis.core.config import load_config, resolve_mcp_servers

    cfg = load_config()
    servers = resolve_mcp_servers(cfg.tools.mcp.servers, cfg._config_dir)
    for srv in servers:
        if srv.get("name") == "homeassistant" or "ha-mcp" in str(srv.get("name", "")):
            return srv
    if servers:
        return servers[0]
    raise RuntimeError("No MCP server configured for Home Assistant")


class _HA:
    """Tiny read-only ha-mcp client."""

    def __init__(self) -> None:
        from openjarvis.mcp.client import MCPClient
        from openjarvis.mcp.transport import StreamableHTTPTransport

        srv = _load_server()
        self._client = MCPClient(
            StreamableHTTPTransport(url=srv["url"], token=srv.get("token"))
        )
        self._client.initialize()

    def read(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if name not in _READ_CALLS:
            raise PermissionError(f"{name} is not an allowed read call")
        if name == "ha_get_overview":
            raw = self._client.call_tool(name, arguments or {})
        else:
            raw = self._client.call_tool(
                "ha_call_read_tool", {"name": name, "arguments": arguments or {}}
            )
        text = raw["content"][0]["text"]
        return json.loads(text)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def _clean_log_message(msg: str) -> str:
    msg = _ANSI.sub("", msg)
    msg = _SECRET.sub(r"\1<redacted>", msg)
    return msg[:220]


def _parse_logs(text: str) -> dict[str, Any]:
    """Group ERROR/CRITICAL lines by component; keep a few recent examples."""
    counts: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    critical = errors = warnings = 0
    for line in _ANSI.sub("", text).splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        level = m.group("level")
        if level == "WARNING":
            warnings += 1
            continue
        if level not in ("ERROR", "CRITICAL"):
            continue
        if level == "CRITICAL":
            critical += 1
        else:
            errors += 1
        comp = m.group("logger")
        comp = comp.replace("homeassistant.components.", "").replace(
            "custom_components.", ""
        )
        comp = comp.split(".")[0] if comp else "unknown"
        counts[comp] = counts.get(comp, 0) + 1
        if len(recent) < 8:
            recent.append(
                {
                    "ts": m.group("ts"),
                    "level": level,
                    "component": comp,
                    "message": _clean_log_message(m.group("msg")),
                }
            )
    by_component = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    return {
        "critical": critical,
        "errors": errors,
        "warnings": warnings,
        "by_component": [{"component": c, "count": n} for c, n in by_component],
        "recent": recent,
        "lines_scanned": LOG_LINES,
    }


def _entity_stats(overview: dict[str, Any]) -> dict[str, Any]:
    domains = overview.get("domain_stats", {}) or {}
    total = int(overview.get("system_summary", {}).get("total_entities", 0) or 0)
    unavailable = 0
    worst: list[dict[str, Any]] = []
    for name, d in domains.items():
        un = int((d.get("states_summary") or {}).get("unavailable", 0) or 0)
        unavailable += un
        if un:
            worst.append(
                {"domain": name, "unavailable": un, "count": int(d.get("count", 0))}
            )
    worst.sort(key=lambda x: -x["unavailable"])
    updates = int(
        ((domains.get("update") or {}).get("states_summary") or {}).get("on", 0) or 0
    )
    autos = domains.get("automation") or {}
    return {
        "total": total,
        "unavailable": unavailable,
        "worst_domains": worst[:5],
        "updates_available": updates,
        "automations_total": int(autos.get("count", 0)),
        "automations_unavailable": int(
            (autos.get("states_summary") or {}).get("unavailable", 0) or 0
        ),
    }


def _repair_stats(overview: dict[str, Any]) -> dict[str, Any]:
    issues = overview.get("repairs") or []
    by_sev: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    automation_errors = backup_issues = 0
    upcoming: list[dict[str, str]] = []
    for i in issues:
        if i.get("ignored"):
            continue
        sev = str(i.get("severity", "unknown"))
        dom = str(i.get("domain", "unknown"))
        by_sev[sev] = by_sev.get(sev, 0) + 1
        by_domain[dom] = by_domain.get(dom, 0) + 1
        if dom == "automation":
            automation_errors += 1
        if dom == "backup":
            backup_issues += 1
        if i.get("breaks_in_ha_version"):
            upcoming.append(
                {
                    "domain": dom,
                    "issue": str(i.get("translation_key", "")),
                    "breaks_in": str(i["breaks_in_ha_version"]),
                }
            )
    top_domains = sorted(by_domain.items(), key=lambda kv: -kv[1])[:6]
    return {
        "total": sum(by_sev.values()),
        "errors": by_sev.get("error", 0) + by_sev.get("critical", 0),
        "warnings": by_sev.get("warning", 0),
        "by_domain": [{"domain": d, "count": n} for d, n in top_domains],
        "automation_errors": automation_errors,
        "backup_issues": backup_issues,
        "upcoming_breaking": upcoming,
    }


def _collect() -> dict[str, Any]:
    ha = _HA()
    errors: list[str] = []
    try:
        overview: dict[str, Any] = {}
        try:
            overview = ha.read("ha_get_overview")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"overview: {exc}")

        integrations: dict[str, Any] = {"total": 0, "failed": 0, "states": {}}
        try:
            it = ha.read("ha_get_integration", {"limit": 1})
            states = it.get("state_summary", {}) or {}
            integrations = {
                "total": int(it.get("total_count", 0)),
                "failed": sum(int(states.get(s, 0)) for s in FAILED_ENTRY_STATES),
                "states": states,
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"integrations: {exc}")

        motion: dict[str, Any] = {"total": 0, "offline": 0, "offline_entities": []}
        try:
            seen: dict[str, dict[str, Any]] = {}
            for word in MOTION_WORDS[:3]:
                r = ha.read(
                    "ha_search",
                    {"query": word, "domain_filter": "binary_sensor", "limit": 200},
                )
                for e in r.get("entities", []):
                    seen[e["entity_id"]] = e
            offline = [
                e for e in seen.values() if e.get("state") in ("unavailable", "unknown")
            ]
            motion = {
                "total": len(seen),
                "offline": len(offline),
                "offline_entities": [
                    {
                        "entity_id": e["entity_id"],
                        "name": e.get("friendly_name", ""),
                        "state": e["state"],
                    }
                    for e in offline[:10]
                ],
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"motion: {exc}")

        logs: dict[str, Any] = {}
        try:
            lg = ha.read(
                "ha_get_logs",
                {"source": "error_log", "limit": LOG_LINES, "compact": True},
            )
            logs = _parse_logs(str(lg.get("log", "")))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"logs: {exc}")

        return {
            "ok": not errors or bool(overview),
            "collected_at": time.time(),
            "errors": errors,
            "system": {
                "version": overview.get("system_summary", {}).get("version"),
                "name": overview.get("system_info", {}).get("location_name"),
                "state": overview.get("system_info", {}).get("state"),
            },
            "repairs": _repair_stats(overview) if overview else {},
            "entities": _entity_stats(overview) if overview else {},
            "integrations": integrations,
            "motion": motion,
            "logs": logs,
        }
    finally:
        ha.close()


def get_dashboard(force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _lock:
        fresh = _cache["data"] is not None and now - _cache["at"] < CACHE_TTL_S
        if fresh and not force:
            return {**_cache["data"], "cached": True}
        future = _pool.submit(_collect)
    try:
        data = future.result(timeout=COLLECT_TIMEOUT_S)
    except FutureTimeout:
        stale = _cache["data"]
        if stale:
            return {**stale, "cached": True, "stale": True}
        return {
            "ok": False,
            "errors": ["Home Assistant did not answer in time"],
            "collected_at": now,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("HA dashboard collection failed: %s", exc)
        stale = _cache["data"]
        if stale:
            return {**stale, "cached": True, "stale": True, "errors": [str(exc)]}
        return {"ok": False, "errors": [str(exc)], "collected_at": now}
    with _lock:
        _cache["at"] = time.time()
        _cache["data"] = data
    return {**data, "cached": False}


ha_router = APIRouter(prefix="/v1/ha", tags=["home-assistant"])


@ha_router.get("/dashboard")
def ha_dashboard(refresh: int = 0) -> dict[str, Any]:
    """Home Assistant health numbers for the GUI cards (read-only, cached)."""
    return get_dashboard(force=bool(refresh))


__all__ = ["ha_router", "get_dashboard"]
