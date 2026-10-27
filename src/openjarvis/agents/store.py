"""SQLite store for Jarvis the Janitor (``~/.openjarvis/ha_agents.db``).

Tables: issues, runs, reports, proposals, ledger, settings. Everything here is
local; nothing in this file talks to the network.
"""

# ruff: noqa: E501  -- long SQL strings
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DB = (
    Path(os.environ.get("OPENJARVIS_HOME", Path.home() / ".openjarvis"))
    / "ha_agents.db"
)

# Verified 2026-10-25: platform.claude.com/docs/en/about-claude/pricing
# USD per million tokens. ``long`` applies when the prompt exceeds ``long_over`` tokens.
DEFAULT_PRICES: dict[str, dict[str, Any]] = {
    "claude-haiku-5-5": {
        "input": 0.10,
        "output": 0.50,
        "cache_write": 0.125,
        "cache_read": 0.01,
        "long_over": 100_000,
        "long": {
            "input": 0.50,
            "output": 2.50,
            "cache_write": 0.625,
            "cache_read": 0.05,
        },
    },
    "claude-sonnet-5-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_write": 2.5,
        "cache_read": 0.10,
    },
    "claude-opus-5-5": {
        "input": 4.0,
        "output": 20.0,
        "cache_write": 5.0,
        "cache_read": 0.20,
    },
}

