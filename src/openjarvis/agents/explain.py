"""Plain-language details for issues, written by Python (no model).

* An automation repair ("service_not_found", "validation_failed") says which
  automation it is and why, instead of "Repair: automation / service_not_found".
* A cluster of unavailable entities lists them (name + entity id), limited to
  ``LIST_LIMIT`` with a count of the rest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from openjarvis.agents.store import Store

LIST_LIMIT = 10

AUTOMATION_REPAIR = re.compile(
    r"^(?P<eid>automation\.[a-z0-9_]+?)_(?:service_not_found_(?P<svc>.+)"
    r"|validation_failed_(?P<part>triggers|conditions|actions))$"
)
_alias_cache: dict[str, Any] = {"mtime": None, "map": {}}


def _pretty(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def automation_name(entity_id: str) -> str:
    """The automation's real name from the Architect map, else a tidied id."""
    from openjarvis.agents.architect import cache_path

    path: Path = cache_path()
    try:
        mtime = path.stat().st_mtime
        if _alias_cache["mtime"] != mtime:
            inv = json.loads(path.read_text(encoding="utf-8"))
            _alias_cache.update(
                mtime=mtime,
                map={it["entity_id"]: it["alias"] for it in inv.get("items", [])},
            )
    except (OSError, ValueError):
        pass
    return _alias_cache["map"].get(entity_id) or _pretty(entity_id)


def explain_repair(issue: dict[str, Any]) -> tuple[str, str] | None:
    """(automation name, why) for an automation repair, else None."""
    if issue.get("category") != "repair":
        return None
    source = str(issue.get("source_id") or "")
    if not source.startswith("automation/"):
        return None
    m = AUTOMATION_REPAIR.match(source.split("/", 1)[1])
    if not m:
        return None
    name = automation_name(m["eid"])
    if m["svc"]:
        return name, (
            f"calls {m['svc']}, which does not exist in Home Assistant "
            "(integration removed or renamed)"
        )
    return name, (
        f"its {m['part']} are invalid: an entity, device or service they use "
        "is missing or changed"
    )


def example_text(issue: dict[str, Any]) -> str:
    found = explain_repair(issue)
    return f"{found[0]}: {found[1]}" if found else str(issue["title"])


def _meta(issue: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(issue.get("meta") or "{}")
    except (TypeError, ValueError):
        return {}


def affected(store: Store, members: list[dict[str, Any]]) -> tuple[list[dict], int]:
    """Up to ``LIST_LIMIT`` {name, detail} rows for a cluster, and the total."""
    rows: list[dict[str, str]] = []
    for m in members:
        found = explain_repair(m)
        if found:
            rows.append({"name": found[0], "detail": found[1]})
    if rows:
        return rows[:LIST_LIMIT], len(rows)
    ids: list[str] = []
    total = 0
    tracked: dict[str, str] | None = None
    for m in members:
        if m["category"] != "entity_group":
            continue
        meta = _meta(m)
        count = int(meta.get("count") or 0)
        have = list(meta.get("entities") or meta.get("examples") or [])
        if len(have) < count:  # an older issue stored only a few: use the tracker
            if tracked is None:
                tracked = {
                    r["entity_id"]: r["name"]
                    for r in store._all("SELECT entity_id, name FROM unavail_track")  # noqa: SLF001
                }
            dom = str(meta.get("domain") or "")
            have = sorted(e for e in tracked if e.split(".")[0] == dom) or have
        ids += have
        total += max(count, len(have))
    if not ids:
        return [], 0
    names = {
        r["entity_id"]: r["name"]
        for r in store._all("SELECT entity_id, name FROM unavail_track")  # noqa: SLF001
    }
    out = [{"name": names.get(e) or _pretty(e), "detail": e} for e in ids]
    return out[:LIST_LIMIT], total
