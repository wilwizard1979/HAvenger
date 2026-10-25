"""Read-only Home Assistant collector for the L0 watchdog.

Every call goes through ``ha_dashboard_routes._HA``, which only sends the tools in
its ``_READ_CALLS`` allow-list. Sections that fail are left ``None`` and named in
``errors``, so the watchdog never resolves issues from missing data.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from openjarvis.agents.watchdog import is_ignored, parse_ts
from openjarvis.server.ha_dashboard_routes import (
    _HA,
    LOG_LINES,
    _clean_log_message,
)

logger = logging.getLogger(__name__)

UNAVAILABLE = ("unavailable", "unknown")
STATE_BATCH = 80
PAGE = 500
BOOT_PROBES = ("sun.sun", "zone.home")
BACKUP_SENSOR = "sensor.backup_last_successful_automatic_backup"
# Device classes proposed as "critical"; lock and alarm panels by domain.
CRITICAL_CLASSES = ("smoke", "gas", "carbon_monoxide", "moisture", "safety")
CRITICAL_DOMAINS = ("lock", "alarm_control_panel")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)? (?P<level>[A-Z]+) "
    r"\((?P<thread>[^)]*)\) \[(?P<logger>[^\]]*)\] (?P<msg>.*)$"
)


def parse_log_groups(text: str) -> dict[str, dict[str, Any]]:
    """ERROR/CRITICAL counts per component with one cleaned example line."""
    groups: dict[str, dict[str, Any]] = {}
    for line in _ANSI.sub("", text).splitlines():
        m = _LOG_LINE.match(line)
        level = {"FATAL": "CRITICAL", "WARN": "WARNING"}.get(
            m.group("level") if m else "", m.group("level") if m else ""
        )
        if not m or level not in ("WARNING", "ERROR", "CRITICAL"):
            continue
        comp = (
            m.group("logger")
            .replace("homeassistant.components.", "")
            .replace("custom_components.", "")
            .split(".")[0]
            or "unknown"
        )
        g = groups.setdefault(
            comp,
            {
                "critical": 0,
                "error": 0,
                "warning": 0,
                "example": "",
                "ts": m.group("ts"),
            },
        )
        g[{"CRITICAL": "critical", "ERROR": "error", "WARNING": "warning"}[level]] += 1
        g["example"] = _clean_log_message(m.group("msg"))
        g["ts"] = m.group("ts")
    return groups


class HaCollector:
    """One instance per service; a fresh MCP connection is opened per collect."""

    def __init__(self, ha_factory: Callable[[], Any] = _HA) -> None:
        self._factory = ha_factory

    # -- low level ------------------------------------------------------------
    @staticmethod
    def _states(ha: Any, ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """entity_id -> {state, attributes, last_changed}; None when HA lacks it."""
        out: dict[str, dict[str, Any] | None] = {i: None for i in ids}
        for n in range(0, len(ids), STATE_BATCH):
            chunk = ids[n : n + STATE_BATCH]
            r = ha.read(
                "ha_get_state",
                {"entity_id": chunk, "fields": ["state", "attributes", "last_changed"]},
            )
            d = r.get("data", r)
            states = d.get("states")
            if states is None and d.get("entity_id"):
                states = {d["entity_id"]: d}
            for eid, info in (states or {}).items():
                if eid in out and isinstance(info, dict):
                    out[eid] = info
        return out

    @staticmethod
    def _list_domain(ha: Any, domain: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        offset = 0
        while True:
            r = ha.read(
                "ha_search",
                {
                    "query": "",
                    "domain_filter": domain,
                    "limit": PAGE,
                    "offset": offset,
                },
            )
            found.extend(r.get("entities", []))
            nxt = r.get("entity_next_offset") or r.get("next_offset")
            if not r.get("entity_has_more") or not nxt or nxt <= offset:
                return found
            offset = int(nxt)

    @staticmethod
    def _integrations(ha: Any) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        offset = 0
        while True:
            r = ha.read("ha_get_integration", {"limit": 200, "offset": offset})
            entries.extend(r.get("entries", []))
            nxt = r.get("next_offset")
            if not r.get("has_more") or not nxt or nxt <= offset:
                return entries
            offset = int(nxt)

    # -- the snapshot -------------------------------------------------------------
    def collect(self, settings: dict[str, Any]) -> dict[str, Any]:
        ha = self._factory()
        errors: dict[str, str] = {}
        snap: dict[str, Any] = {
            "repairs": None,
            "domain_totals": None,
            "integrations": None,
            "unavailable": None,
            "critical": None,
            "motion": None,
            "batteries": None,
            "backup": None,
            "logs": None,
            "unavailable_since": None,
            "facts": {},
            "errors": errors,
        }

        def guarded(section: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                errors[section] = str(exc)[:200]
                return None

        try:
            overview = guarded("overview", lambda: ha.read("ha_get_overview")) or {}
            if overview:
                snap["repairs"] = overview.get("repairs") or []
                domains = overview.get("domain_stats") or {}
                snap["domain_totals"] = {
                    d: {
                        "count": int(v.get("count", 0)),
                        "unavailable": int(
                            (v.get("states_summary") or {}).get("unavailable", 0) or 0
                        ),
                        "on": int((v.get("states_summary") or {}).get("on", 0) or 0),
                    }
                    for d, v in domains.items()
                }

            snap["integrations"] = guarded(
                "integrations", lambda: self._integrations(ha)
            )

            if snap["domain_totals"] is not None:
                snap["unavailable"] = guarded(
                    "unavailable",
                    lambda: self._unavailable(ha, snap["domain_totals"], settings),
                )

            if snap["unavailable"] is not None:
                snap["unavailable_since"] = guarded(
                    "since", lambda: self._since(ha, snap, settings)
                )

            snap["critical"] = guarded(
                "critical",
                lambda: self._states(ha, list(settings["critical_entities"])),
            )
            self._motion_and_batteries(ha, settings, snap, guarded)

            snap["backup"] = guarded("backup", lambda: self._backup(ha))

            facts = snap["facts"]
            if snap["domain_totals"] is not None:
                facts["updates_pending"] = (
                    snap["domain_totals"].get("update", {}).get("on", 0)
                )
                if facts["updates_pending"]:
                    facts["updates_list"] = (
                        guarded("updates", lambda: self._updates(ha)) or []
                    )
            if snap["backup"] is not None:
                facts["backup_last_success"] = snap["backup"]["last_success"]
            if overview:
                facts["ha_version"] = (overview.get("system_summary") or {}).get(
                    "version"
                )

            def _logs() -> dict[str, dict[str, Any]]:
                lg = ha.read(
                    "ha_get_logs",
                    {"source": "error_log", "limit": LOG_LINES, "compact": True},
                )
                return parse_log_groups(str(lg.get("log", "")))

            snap["logs"] = guarded("logs", _logs)
            if snap["logs"] is not None:
                facts["log_critical_lines"] = sum(
                    g["critical"] for g in snap["logs"].values()
                )
                facts["log_levels"] = [
                    {
                        "component": k,
                        **{f: g[f] for f in ("critical", "error", "warning")},
                        "example": g["example"],
                    }
                    for k, g in sorted(
                        snap["logs"].items(),
                        key=lambda kv: (
                            -kv[1]["critical"],
                            -kv[1]["error"],
                            -kv[1]["warning"],
                        ),
                    )
                ][:15]
        finally:
            ha.close()
        return snap

    def _updates(self, ha: Any) -> list[dict[str, str]]:
        return [
            {"entity_id": e["entity_id"], "name": e.get("friendly_name", "")}
            for e in self._list_domain(ha, "update")
            if e.get("state") == "on"
        ][:10]

    def _since(
        self, ha: Any, snap: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """When each unavailable entity last changed (a lower bound: HA resets
        ``last_changed`` at every start). The oldest value over all of them and two
        always-present entities also tells when Home Assistant last started."""
        names = {
            e["entity_id"]: e.get("name", "")
            for ents in snap["unavailable"].values()
            for e in ents
            if not is_ignored(e["entity_id"], settings["ignore_patterns"])
        }
        states = self._states(ha, [*names, *BOOT_PROBES])
        out = {
            eid: {
                "name": names[eid],
                "since": parse_ts((states.get(eid) or {}).get("last_changed")),
            }
            for eid in names
        }
        stamps = [
            t
            for t in (parse_ts((s or {}).get("last_changed")) for s in states.values())
            if t is not None
        ]
        if stamps:
            snap["facts"]["boot_estimate"] = min(stamps)
        return out

    def _backup(self, ha: Any) -> dict[str, Any]:
        info = self._states(ha, [BACKUP_SENSOR])[BACKUP_SENSOR] or {}
        state = info.get("state")
        return {"last_success": None if state in (None, *UNAVAILABLE) else state}

    def _unavailable(
        self, ha: Any, totals: dict[str, dict[str, int]], settings: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        skip = {p.lower() for p in settings["ignore_patterns"]}
        result: dict[str, list[dict[str, Any]]] = {}
        for domain, t in sorted(totals.items()):
            if not t["unavailable"] or f"{domain}.*" in skip:
                continue
            ents = self._list_domain(ha, domain)
            bad = [e for e in ents if e.get("state") in UNAVAILABLE]
            if bad:
                result[domain] = [
                    {"entity_id": e["entity_id"], "name": e.get("friendly_name", "")}
                    for e in bad
                ]
        return result

    def _motion_and_batteries(
        self,
        ha: Any,
        settings: dict[str, Any],
        snap: dict[str, Any],
        guarded: Callable[[str, Callable[[], Any]], Any],
    ) -> None:
        wanted = list(settings["motion_sensors"])

        def binary() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            ids = [e["entity_id"] for e in self._list_domain(ha, "binary_sensor")]
            states = self._states(ha, wanted or ids)
            motion = {
                eid: s
                for eid, s in states.items()
                if s and (wanted or s["attributes"].get("device_class") == "motion")
            }
            low = [
                {"entity_id": eid, "level": None, "low": True}
                for eid, s in states.items()
                if s
                and s["attributes"].get("device_class") == "battery"
                and s.get("state") == "on"
            ]
            return motion, low

        res = guarded("motion", binary)
        low_binary: list[dict[str, Any]] = []
        if res is not None:
            snap["motion"], low_binary = res

        def sensors() -> list[dict[str, Any]]:
            r = ha.read(
                "ha_search",
                {"query": "battery", "domain_filter": "sensor", "limit": 200},
            )
            ids = [e["entity_id"] for e in r.get("entities", [])]
            out = []
            for eid, s in self._states(ha, ids).items():
                if not s or s["attributes"].get("device_class") != "battery":
                    continue
                try:
                    level = float(s["state"])
                except (TypeError, ValueError):
                    continue  # unavailable is the entity group's job
                out.append({"entity_id": eid, "level": level, "low": False})
            return out

        lows = guarded("batteries", sensors)
        if lows is not None and res is not None:
            snap["batteries"] = lows + low_binary

    # -- proposals for the user to confirm -------------------------------------------
    def suggest(self) -> dict[str, Any]:
        """Critical-entity candidates by device class / domain, plus the motion list."""
        ha = self._factory()
        try:
            names: dict[str, str] = {}
            binary_ids = []
            for e in self._list_domain(ha, "binary_sensor"):
                names[e["entity_id"]] = e.get("friendly_name", "")
                binary_ids.append(e["entity_id"])
            states = self._states(ha, binary_ids)
            critical: list[dict[str, Any]] = []
            motion: list[dict[str, Any]] = []
            for eid, s in states.items():
                if not s:
                    continue
                cls = s["attributes"].get("device_class")
                row = {
                    "entity_id": eid,
                    "name": names.get(eid, ""),
                    "kind": cls,
                    "state": s.get("state"),
                }
                if cls in CRITICAL_CLASSES:
                    critical.append(row)
                elif cls == "motion":
                    motion.append(row)
            for domain in CRITICAL_DOMAINS:
                for e in self._list_domain(ha, domain):
                    critical.append(
                        {
                            "entity_id": e["entity_id"],
                            "name": e.get("friendly_name", ""),
                            "kind": domain,
                            "state": e.get("state"),
                        }
                    )
        finally:
            ha.close()
        counts: dict[str, int] = {}
        for r in critical:
            counts[str(r["kind"])] = counts.get(str(r["kind"]), 0) + 1
        return {
            "critical": critical,
            "critical_by_kind": counts,
            "motion": motion,
            "note": "Nothing is saved until you confirm; then PUT critical_entities.",
        }