DEFAULT_SETTINGS: dict[str, Any] = {
    # Pilot: daily. Target: {"frequency": "weekly", "weekday": "fri"}.
    "schedule": {
        "frequency": "daily",
        "weekday": "fri",
        "time": "07:30",
        "timezone": "Europe/Berlin",
    },
    "budget": {
        "monthly_limit_usd": 20.0,
        "pause_auto_escalation_pct": 75,
        "hard_stop_pct": 100,
    },
    "models": {
        "triage": "claude-haiku-5-5",
        "diagnose": "claude-sonnet-5-5",
        "deep": "claude-opus-5-5",
    },
    "prices": DEFAULT_PRICES,
    "prices_verified": {
        "date": "2026-10-25",
        "source": "platform.claude.com/docs/en/about-claude/pricing",
    },
    "dry_run": True,
    "auto_escalation": False,  # Sonnet / Opus only when the user escalates
    "thresholds": {
        "entities_amber_pct": 2,
        "entities_red_pct": 10,
        "backup_age_hours": 36,
        "battery_low_pct": 20,
        # Motion sensor batteries: orange below this, red when unknown or offline.
        "battery_warn_pct": 50,
        "persist_min": 10,
        "log_error_min": 3,
        "automation_fail_runs_24h": 3,
        "breaking_change_days": 30,
        "stale_unavailable_days": 10,
    },
    # L0 watchdog: read-only polling of Home Assistant, no cloud calls.
    "watchdog": {
        "enabled": True,
        "interval_min": 15,
        # Quiet hours always win, critical alerts included.
        "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"},
    },
    # Severity x effort matrix. "auto" = Haiku rates it (until then default_effort).
    "matrix": {
        "effort_defaults": {
            "repair": "auto",
            "integration": "auto",
            "log": "auto",
            "entity_group": "hard",
            "motion": "medium",
            "critical_entity": "medium",
            "battery": "quick",
            "backup": "medium",
        },
        # Needs no change in Home Assistant (e.g. replace a battery): a chore.
        "chore_categories": ["battery"],
        "default_effort": "medium",
    },
    # One escalation (Sonnet or Opus) may look around read-only, within these limits.
    "escalation": {"max_rounds": 6, "max_usd": 0.5, "opus_max_usd": 2.0},
    # Qwen (local, free): plain-English house status. Both switches are yours.
    "qwen": {"house_status": True, "read_aloud": True},
    # Janitor Telegram bot (own token in the Keychain). Locked to one Telegram user.
    "telegram": {
        "enabled": True,
        "allowed_user_id": 8259290562,
        "digest": True,
        # Pilot: alerts stay on the dashboard; switch on once the bot is trusted.
        "alerts": False,
    },
    # Executor: runs only what you approved in Telegram. live=false = simulation.
    "executor": {
        "live": False,
        "operator": "haiku",
        "require_backup": True,
        "allowed_tools": ["ha_call_write_tool"],
        "deny_names_regex": "delete|remove|shutdown|restart|reboot|eval_template|reset",
        "step_timeout_s": 660,
    },
    "critical_entities": [],
    # Empty = automatic: every binary_sensor with device_class "motion".
    "motion_sensors": [],
    "ignore_patterns": ["device_tracker.*", "button.*", "*alexa*", "*echo*"],
    "extra_redaction_terms": [],
    "features": {"qwen_presort": False},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
  fingerprint TEXT PRIMARY KEY, category TEXT NOT NULL, source_id TEXT, title TEXT,
  severity TEXT NOT NULL DEFAULT 'warning', state TEXT NOT NULL DEFAULT 'open',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1, meta TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT NOT NULL, model TEXT, started REAL NOT NULL,
  finished REAL, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, outcome TEXT, purpose TEXT, dry_run INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, status TEXT NOT NULL,
  summary_text TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT, model TEXT, diagnosis TEXT,
  risk TEXT, change TEXT, rollback TEXT, verification TEXT,
  status TEXT NOT NULL DEFAULT 'pending', telegram_message_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  month TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, calls INTEGER DEFAULT 0,
  PRIMARY KEY (month, model)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_overrides (
  cluster_id TEXT PRIMARY KEY, effort TEXT, kind TEXT, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_notes (
  cluster_id TEXT PRIMARY KEY, members_hash TEXT NOT NULL, effort TEXT, note TEXT, cause TEXT,
  model TEXT, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_selection (
  cluster_id TEXT PRIMARY KEY, selected INTEGER NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, label TEXT NOT NULL,
  opened_at REAL NOT NULL, opened_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  seen_before TEXT, cleared_at REAL, closed_at REAL,
  solution TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
  record TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS tasks_cluster ON tasks(cluster_id, status);
CREATE TABLE IF NOT EXISTS findings_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id TEXT NOT NULL, text TEXT NOT NULL,
  replaced_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_findings (
  cluster_id TEXT PRIMARY KEY, text TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_proposals (
  cluster_id TEXT PRIMARY KEY, members_hash TEXT NOT NULL, body TEXT NOT NULL, model TEXT,
  cost_usd REAL DEFAULT 0, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id TEXT NOT NULL, label TEXT, members_hash TEXT,
  instruction TEXT NOT NULL, instruction_hash TEXT NOT NULL, risk TEXT, summary TEXT,
  status TEXT NOT NULL, live INTEGER NOT NULL DEFAULT 0, telegram_message_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, approved_at REAL, started_at REAL,
  finished_at REAL, log TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS readouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, report_id INTEGER, model TEXT,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, seconds REAL DEFAULT 0,
  text TEXT NOT NULL, guard_ok INTEGER NOT NULL DEFAULT 1, note TEXT
);
CREATE TABLE IF NOT EXISTS unavail_track (
  entity_id TEXT PRIMARY KEY, name TEXT, since REAL NOT NULL, last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ha_boots (
  boot_at REAL PRIMARY KEY, detected REAL NOT NULL, after_outage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS watch_state (
  fingerprint TEXT PRIMARY KEY, category TEXT NOT NULL, first_seen REAL NOT NULL, misses INTEGER NOT NULL DEFAULT 0
);
"""

ISSUE_STATES = ("open", "acknowledged", "ignored", "resolved")
PROPOSAL_STATES = (
    "pending",
    "approved",
    "rejected",
    "applied",
    "failed",
    "rolled_back",
)
RISKS = ("low", "medium", "high")
EFFORTS = ("quick", "medium", "hard")
CLUSTER_KINDS = ("change", "chore")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SettingsError(ValueError):
    """A settings change was rejected."""


def month_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


def _num(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{name} must be a number")
    if not lo <= value <= hi:
        raise SettingsError(f"{name} must be between {lo} and {hi}")
    return float(value)


def _validate_group(key: str, value: Any) -> Any:
    if key == "schedule":
        if not isinstance(value, dict):
            raise SettingsError("schedule must be an object")
        if value.get("frequency") not in ("daily", "weekly"):
            raise SettingsError("schedule.frequency must be 'daily' or 'weekly'")
        if str(value.get("weekday", "")).lower() not in WEEKDAYS:
            raise SettingsError("schedule.weekday must be mon..sun")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value.get("time", ""))):
            raise SettingsError("schedule.time must be HH:MM (24 h)")
        try:
            ZoneInfo(str(value.get("timezone", "")))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SettingsError(
                "schedule.timezone is not a valid IANA timezone"
            ) from exc
        return {**value, "weekday": str(value["weekday"]).lower()}
    if key == "budget":
        if not isinstance(value, dict):
            raise SettingsError("budget must be an object")
        limit = _num(
            value.get("monthly_limit_usd"), "budget.monthly_limit_usd", 0, 100_000
        )
        pause = _num(
            value.get("pause_auto_escalation_pct"),
            "budget.pause_auto_escalation_pct",
            1,
            100,
        )
        stop = _num(value.get("hard_stop_pct"), "budget.hard_stop_pct", 1, 100)
        if pause > stop:
            raise SettingsError("pause_auto_escalation_pct cannot exceed hard_stop_pct")
        return {
            "monthly_limit_usd": limit,
            "pause_auto_escalation_pct": pause,
            "hard_stop_pct": stop,
        }
    if key == "models":
        if not isinstance(value, dict) or set(value) != {"triage", "diagnose", "deep"}:
            raise SettingsError("models needs exactly: triage, diagnose, deep")
        if not all(isinstance(v, str) and v.strip() for v in value.values()):
            raise SettingsError("model names must be non-empty strings")
        return value
    if key == "prices":
        if not isinstance(value, dict):
            raise SettingsError("prices must be an object keyed by model")
        for model, row in value.items():
            if not isinstance(row, dict) or not all(
                isinstance(row.get(f), (int, float))
                for f in ("input", "output", "cache_write", "cache_read")
            ):
                raise SettingsError(
                    f"prices[{model}] needs numeric input/output/cache_write/cache_read"
                )
        return value
    if key in ("dry_run", "auto_escalation"):
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false")
        return value
    if key == "thresholds":
        if not isinstance(value, dict) or set(value) != set(
            DEFAULT_SETTINGS["thresholds"]
        ):
            raise SettingsError("thresholds has missing or unknown fields")
        return {k: _num(v, f"thresholds.{k}", 0, 100_000) for k, v in value.items()}
    if key in (
        "critical_entities",
        "motion_sensors",
        "ignore_patterns",
        "extra_redaction_terms",
    ):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{key} must be a list of strings")
        if key == "extra_redaction_terms":
            from openjarvis.agents.redaction import split_terms

            return split_terms(value)
        return [v.strip() for v in value if v.strip()]
    if key == "watchdog":
        if not isinstance(value, dict) or set(value) != {
            "enabled",
            "interval_min",
            "quiet_hours",
        }:
            raise SettingsError("watchdog needs: enabled, interval_min, quiet_hours")
        if not isinstance(value["enabled"], bool):
            raise SettingsError("watchdog.enabled must be true or false")
        interval = _num(value["interval_min"], "watchdog.interval_min", 1, 1440)
        qh = value["quiet_hours"]
        if not isinstance(qh, dict) or set(qh) != {"enabled", "start", "end"}:
            raise SettingsError("watchdog.quiet_hours needs: enabled, start, end")
        if not isinstance(qh["enabled"], bool):
            raise SettingsError("watchdog.quiet_hours.enabled must be true or false")
        for f in ("start", "end"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(qh[f])):
                raise SettingsError(f"watchdog.quiet_hours.{f} must be HH:MM (24 h)")
        return {
            "enabled": value["enabled"],
            "interval_min": interval,
            "quiet_hours": dict(qh),
        }
    if key == "matrix":
        keys = {"effort_defaults", "chore_categories", "default_effort"}
        if not isinstance(value, dict) or set(value) != keys:
            raise SettingsError(f"matrix needs: {', '.join(sorted(keys))}")
        eff = value["effort_defaults"]
        if not isinstance(eff, dict) or not all(
            isinstance(k, str) and v in (*EFFORTS, "auto") for k, v in eff.items()
        ):
            raise SettingsError(
                "matrix.effort_defaults: category -> quick|medium|hard|auto"
            )
        if value["default_effort"] not in EFFORTS:
            raise SettingsError("matrix.default_effort must be quick, medium or hard")
        chores = value["chore_categories"]
        if not isinstance(chores, list) or not all(isinstance(c, str) for c in chores):
            raise SettingsError("matrix.chore_categories must be a list of categories")
        return {
            "effort_defaults": dict(eff),
            "chore_categories": list(chores),
            "default_effort": value["default_effort"],
        }
    if key == "qwen":
        if not isinstance(value, dict) or set(value) != {"house_status", "read_aloud"}:
            raise SettingsError("qwen needs: house_status, read_aloud")
        if not all(isinstance(v, bool) for v in value.values()):
            raise SettingsError("qwen switches must be true or false")
        return dict(value)
    if key == "telegram":
        keys = {"enabled", "allowed_user_id", "digest", "alerts"}
        if not isinstance(value, dict) or set(value) != keys:
            raise SettingsError(
                "telegram needs: enabled, allowed_user_id, digest, alerts"
            )
        for f in ("enabled", "digest", "alerts"):
            if not isinstance(value[f], bool):
                raise SettingsError(f"telegram.{f} must be true or false")
        uid = value["allowed_user_id"]
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise SettingsError("telegram.allowed_user_id must be a positive integer")
        return dict(value)
    if key == "executor":
        keys = {
            "live",
            "operator",
            "require_backup",
            "allowed_tools",
            "deny_names_regex",
            "step_timeout_s",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise SettingsError(f"executor needs: {', '.join(sorted(keys))}")
        for f in ("live", "require_backup"):
            if not isinstance(value[f], bool):
                raise SettingsError(f"executor.{f} must be true or false")
        if value["operator"] not in ("haiku", "python"):
            raise SettingsError("executor.operator must be haiku or python")
        tools = value["allowed_tools"]
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and re.fullmatch(r"ha_[a-z0-9_]+", t) for t in tools
        ):
            raise SettingsError("executor.allowed_tools must be a list of ha_* tools")
        try:
            re.compile(str(value["deny_names_regex"]))
        except re.error as exc:
            raise SettingsError(
                "executor.deny_names_regex is not a valid regex"
            ) from exc
        return {
            **value,
            "allowed_tools": list(tools),
            "step_timeout_s": _num(
                value["step_timeout_s"], "executor.step_timeout_s", 10, 3600
            ),
        }
    if key == "escalation":
        keys = {"max_rounds", "max_usd", "opus_max_usd"}
        if not isinstance(value, dict) or set(value) != keys:
            raise SettingsError("escalation needs: max_rounds, max_usd, opus_max_usd")
        return {
            "max_rounds": int(
                _num(value["max_rounds"], "escalation.max_rounds", 0, 12)
            ),
            "max_usd": _num(value["max_usd"], "escalation.max_usd", 0, 50),
            "opus_max_usd": _num(
                value["opus_max_usd"], "escalation.opus_max_usd", 0, 100
            ),
        }
    if key == "features":
        if not isinstance(value, dict) or not all(
            isinstance(v, bool) for v in value.values()
        ):
            raise SettingsError("features must map names to true/false")
        return value
    if key == "prices_verified":
        return value
    raise SettingsError(f"unknown setting: {key}")


FINDINGS_MAX = 2000


class Store:
    """Thread-safe SQLite wrapper (one short-lived connection per call)."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = (
            Path(path)
            if path
            else Path(os.environ.get("JARVIS_JANITOR_DB", DEFAULT_DB))
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
            try:  # older databases: add the column the read-out hash needs
                c.execute("ALTER TABLE readouts ADD COLUMN source_hash TEXT")
            except sqlite3.OperationalError:
                pass

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn() as c:
            return c.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # -- settings ---------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        out = copy.deepcopy(DEFAULT_SETTINGS)
        for row in self._all("SELECT key, value FROM settings"):
            if row["key"] in out:
                out[row["key"]] = json.loads(row["value"])
        return out

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate and merge ``patch`` (top-level groups replace the old group)."""
        clean = {k: _validate_group(k, v) for k, v in patch.items()}
        with self._lock, self._conn() as c:
            for k, v in clean.items():
                c.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
        return self.settings()

    # -- ledger and runs --------------------------------------------------
    def add_usage(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        ts: float | None = None,
    ) -> None:
        self._run(
            "INSERT INTO ledger(month,model,input_tokens,output_tokens,cache_read_tokens,"
            "cache_write_tokens,cost_usd,calls) VALUES(?,?,?,?,?,?,?,1) "
            "ON CONFLICT(month,model) DO UPDATE SET "
            "input_tokens=input_tokens+excluded.input_tokens,"
            "output_tokens=output_tokens+excluded.output_tokens,"
            "cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,"
            "cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens,"
            "cost_usd=cost_usd+excluded.cost_usd, calls=calls+1",
            (
                month_key(ts),
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cost_usd,
            ),
        )

    def month_spend(self, ts: float | None = None) -> float:
        rows = self._all(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM ledger WHERE month=?",
            (month_key(ts),),
        )
        return float(rows[0]["s"])

    def month_breakdown(self, ts: float | None = None) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM ledger WHERE month=? ORDER BY cost_usd DESC",
            (month_key(ts),),
        )

    def record_run(self, layer: str, model: str | None, **fields: Any) -> int:
        cols = {
            "started": fields.pop("started", time.time()),
            "finished": fields.pop("finished", time.time()),
        }
        allowed = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "outcome",
            "purpose",
            "dry_run",
        )
        cols.update({k: v for k, v in fields.items() if k in allowed})
        names = ", ".join(["layer", "model", *cols])
        marks = ", ".join("?" * (2 + len(cols)))
        cur = self._run(
            f"INSERT INTO runs({names}) VALUES({marks})", (layer, model, *cols.values())
        )
        return int(cur.lastrowid or 0)

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def runs_by_layer(self, layer: str, limit: int = 5) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM runs WHERE layer=? ORDER BY id DESC LIMIT ?", (layer, limit)
        )

    # -- issues -----------------------------------------------------------
    def upsert_issue(
        self,
        fingerprint: str,
        *,
        category: str,
        source_id: str = "",
        title: str = "",
        severity: str = "warning",
        meta: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        existing = self.issue(fingerprint)
        if existing:
            state = "open" if existing["state"] == "resolved" else existing["state"]
            self._run(
                "UPDATE issues SET last_seen=?, occurrences=occurrences+1, severity=?, title=?, state=?, meta=? "
                "WHERE fingerprint=?",
                (
                    now,
                    severity,
                    title or existing["title"],
                    state,
                    json.dumps(meta or {}),
                    fingerprint,
                ),
            )
        else:
            self._run(
                "INSERT INTO issues(fingerprint,category,source_id,title,severity,state,first_seen,last_seen,"
                "occurrences,meta) VALUES(?,?,?,?,?,'open',?,?,1,?)",
                (
                    fingerprint,
                    category,
                    source_id,
                    title,
                    severity,
                    now,
                    now,
                    json.dumps(meta or {}),
                ),
            )
        return self.issue(fingerprint)  # type: ignore[return-value]

    def issue(self, fingerprint: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM issues WHERE fingerprint=?", (fingerprint,))
        return rows[0] if rows else None

    def issues(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            return self._all(
                "SELECT * FROM issues WHERE state=? ORDER BY last_seen DESC", (state,)
            )
        return self._all("SELECT * FROM issues ORDER BY last_seen DESC")

    def set_issue_state(self, fingerprint: str, state: str) -> None:
        if state not in ISSUE_STATES:
            raise ValueError(f"issue state must be one of {ISSUE_STATES}")
        self._run("UPDATE issues SET state=? WHERE fingerprint=?", (state, fingerprint))

    # -- clusters: overrides and Haiku notes ------------------------------
    def set_override(
        self, cluster_id: str, effort: str | None, kind: str | None
    ) -> None:
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        if kind is not None and kind not in CLUSTER_KINDS:
            raise ValueError(f"kind must be one of {CLUSTER_KINDS}")
        if effort is None and kind is None:
            self._run("DELETE FROM cluster_overrides WHERE cluster_id=?", (cluster_id,))
            return
        self._run(
            "INSERT INTO cluster_overrides(cluster_id,effort,kind,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(cluster_id) DO UPDATE SET effort=excluded.effort, kind=excluded.kind, updated=excluded.updated",
            (cluster_id, effort, kind, time.time()),
        )

    def overrides(self) -> dict[str, dict[str, Any]]:
        return {
            r["cluster_id"]: r for r in self._all("SELECT * FROM cluster_overrides")
        }

    def put_triage(
        self,
        cluster_id: str,
        members_hash: str,
        *,
        effort: str | None,
        note: str,
        cause: str,
        model: str,
    ) -> None:
        self._run(
            "INSERT INTO triage_notes(cluster_id,members_hash,effort,note,cause,model,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(cluster_id) DO UPDATE SET members_hash=excluded.members_hash,"
            " effort=excluded.effort, note=excluded.note, cause=excluded.cause, model=excluded.model,"
            " updated=excluded.updated",
            (cluster_id, members_hash, effort, note, cause, model, time.time()),
        )

    def triage_notes(self) -> dict[str, dict[str, Any]]:
        return {r["cluster_id"]: r for r in self._all("SELECT * FROM triage_notes")}

    # -- plan: the user's task selection and on-demand proposals --------------
    def set_selected(self, cluster_id: str, selected: bool) -> None:
        self._run(
            "INSERT INTO task_selection(cluster_id,selected,updated) VALUES(?,?,?) "
            "ON CONFLICT(cluster_id) DO UPDATE SET selected=excluded.selected, updated=excluded.updated",
            (cluster_id, int(selected), time.time()),
        )

    def selected_tasks(self) -> set[str]:
        rows = self._all("SELECT cluster_id FROM task_selection WHERE selected=1")
        return {r["cluster_id"] for r in rows}

    # -- task ids: year_month_day_sequence, one open task per cluster ---------
    def sync_tasks(
        self,
        clusters: list[dict[str, Any]],
        now: float | None = None,
        last_check: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Give every current cluster an open task id; mark vanished ones 'cleared'.

        A cluster that comes back after its task was closed gets a NEW id that
        remembers the old one in ``seen_before``. Returns open/cleared tasks by
        cluster id.
        """
        now = now if now is not None else time.time()
        day = time.strftime("%Y_%m_%d", time.localtime(now))
        present = {c["id"] for c in clusters}
        with self._lock, self._conn() as c:
            live = {
                r["cluster_id"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM tasks WHERE status IN ('open','cleared')"
                ).fetchall()
            }
            for cl in clusters:
                meta = json.dumps(
                    {
                        k: cl.get(k)
                        for k in (
                            "severity",
                            "effort",
                            "examples",
                            "cause",
                            "note",
                            "count",
                        )
                    }
                )
                row = live.get(cl["id"])
                if row:
                    c.execute(
                        "UPDATE tasks SET status='open', cleared_at=NULL, label=?, meta=? WHERE id=?",
                        (cl["label"], meta, row["id"]),
                    )
                    row.update(status="open", label=cl["label"], meta=meta)
                    continue
                prev = c.execute(
                    "SELECT id, closed_at FROM tasks WHERE cluster_id=? "
                    "AND status='closed' ORDER BY closed_at DESC LIMIT 1",
                    (cl["id"],),
                ).fetchone()
                # closed by the owner but the watchdog has not looked again yet:
                # wait for the next check before believing the problem is back
                if prev and prev["closed_at"] > (last_check or 0.0):
                    continue
                n = c.execute(
                    "SELECT COALESCE(MAX(CAST(substr(id, 12) AS INTEGER)), 0) "
                    "FROM tasks WHERE opened_day=?",
                    (day,),
                ).fetchone()[0]
                tid = f"{day}_{n + 1:03d}"
                c.execute(
                    "INSERT INTO tasks(id,cluster_id,label,opened_at,opened_day,seen_before,meta) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        tid,
                        cl["id"],
                        cl["label"],
                        now,
                        day,
                        prev["id"] if prev else None,
                        meta,
                    ),
                )
                live[cl["id"]] = {
                    "id": tid,
                    "cluster_id": cl["id"],
                    "label": cl["label"],
                    "status": "open",
                    "seen_before": prev["id"] if prev else None,
                    "solution": "",
                    "meta": meta,
                    "opened_at": now,
                }
            for cid, row in live.items():
                if cid not in present and row["status"] == "open":
                    c.execute(
                        "UPDATE tasks SET status='cleared', cleared_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    row["status"] = "cleared"
        return live

    def task(self, task_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM tasks WHERE id=?", (task_id,))
        return rows[0] if rows else None

    def open_task_id(self, cluster_id: str) -> str | None:
        rows = self._all(
            "SELECT id FROM tasks WHERE cluster_id=? AND status IN ('open','cleared')",
            (cluster_id,),
        )
        return rows[0]["id"] if rows else None

    def awaiting_check(self, last_check: float | None) -> set[str]:
        """Closed task ids the watchdog has not re-verified yet."""
        rows = self._all(
            "SELECT id FROM tasks WHERE status='closed' AND closed_at>?",
            (last_check or 0.0,),
        )
        return {r["id"] for r in rows}

    def closed_tasks(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM tasks WHERE status='closed' ORDER BY id")

    def set_solution(self, task_id: str, text: str) -> bool:
        cur = self._run(
            "UPDATE tasks SET solution=? WHERE id=? AND status IN ('open','cleared')",
            (text.strip()[: FINDINGS_MAX * 2], task_id),
        )
        return cur.rowcount > 0

    def close_task(
        self, task_id: str, record: dict[str, Any], now: float | None = None
    ) -> None:
        """Close a task and forget its working data (the record keeps the story)."""
        row = self.task(task_id)
        assert row is not None
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE tasks SET status='closed', closed_at=?, record=? WHERE id=?",
                (now if now is not None else time.time(), json.dumps(record), task_id),
            )
            for table in ("task_findings", "plan_proposals", "task_selection"):
                c.execute(
                    f"DELETE FROM {table} WHERE cluster_id=?", (row["cluster_id"],)
                )

    def set_findings(self, cluster_id: str, text: str) -> None:
        """The owner's own diagnosis of a task. Empty text removes it."""
        text = text.strip()[:FINDINGS_MAX]
        old = self.findings().get(cluster_id, "")
        if old and old != text:  # nothing the owner wrote is ever lost
            self._run(
                "INSERT INTO findings_log(cluster_id,text,replaced_at) VALUES(?,?,?)",
                (cluster_id, old, time.time()),
            )
        if not text:
            self._run("DELETE FROM task_findings WHERE cluster_id=?", (cluster_id,))
            return
        self._run(
            "INSERT INTO task_findings(cluster_id,text,updated) VALUES(?,?,?) "
            "ON CONFLICT(cluster_id) DO UPDATE SET text=excluded.text, updated=excluded.updated",
            (cluster_id, text, time.time()),
        )

    def findings(self) -> dict[str, str]:
        rows = self._all("SELECT cluster_id,text FROM task_findings")
        return {r["cluster_id"]: r["text"] for r in rows}

    def put_plan_proposal(
        self,
        cluster_id: str,
        members_hash: str,
        body: dict[str, Any],
        *,
        model: str,
        cost_usd: float,
    ) -> None:
        self._run(
            "INSERT INTO plan_proposals(cluster_id,members_hash,body,model,cost_usd,updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(cluster_id) DO UPDATE SET members_hash=excluded.members_hash, body=excluded.body,"
            " model=excluded.model, cost_usd=excluded.cost_usd, updated=excluded.updated",
            (cluster_id, members_hash, json.dumps(body), model, cost_usd, time.time()),
        )

    def plan_proposals(self) -> dict[str, dict[str, Any]]:
        rows = self._all("SELECT * FROM plan_proposals")
        return {r["cluster_id"]: {**r, "body": json.loads(r["body"])} for r in rows}

    # -- executions: what the user approved and what happened ---------------
    def create_execution(
        self,
        cluster_id: str,
        *,
        label: str,
        members_hash: str,
        instruction: dict[str, Any],
        instruction_hash: str,
        risk: str,
        summary: str,
        live: bool,
    ) -> int:
        now = time.time()
        cur = self._run(
            "INSERT INTO executions(cluster_id,label,members_hash,instruction,instruction_hash,risk,summary,"
            "status,live,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'awaiting_approval',?,?,?)",
            (
                cluster_id,
                label,
                members_hash,
                json.dumps(instruction),
                instruction_hash,
                risk,
                summary,
                int(live),
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    def execution(self, exec_id: int) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM executions WHERE id=?", (exec_id,))
        if not rows:
            return None
        r = rows[0]
        return {
            **r,
            "instruction": json.loads(r["instruction"]),
            "log": json.loads(r["log"]),
        }

    def executions(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._all("SELECT id FROM executions ORDER BY id DESC LIMIT ?", (limit,))
        return [e for e in (self.execution(r["id"]) for r in rows) if e]

    def update_execution(self, exec_id: int, **fields: Any) -> None:
        allowed = {
            "status",
            "telegram_message_id",
            "approved_at",
            "started_at",
            "finished_at",
            "live",
        }
        cols = {k: v for k, v in fields.items() if k in allowed}
        cols["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        self._run(f"UPDATE executions SET {sets} WHERE id=?", (*cols.values(), exec_id))

    def log_execution(self, exec_id: int, event: str, detail: str = "") -> None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT log FROM executions WHERE id=?", (exec_id,)
            ).fetchone()
            if row is None:
                return
            log = json.loads(row["log"])
            log.append({"t": time.time(), "event": event, "detail": detail[:600]})
            c.execute(
                "UPDATE executions SET log=? WHERE id=?",
                (json.dumps(log[-80:]), exec_id),
            )

    # -- Qwen read-outs ----------------------------------------------------
    def add_readout(
        self,
        *,
        report_id: int | None,
        model: str,
        tokens_in: int,
        tokens_out: int,
        seconds: float,
        text: str,
        guard_ok: bool,
        note: str,
        ts: float | None = None,
        source_hash: str | None = None,
    ) -> int:
        cur = self._run(
            "INSERT INTO readouts(created_at,report_id,model,tokens_in,tokens_out,seconds,text,guard_ok,note,source_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                ts if ts is not None else time.time(),
                report_id,
                model,
                tokens_in,
                tokens_out,
                seconds,
                text,
                int(guard_ok),
                note,
                source_hash,
            ),
        )
        return int(cur.lastrowid or 0)

    def latest_readout(self) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM readouts ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # -- unavailable-for-long tracking and Home Assistant restarts ------------
    def track_unavailable(self, rows: dict[str, dict[str, Any]], now: float) -> None:
        """Keep the oldest known ``since`` per entity; drop the ones that came back."""
        with self._lock, self._conn() as c:
            old = {
                r["entity_id"]: r["since"]
                for r in c.execute("SELECT entity_id, since FROM unavail_track")
            }
            c.executemany(
                "INSERT INTO unavail_track(entity_id,name,since,last_seen) VALUES(?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET name=excluded.name, since=excluded.since, last_seen=excluded.last_seen",
                [
                    (
                        eid,
                        r.get("name", ""),
                        min(old.get(eid, now), r.get("since") or now, now),
                        now,
                    )
                    for eid, r in rows.items()
                ],
            )
            gone = [e for e in old if e not in rows]
            c.executemany(
                "DELETE FROM unavail_track WHERE entity_id=?", [(e,) for e in gone]
            )

    def unavailable_longer_than(self, days: float, now: float) -> dict[str, Any]:
        cut = now - days * 86400
        rows = self._all(
            "SELECT entity_id, name, since FROM unavail_track WHERE since<=? ORDER BY since ASC",
            (cut,),
        )
        first = self._all("SELECT MIN(since) AS m, COUNT(*) AS n FROM unavail_track")[0]
        return {
            "count": len(rows),
            "items": rows,
            "tracked": int(first["n"]),
            "counting_since": first["m"],
        }

    def record_boot(self, boot_at: float, now: float, after_outage: bool) -> bool:
        """Remember a Home Assistant start; True when it is a new one."""
        last = self._all("SELECT MAX(boot_at) AS m FROM ha_boots")[0]["m"]
        if last is not None and boot_at <= last + 300:
            return False
        self._run(
            "INSERT OR IGNORE INTO ha_boots(boot_at,detected,after_outage) VALUES(?,?,?)",
            (boot_at, now, int(after_outage)),
        )
        return True

    def boots(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM ha_boots ORDER BY boot_at DESC LIMIT ?", (limit,)
        )

    # -- facts: latest values the watchdog read (backup time, updates, ...) -----
    def set_facts(self, facts: dict[str, Any]) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            for k, v in facts.items():
                c.execute(
                    "INSERT INTO facts(key,value,updated) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                    (k, json.dumps(v), now),
                )

    def facts(self) -> dict[str, Any]:
        return {
            r["key"]: json.loads(r["value"]) for r in self._all("SELECT * FROM facts")
        }

    # -- watchdog bookkeeping ---------------------------------------------
    def watch_states(self) -> dict[str, dict[str, Any]]:
        rows = self._all("SELECT * FROM watch_state")
        return {r["fingerprint"]: r for r in rows}

    def watch_seen(self, fingerprint: str, category: str, now: float) -> float:
        """Mark seen this tick (misses reset); return when it was first seen."""
        self._run(
            "INSERT INTO watch_state(fingerprint,category,first_seen,misses) VALUES(?,?,?,0) "
            "ON CONFLICT(fingerprint) DO UPDATE SET misses=0",
            (fingerprint, category, now),
        )
        return float(
            self._all(
                "SELECT first_seen FROM watch_state WHERE fingerprint=?", (fingerprint,)
            )[0]["first_seen"]
        )

    def watch_miss(self, fingerprint: str) -> int:
        self._run(
            "UPDATE watch_state SET misses=misses+1 WHERE fingerprint=?", (fingerprint,)
        )
        rows = self._all(
            "SELECT misses FROM watch_state WHERE fingerprint=?", (fingerprint,)
        )
        return int(rows[0]["misses"]) if rows else 0

    def watch_forget(self, fingerprint: str) -> None:
        self._run("DELETE FROM watch_state WHERE fingerprint=?", (fingerprint,))

    # -- reports ----------------------------------------------------------
    def add_report(
        self,
        status: str,
        summary_text: str,
        body: dict[str, Any],
        ts: float | None = None,
    ) -> int:
        if status not in ("ok", "attention", "critical"):
            raise ValueError("report status must be ok, attention or critical")
        cur = self._run(
            "INSERT INTO reports(created_at,status,summary_text,body) VALUES(?,?,?,?)",
            (
                ts if ts is not None else time.time(),
                status,
                summary_text,
                json.dumps(body),
            ),
        )
        return int(cur.lastrowid or 0)

    def latest_report(self) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM reports ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        return {**rows[0], "body": json.loads(rows[0]["body"] or "{}")}

    # -- proposals --------------------------------------------------------
    def add_proposal(
        self,
        fingerprint: str,
        model: str,
        diagnosis: str,
        risk: str,
        change: dict[str, Any],
        rollback: dict[str, Any],
        verification: list[str],
    ) -> int:
        if risk not in RISKS:
            raise ValueError(f"risk must be one of {RISKS}")
        now = time.time()
        cur = self._run(
            "INSERT INTO proposals(fingerprint,model,diagnosis,risk,change,rollback,verification,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                fingerprint,
                model,
                diagnosis,
                risk,
                json.dumps(change),
                json.dumps(rollback),
                json.dumps(verification),
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    def set_proposal_status(
        self, proposal_id: int, status: str, telegram_message_id: str | None = None
    ) -> None:
        if status not in PROPOSAL_STATES:
            raise ValueError(f"proposal status must be one of {PROPOSAL_STATES}")
        self._run(
            "UPDATE proposals SET status=?, telegram_message_id=COALESCE(?,telegram_message_id), updated_at=? WHERE id=?",
            (status, telegram_message_id, time.time(), proposal_id),
        )

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = (
            self._all(
                "SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,)
            )
            if status
            else self._all("SELECT * FROM proposals ORDER BY id DESC")
        )
        for r in rows:
            for f in ("change", "rollback", "verification"):
                r[f] = json.loads(r[f] or "null")
        return rows


_store: Store | None = None


def get_store() -> Store:
    """Process-wide store (path from ``JARVIS_JANITOR_DB`` or ``~/.openjarvis``)."""
    global _store
    if _store is None:
        _store = Store()
    return _store


def reset_store_for_tests() -> None:
    global _store
    _store = None
