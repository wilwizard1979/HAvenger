"""The plain-English house status: one short file Qwen reads and talks about.

``build_house_status`` is deterministic Python over the Janitor's real data, with
Haiku's one-line notes added where they exist. It has no entity ids and no jargon.
The same text is written to ``<janitor dir>/house-status.md`` so Qwen (and you)
always know where it is. ``ask_house`` lets Qwen answer a question from that text
only, with the same number check as the read-out; a wrong number never gets through.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from openjarvis.agents import readout
from openjarvis.agents.clusters import build_clusters, matrix_counts
from openjarvis.agents.planning import overview
from openjarvis.agents.store import Store

logger = logging.getLogger(__name__)

FILE_NAME = "house-status.md"
TOP = 3

PLAIN = {
    "devices:motion": "The motion sensors",
    "devices:critical": "The safety devices (smoke alarms, locks, alarm)",
    "chore:batteries": "Low batteries",
    "backup": "The backups",
}

ASK_SYSTEM = (
    "You answer questions about a home using ONLY the status text you are given. "
    "Answer in at most three short, plain sentences, in English. Copy every number "
    "exactly as written. If the text does not contain the answer, say you do not "
    "know. Do not give advice and do not guess."
)


def status_path() -> Path:
    from openjarvis.agents.watchdog import janitor_dir

    return janitor_dir() / FILE_NAME


def plain_label(cluster: dict[str, Any]) -> str:
    cid = cluster["id"]
    if cid in PLAIN:
        return PLAIN[cid]
    kind, _, name = cid.partition(":")
    if kind == "integration":
        return f"The {name.replace('_', ' ')} connection"
    if kind == "entities":
        return f"Offline {name.replace('_', ' ')} devices"
    return cluster["label"]


def _span(hours: float) -> str:
    if hours < 2:
        return "less than 2 hours"
    if hours < 48:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"


def build_house_status(store: Store, now: float | None = None) -> str:
    now = now if now is not None else time.time()
    s = store.settings()
    ov = overview(store, now)
    c = ov["cards"]
    clusters = build_clusters(
        store.issues(), s, store.overrides(), store.triage_notes()
    )
    mx = matrix_counts(clusters)
    active = [i for i in store.issues() if i["state"] in ("open", "acknowledged")]
    serious = sum(1 for i in active if i["severity"] == "critical")
    down = [i for i in active if i["category"] == "critical_entity"]
    days = int(s["thresholds"].get("stale_unavailable_days", 10))
    when = datetime.fromtimestamp(now, ZoneInfo(s["schedule"]["timezone"]))

    if down or c["backup_failed"]["value"]:
        verdict = "Something important needs attention."
    elif active:
        verdict = "Mostly fine, a few things need attention."
    else:
        verdict = "All good."

    lines = [f"# House status, {when.strftime('%A %d %B, %H:%M')}", "", verdict, ""]
    lines.append(
        f"- Open problems: {len(active)}, of which {serious} are serious "
        f"and {len(active) - serious} are minor."
    )
    lines.append(
        "- Safety devices: all fine."
        if not down
        else f"- Safety devices: {len(down)} not responding."
    )
    last = c["last_backup"]["value"]
    backup = "unknown" if last is None else f"{_span(last)} ago"
    lines.append(
        f"- Backup: the last good one was {backup}"
        + (
            f"; {c['backup_failed']['value']} backup problems."
            if c["backup_failed"]["value"]
            else "."
        )
    )
    if c["updates_pending"]["value"] is not None:
        lines.append(f"- Updates waiting: {c['updates_pending']['value']}.")
    if c["deprecations"]["value"]:
        lines.append(
            f"- Coming changes: {c['deprecations']['value']} things will stop working "
            "in a future Home Assistant version "
            f"(first: {c['deprecations']['next_version']})."
        )
    mb = c["motion_batteries"]
    if mb["value"] is not None:
        if mb["kind"] == "ok":
            lines.append("- Motion sensor batteries: all fine.")
        else:
            parts = []
            if mb["red"]:
                parts.append(f"{mb['red']} unknown or offline")
            if mb["orange"]:
                parts.append(f"{mb['orange']} below {mb['warn_pct']}%")
            lines.append("- Motion sensor batteries: " + ", ".join(parts) + ".")
    lines.append(
        f"- Devices offline for more than {days} days: "
        f"{c['stale_unavailable']['value']}."
    )
    if c["last_restart"]["value"] is not None:
        lines.append(
            f"- Home Assistant last restarted {_span(c['last_restart']['value'])} ago."
        )

    by_id = {x["id"]: x for x in clusters}
    quick = [by_id[i] for i in mx["quick_wins"][:TOP]]
    if quick:
        lines += [
            "",
            "Easy wins: "
            + ", ".join(f"{plain_label(q)} ({q['count']})" for q in quick)
            + ".",
        ]
    needs = len(mx["needs_user"])
    chores = len(mx["chores"])
    lines.append(
        f"Waiting for your decision: {needs} groups of problems. "
        f"Chores for you: {chores}."
    )
    if clusters:
        lines += ["", "What to look at first:"]
        for cl in clusters[:TOP]:
            note = f" Note: {cl['note']}" if cl.get("note") else ""
            n = cl["count"]
            lines.append(
                f"- {plain_label(cl)}: {n} problem{'' if n == 1 else 's'}.{note}"
            )
    spend = store.month_spend(now)
    lines += [
        "",
        f"Cloud cost this month: ${spend:.2f} of "
        f"${s['budget']['monthly_limit_usd']:.2f}.",
    ]
    return "\n".join(lines) + "\n"


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def write_house_status(store: Store, now: float | None = None) -> tuple[str, Path]:
    text = build_house_status(store, now)
    path = status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:  # a read-only disk must not break anything
        logger.warning("Could not write %s: %s", path, exc)
    return text, path


def spoken_text(store: Store, text: str) -> str:
    """The verified Qwen read-out of exactly this status, else the status itself."""
    r = store.latest_readout()
    if r and r["guard_ok"] and r.get("source_hash") == source_hash(text):
        return r["text"]
    return text


def ask_house(
    store: Store,
    question: str,
    *,
    generate: Callable[[str, str, str], dict[str, Any]] = readout.ollama_generate,
) -> dict[str, Any]:
    """Qwen answers from the house status only; numbers are checked."""
    if not store.settings()["qwen"]["house_status"]:
        return {"status": "disabled"}
    text, _ = write_house_status(store)
    allowed = readout.numbers(text) | readout.numbers(question)
    model = readout.DEFAULT_MODEL
    prompt = f"STATUS:\n{text}\nQUESTION: {question.strip()[:300]}"
    try:
        out = generate(model, ASK_SYSTEM, prompt)
        bad = readout.numbers(out["text"]) - allowed
        if bad:
            out = generate(
                model,
                ASK_SYSTEM,
                prompt
                + f"\nNot faithful: {', '.join(sorted(bad))} is not in the status. "
                "Answer again using only numbers from the status.",
            )
            bad = readout.numbers(out["text"]) - allowed
    except Exception as exc:  # noqa: BLE001 - Qwen being off is not an error here
        return {"status": "unavailable", "detail": str(exc)[:200], "status_text": text}
    answer = out["text"].strip()
    if bad or not answer or len(answer) > readout.MAX_CHARS:
        return {
            "status": "fallback",
            "answer": text,
            "note": "Qwen could not answer safely; here is the full status.",
        }
    store.record_run(
        "L1",
        model,
        outcome="ok",
        purpose="ask",
        input_tokens=out["tokens_in"],
        output_tokens=out["tokens_out"],
        cost_usd=0.0,
    )
    return {"status": "ok", "answer": answer}
