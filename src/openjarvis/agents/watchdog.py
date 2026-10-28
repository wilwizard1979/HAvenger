"""L0 watchdog for Jarvis the Janitor: deterministic, read-only, no cloud calls.

One *tick* does this:

1. ``collector.collect`` reads Home Assistant (read-only) into a plain snapshot
2. ``observe`` turns the snapshot into fingerprinted observations (pure function)
3. ``run_tick`` applies the issue lifecycle against the store:

   * an observation must persist ``persist_min`` minutes before it becomes an
     issue (the clock starts at the HA ``last_changed`` time when HA gives one)
   * a missing issue is resolved only after ``RESOLVE_AFTER_MISSES`` clean ticks,
     and never for a category whose data could not be collected this tick
   * acknowledged and ignored issues keep their state; resolved ones reopen

Quiet hours always win: events raised inside them are flagged ``suppressed`` so a
notifier (Phase 5) never pings during them, critical ones included.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from openjarvis.agents.store import Store, get_store

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
RESOLVE_AFTER_MISSES = 2
UNAVAILABLE = ("unavailable", "unknown")
FAILED_ENTRY_STATES = ("setup_error", "setup_retry", "not_loaded", "migration_error")
FIRST_TICK_DELAY_S = 45

# snapshot section -> issue category it feeds
SECTION_CATEGORY = {
    "repairs": "repair",
    "integrations": "integration",
    "unavailable": "entity_group",
    "critical": "critical_entity",
    "motion": "motion",
    "batteries": "battery",
    "backup": "backup",
    "logs": "log",
}


@dataclass(frozen=True)
class Observation:
    category: str
    source_id: str
    title: str
    severity: str = "warning"
    meta: dict[str, Any] = field(default_factory=dict, compare=False)
    persist_min: float = 0.0
    since: float | None = None  # epoch when HA says the condition began

    @property
    def fingerprint(self) -> str:
        raw = f"{self.category}|{self.source_id}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324


@dataclass
class TickResult:
    ok: bool
    started: float
    duration_s: float = 0.0
    observed: int = 0
    pending: int = 0
    new: list[dict[str, Any]] = field(default_factory=list)
    escalated: list[dict[str, Any]] = field(default_factory=list)
    reopened: list[dict[str, Any]] = field(default_factory=list)
    resolved: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    quiet_hours: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started": self.started,
            "duration_s": round(self.duration_s, 2),
            "observed": self.observed,
            "pending": self.pending,
            "new": self.new,
            "escalated": self.escalated,
            "reopened": self.reopened,
            "resolved": self.resolved,
            "errors": self.errors,
            "quiet_hours": self.quiet_hours,
        }


class Collector(Protocol):
    def collect(self, settings: dict[str, Any]) -> dict[str, Any]: ...


# -- helpers ---------------------------------------------------------------
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_quiet_hours(now: float, settings: dict[str, Any]) -> bool:
    qh = settings["watchdog"]["quiet_hours"]
    if not qh["enabled"]:
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(settings["schedule"]["timezone"]))
    cur = local.hour * 60 + local.minute
    start, end = _minutes(qh["start"]), _minutes(qh["end"])
    if start == end:
        return False
    return start <= cur < end if start < end else cur >= start or cur < end


def parse_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_ignored(entity_id: str, patterns: list[str]) -> bool:
    eid = entity_id.lower()
    return any(fnmatch.fnmatch(eid, p.lower()) for p in patterns)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -- observe (pure) ---------------------------------------------------------
def observe(
    snap: dict[str, Any], settings: dict[str, Any], now: float
) -> tuple[list[Observation], set[str]]:
    """Snapshot -> observations, plus the categories that were collected."""
    th = settings["thresholds"]
    persist = float(th["persist_min"])
    ignore = settings["ignore_patterns"]
    out: list[Observation] = []
    ok = {cat for sec, cat in SECTION_CATEGORY.items() if snap.get(sec) is not None}
    if snap.get("domain_totals") is None:
        ok.discard("entity_group")  # percentages need the overview

    # Home Assistant repairs: persistent by nature, no debounce.
    for i in snap.get("repairs") or []:
        if i.get("ignored"):
            continue
        domain = str(i.get("domain", "unknown"))
        key = str(i.get("issue_id") or i.get("translation_key") or "unknown")
        ha_sev = str(i.get("severity", "unknown"))
        sev = {"critical": "critical", "error": "warning", "warning": "warning"}.get(
            ha_sev, "info"
        )
        out.append(
            Observation(
                "repair",
                f"{domain}/{key}",
                f"Repair: {domain} / {i.get('translation_key') or key}",
                sev,
                {
                    "domain": domain,
                    "ha_severity": ha_sev,
                    "breaks_in": i.get("breaks_in_ha_version"),
                },
            )
        )

    for e in snap.get("integrations") or []:
        if e.get("state") in FAILED_ENTRY_STATES and not e.get("disabled_by"):
            out.append(
                Observation(
                    "integration",
                    str(e.get("entry_id")),
                    f"Integration {e.get('domain')} ({e.get('title', '')}): "
                    f"{e.get('state')}",
                    "warning",
                    {"domain": e.get("domain"), "state": e.get("state")},
                    persist,
                )
            )

    critical = {k for k in (snap.get("critical") or {})}
    motion = snap.get("motion") or {}
    owned = critical | set(motion)

    totals = snap.get("domain_totals") or {}
    for domain, ents in sorted((snap.get("unavailable") or {}).items()):
        ids = [
            e["entity_id"]
            for e in ents
            if e["entity_id"] not in owned and not is_ignored(e["entity_id"], ignore)
        ]
        total = int((totals.get(domain) or {}).get("count", 0))
        pct = len(ids) / total * 100 if total else 0.0
        if not ids or pct < th["entities_amber_pct"]:
            continue
        sev = "critical" if pct >= th["entities_red_pct"] else "warning"
        out.append(
            Observation(
                "entity_group",
                domain,
                f"{domain}: {len(ids)} of {total} entities unavailable ({pct:.0f}%)",
                sev,
                {
                    "domain": domain,
                    "count": len(ids),
                    "total": total,
                    "examples": ids[:8],
                    "entities": ids[:50],
                },
                persist,
            )
        )

    for eid, info in sorted((snap.get("critical") or {}).items()):
        if info is None:
            out.append(
                Observation(
                    "critical_entity",
                    eid,
                    f"Critical entity {eid} not found in Home Assistant",
                    "critical",
                    {"entity_id": eid, "state": "missing"},
                    persist,
                )
            )
        elif info.get("state") in UNAVAILABLE:
            out.append(
                Observation(
                    "critical_entity",
                    eid,
                    f"Critical entity {eid} is {info['state']}",
                    "critical",
                    {"entity_id": eid, "state": info["state"]},
                    persist,
                    parse_ts(info.get("last_changed")),
                )
            )

    for eid, info in sorted(motion.items()):
        if info.get("state") in UNAVAILABLE and not is_ignored(eid, ignore):
            out.append(
                Observation(
                    "motion",
                    eid,
                    f"Motion sensor {eid} is {info['state']}",
                    "warning",
                    {"entity_id": eid, "state": info["state"]},
                    persist,
                    parse_ts(info.get("last_changed")),
                )
            )

    for b in snap.get("batteries") or []:
        eid = b["entity_id"]
        if is_ignored(eid, ignore):
            continue
        level = b.get("level")
        if b.get("low") or (level is not None and level <= th["battery_low_pct"]):
            shown = f"{level:g}%" if level is not None else "low"
            out.append(
                Observation(
                    "battery",
                    eid,
                    f"Battery {shown}: {eid}",
                    "warning",
                    {"entity_id": eid, "level": level},
                    persist,
                )
            )

    backup = snap.get("backup")
    if backup is not None:
        last = parse_ts(backup.get("last_success"))
        limit_h = float(th["backup_age_hours"])
        if last is None:
            out.append(
                Observation(
                    "backup",
                    "age",
                    "Last successful backup is unknown",
                    "warning",
                    {},
                    persist,
                )
            )
        else:
            age_h = (now - last) / 3600
            if age_h > limit_h:
                out.append(
                    Observation(
                        "backup",
                        "age",
                        f"Last successful backup is {age_h:.0f} h old "
                        f"(limit {limit_h:g} h)",
                        "critical" if age_h > 2 * limit_h else "warning",
                        {"age_hours": round(age_h, 1)},
                        persist,
                    )
                )

    for comp, g in sorted((snap.get("logs") or {}).items()):
        if g.get("critical"):
            out.append(
                Observation(
                    "log",
                    f"{comp}:critical",
                    f"Log: {g['critical']} CRITICAL from {comp}",
                    "critical",
                    {"component": comp, "example": g.get("example", "")},
                )
            )
        elif g.get("error", 0) >= th["log_error_min"]:
            out.append(
                Observation(
                    "log",
                    f"{comp}:errors",
                    f"Log: {g['error']} errors from {comp}",
                    "warning",
                    {"component": comp, "example": g.get("example", "")},
                )
            )
    return out, ok


# -- one tick ----------------------------------------------------------------
def _event(o: Observation) -> dict[str, Any]:
    return {
        "fingerprint": o.fingerprint,
        "category": o.category,
        "severity": o.severity,
        "title": o.title,
    }


def apply_observations(
    store: Store,
    observations: list[Observation],
    ok_categories: set[str],
    now: float,
    result: TickResult,
    resolve_after: int = RESOLVE_AFTER_MISSES,
) -> None:
    seen: set[str] = set()
    for o in observations:
        fp = o.fingerprint
        seen.add(fp)
        first = store.watch_seen(fp, o.category, now)
        began = min(first, o.since) if o.since else first
        if now - began < o.persist_min * 60:
            result.pending += 1
            continue
        before = store.issue(fp)
        after = store.upsert_issue(
            fp,
            category=o.category,
            source_id=o.source_id,
            title=o.title,
            severity=o.severity,
            meta=o.meta,
            now=now,
        )
        if before is None:
            result.new.append(_event(o))
        elif before["state"] == "resolved":
            result.reopened.append(_event(o))
        elif SEVERITY_RANK[o.severity] > SEVERITY_RANK.get(before["severity"], 0):
            result.escalated.append(_event(o))
        del after

    for fp, ws in store.watch_states().items():
        if fp in seen or ws["category"] not in ok_categories:
            continue
        issue = store.issue(fp)
        if issue is None or issue["state"] in ("resolved", "ignored"):
            store.watch_forget(fp)
            continue
        if store.watch_miss(fp) >= resolve_after:
            store.set_issue_state(fp, "resolved")
            store.watch_forget(fp)
            result.resolved.append(
                {
                    "fingerprint": fp,
                    "category": issue["category"],
                    "severity": issue["severity"],
                    "title": issue["title"],
                }
            )


def janitor_dir() -> Path:
    return Path(
        os.environ.get("JARVIS_JANITOR_DIR", "~/.openjarvis/janitor")
    ).expanduser()


def export_unavailable_md(store: Store, now: float) -> Path:
    """Rewrite the local list of entities unavailable longer than the threshold.

    The database is the source of truth; this file is an export (rewritten every
    check) that people and Claude can read and annotate next to it.
    """
    days = int(store.settings()["thresholds"].get("stale_unavailable_days", 10))
    data = store.unavailable_longer_than(days, now)
    lines = [
        f"# Entities unavailable for more than {days} days",
        "",
        f"Updated {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')} · "
        f"{data['count']} of {data['tracked']} unavailable entities · generated by the "
        "Janitor watchdog (Python), rewritten at every check.",
        "An entity that becomes available again disappears from this list by itself. "
        "Home Assistant resets its change time at every restart, so counting starts "
        + (
            datetime.fromtimestamp(data["counting_since"]).strftime("%Y-%m-%d")
            if data["counting_since"]
            else "with the first check"
        )
        + ".",
        "",
        "| Entity | Name | Unavailable since | Days |",
        "|---|---|---|---|",
    ]
    for r in data["items"]:
        since = datetime.fromtimestamp(r["since"]).strftime("%Y-%m-%d")
        name = (r["name"] or "").replace("|", "/")
        age = int((now - r["since"]) // 86400)
        lines.append(f"| {r['entity_id']} | {name} | {since} | {age} |")
    path = janitor_dir() / "unavailable-long.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:  # a read-only disk must not break the check
        logger.warning("Could not write %s: %s", path, exc)
    return path


def track_long_unavailable(store: Store, snap: dict[str, Any], now: float) -> None:
    """Keep ``since`` per unavailable entity and note Home Assistant restarts."""
    since = snap.get("unavailable_since")
    if since is not None:
        store.track_unavailable(since, now)
    if since is not None:
        export_unavailable_md(store, now)
    boot = (snap.get("facts") or {}).get("boot_estimate")
    if boot:
        last = store.runs(1)
        outage = (
            bool(last) and last[0]["layer"] == "L0" and last[0]["outcome"] == "error"
        )
        store.record_boot(boot, now, outage)


def run_tick(
    store: Store,
    collector: Collector,
    *,
    now: float | None = None,
    manual: bool = False,
) -> TickResult:
    """One check. ``manual`` (the user pressed Run check): an issue that is gone is
    resolved on the first clean read; scheduled checks wait for two."""
    now = now if now is not None else time.time()
    t0 = time.monotonic()
    settings = store.settings()
    result = TickResult(ok=False, started=now)
    try:
        snap = collector.collect(settings)
        observations, ok_cats = observe(snap, settings, now)
        result.errors = dict(snap.get("errors") or {})
        result.observed = len(observations)
        result.quiet_hours = is_quiet_hours(now, settings)
        apply_observations(
            store,
            observations,
            ok_cats,
            now,
            result,
            1 if manual else RESOLVE_AFTER_MISSES,
        )
        track_long_unavailable(store, snap, now)
        if snap.get("facts"):
            store.set_facts({**snap["facts"], "checked_at": now})
        result.ok = bool(ok_cats)
    except Exception as exc:  # noqa: BLE001 - a tick must never take the server down
        logger.warning("Janitor watchdog tick failed: %s", exc)
        result.errors["tick"] = str(exc)[:200]
    result.duration_s = time.monotonic() - t0
    outcome = "ok" if result.ok and not result.errors else "partial"
    if not result.ok:
        outcome = "error"
    store.record_run(
        "L0",
        None,
        started=now,
        outcome=outcome,
        purpose=json.dumps(
            {
                "observed": result.observed,
                "new": len(result.new),
                "resolved": len(result.resolved),
                "errors": sorted(result.errors),
            }
        ),
        dry_run=0,
        cost_usd=0.0,
    )
    return result


def log_notifier(result: TickResult) -> None:
    """Phase 2 notifier: terminal log only. Quiet hours are respected."""
    for kind, events in (
        ("NEW", result.new),
        ("ESCALATED", result.escalated),
        ("REOPENED", result.reopened),
        ("RESOLVED", result.resolved),
    ):
        for e in events:
            tag = " (quiet hours)" if result.quiet_hours else ""
            logger.warning("Janitor %s [%s] %s%s", kind, e["severity"], e["title"], tag)


# -- service (scheduler) -------------------------------------------------------
class WatchdogService:
    def __init__(
        self,
        store: Store | None = None,
        collector: Collector | None = None,
        notifier: Callable[[TickResult], None] = log_notifier,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._collector = collector
        self._notifier = notifier
        self._clock = clock
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self.last: dict[str, Any] | None = None
        self.next_run: float | None = None
        self.running = False

    @property
    def store(self) -> Store:
        return self._store or get_store()

    @property
    def collector(self) -> Collector:
        if self._collector is None:
            from openjarvis.agents.ha_collector import HaCollector

            self._collector = HaCollector()
        return self._collector

    def tick(self, *, wait: bool = True, manual: bool = False) -> dict[str, Any]:
        """Run one tick (blocking). ``wait=False`` returns the last result if busy."""
        if not self._lock.acquire(blocking=wait):
            return {**(self.last or {}), "busy": True}
        try:
            self.running = True
            result = run_tick(
                self.store, self.collector, now=self._clock(), manual=manual
            )
            try:
                self._notifier(result)
            except Exception:  # noqa: BLE001
                logger.debug("watchdog notifier failed", exc_info=True)
            self.last = result.summary()
            return self.last
        finally:
            self.running = False
            self._lock.release()

    def _maybe_report(self) -> None:
        """Digest (and Haiku triage when allowed) once per scheduled slot."""
        from openjarvis.agents.reporting import generate_report, report_due

        now = self._clock()
        if not report_due(self.store, now):
            return
        try:
            generate_report(self.store, now=now)
            from openjarvis.agents.readout import run_readout

            run_readout(self.store, now=now)  # local Qwen, free; never raises
        except Exception:  # noqa: BLE001
            logger.warning("Janitor report failed", exc_info=True)

    async def _loop(self, first_delay: float) -> None:
        delay = first_delay
        while True:
            self.next_run = self._clock() + delay
            await asyncio.sleep(delay)
            wd = self.store.settings()["watchdog"]
            if wd["enabled"]:
                try:
                    run = asyncio.get_running_loop().run_in_executor
                    await run(None, self.tick)
                    await run(None, self._maybe_report)
                except Exception:  # noqa: BLE001
                    logger.warning("watchdog loop error", exc_info=True)
            delay = max(60.0, float(wd["interval_min"]) * 60) if wd["enabled"] else 60.0

    def start(self, first_delay: float = FIRST_TICK_DELAY_S) -> bool:
        if os.environ.get("JARVIS_WATCHDOG", "1").strip().lower() in (
            "0",
            "false",
            "off",
        ):
            return False
        if self._task and not self._task.done():
            return True
        self._task = asyncio.get_running_loop().create_task(self._loop(first_delay))
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.next_run = None

    def status(self) -> dict[str, Any]:
        issues = self.store.issues()
        active = [i for i in issues if i["state"] in ("open", "acknowledged")]
        by_sev: dict[str, int] = {}
        for i in active:
            by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        s = self.store.settings()
        return {
            "enabled": s["watchdog"]["enabled"],
            "autostart": self._task is not None and not self._task.done(),
            "interval_min": s["watchdog"]["interval_min"],
            "running": self.running,
            "next_run": self.next_run,
            "quiet_hours_now": is_quiet_hours(self._clock(), s),
            "last_tick": self.last,
            "issues": {
                "active": len(active),
                "by_severity": by_sev,
                "ignored": sum(1 for i in issues if i["state"] == "ignored"),
            },
        }


_service: WatchdogService | None = None


def get_watchdog() -> WatchdogService:
    global _service
    if _service is None:
        _service = WatchdogService()
    return _service


def reset_watchdog_for_tests() -> None:
    global _service
    _service = None
