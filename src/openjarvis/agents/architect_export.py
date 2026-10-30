"""Files for Claude web: the cached automations as YAML, scenes and KPIs.

Reads only the local inventory cache (no Home Assistant call, no model call). The
user adds the files to a Claude web project with "Upload from device". Secrets are
redacted; entity and room names stay, because the questions need them.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from openjarvis.agents.redaction import Redactor

DEFAULT_DIR = Path.home() / "Documents" / "Jarvis Architect Export"


def _dump(data: Any) -> str:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return Redactor.redact_secrets(text)


def automations_yaml(inv: dict[str, Any]) -> str:
    rows = [it["config"] for it in inv["items"] if it.get("config")]
    skipped = [it["entity_id"] for it in inv["items"] if not it.get("config")]
    head = f"# {len(rows)} automations from Home Assistant (read-only copy).\n" + (
        f"# Not readable, left out: {', '.join(skipped)}\n" if skipped else ""
    )
    return head + _dump(rows)


def scenes_yaml(inv: dict[str, Any]) -> str:
    rows = [
        {"entity_id": eid, "name": s.get("name"), "entities": s.get("members", [])}
        for eid, s in sorted((inv.get("scenes") or {}).items())
    ]
    head = (
        "# Scenes: name and member entities only. Stored values (brightness, colour)\n"
        "# are not in this copy.\n"
    )
    return head + _dump(rows)


def kpis_md(inv: dict[str, Any]) -> str:
    k = inv["kpis"]
    lines = [
        "# Automation KPIs",
        "",
        f"- Automations: {k['total']} ({k['on']} on, {k['off']} off)",
        f"- Unavailable: {k['unavailable']}; unreadable: {k['unreadable']}",
        f"- With broken references: {k['broken']}",
        f"- Not run for over a year: {k['stale']}",
        f"- Ran in the last 7 days: {k['recent']}",
        "",
        "## Groups (by name prefix)",
        "",
    ]
    for g in inv["groups"]:
        lines.append(
            f"- {g['name']}: {g['count']} ({g['broken']} broken, "
            f"{g['stale']} not run > 1 y, {g['disabled']} off)"
        )
    lines += ["", "## Automations with broken references", ""]
    for it in inv["items"]:
        if it.get("broken"):
            gone = ", ".join(it.get("missing", []) + it.get("offline", []))
            lines.append(f"- {it['alias']} ({it['entity_id']}): {gone}")
    return Redactor.redact_secrets("\n".join(lines) + "\n")


def export(inv: dict[str, Any], out_dir: Path | None = None) -> dict[str, Any]:
    out = Path(out_dir) if out_dir else DEFAULT_DIR
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "automations.yaml": automations_yaml(inv),
        "scenes.yaml": scenes_yaml(inv),
        "kpis.md": kpis_md(inv),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return {
        "dir": str(out),
        "files": {n: len(t.encode()) for n, t in files.items()},
        "inventory_age_h": round((time.time() - inv.get("fetched_at", 0)) / 3600, 1),
    }
