"""Step 4 of Jarvis the Architect: from a recommendation to a Telegram approval.

Sonnet/Opus end their answer with a small ``edits`` block (JSON). Python parses it,
checks it against the case (only the automations that were sent, only known
entities), reads the CURRENT config and builds the exact diff. The approval request
then goes through the Janitor's executor: Telegram buttons, frozen snapshot, backup
check, simulation unless ``executor.live`` is on, restore step, read-back check.
Nothing is written here and no model is called.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from openjarvis.agents import config_edit
from openjarvis.agents import janitor_executor as executor
from openjarvis.agents.architect import ENTITY_DOMAINS, ENTITY_IN_TEXT
from openjarvis.agents.store import Store

BLOCK = re.compile(r"```edits\s*(.*?)```", re.DOTALL | re.IGNORECASE)
RISKS = ("low", "medium", "high")
MAX_AUTOMATIONS = 1


class ApplyError(ValueError):
    """The recommendation cannot become an approval request."""


def parse_edits(answer: str) -> dict[str, Any] | None:
    """The ``edits`` block of an answer, or None when there is none/it is unreadable."""
    m = BLOCK.search(answer or "")
    if not m:
        return None
    try:
        body = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    edits = body.get("edits")
    if not isinstance(body.get("identifier"), str) or not isinstance(edits, list):
        return None
    risk = str(body.get("risk", "medium")).lower()
    return {
        "identifier": body["identifier"].strip(),
        "edits": edits,
        "risk": risk if risk in RISKS else "medium",
    }


def _entities_in_value(value: Any) -> set[str]:
    text = json.dumps(value, default=str).lower()
    return {
        m for m in ENTITY_IN_TEXT.findall(text) if m.split(".")[0] in ENTITY_DOMAINS
    }


def proposal_problems(
    proposal: dict[str, Any] | None, case: dict[str, Any], inv: dict[str, Any]
) -> list[str]:
    """Reasons this proposal must not be sent for approval (empty = fine)."""
    if not proposal:
        return ["the answer has no readable edits block"]
    out: list[str] = []
    ident = proposal["identifier"]
    targets = {t["entity_id"] for t in case.get("targets") or []}
    if ident not in targets:
        out.append(f"{ident} was not one of the automations sent for review")
    item = next((i for i in inv.get("items", []) if i["entity_id"] == ident), None)
    if item is None:
        out.append(f"{ident} is not in the inventory")
    elif item["disabled"]:
        out.append(f"{ident} is off; switch it on separately")
    known = set(inv.get("entity_info", {})) | {i["entity_id"] for i in inv["items"]}
    for e in proposal["edits"]:
        if not isinstance(e, dict):
            out.append("an edit is not an object")
            continue
        if "set" in e:
            missing = sorted(_entities_in_value(e["set"]) - known)
            if missing:
                out.append(f"unknown entity in {e.get('path')}: {', '.join(missing)}")
    out += config_edit.edit_problems(
        {"args": {"identifier": ident, "edits": proposal["edits"]}},
        [config_edit.WRITE_TOOL],
    )
    return out


def _check_old(config: dict[str, Any], edits: list[dict[str, Any]]) -> None:
    """``old`` guards against a shifted index: the value at the path must match."""
    for e in edits:
        if "old" not in e:
            continue
        node: Any = config
        try:
            for part in e["path"].split("."):
                node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise config_edit.EditError(f"{e['path']} does not exist") from None
        if node != e["old"]:
            raise config_edit.EditError(
                f"{e['path']} is not what the review saw; ask again"
            )


def direct_read() -> tuple[Callable[[str, dict[str, Any]], str], Callable[[], None]]:
    """Untruncated read-only reader (the allow-listed reader cuts long configs)."""
    from openjarvis.server.ha_dashboard_routes import _HA

    ha = _HA()

    def read(tool: str, args: dict[str, Any]) -> str:
        if tool != config_edit.READ_TOOL:
            raise ApplyError("only automation reads are allowed here")
        raw = ha._client.call_tool(tool, args)
        return "".join(c.get("text", "") for c in raw.get("content", []))

    return read, getattr(ha, "close", lambda: None)


def _no_close() -> None:
    return None


def build_instruction(case: dict[str, Any]) -> dict[str, Any]:
    p = case["proposal"]
    return {
        "preconditions": ["fresh backup (checked before a live run)"],
        "steps": [
            {
                "tool": config_edit.EDIT_TOOL,
                "args": {"identifier": p["identifier"], "edits": p["edits"]},
                "purpose": case["question"][:200],
                "verify": "config read back shows the edit",
            }
        ],
        "rollback_steps": [],
    }


def request_approval(
    store: Store,
    case: dict[str, Any],
    inv: dict[str, Any],
    read: Callable[[str, dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Create the awaiting_approval execution (the route sends the Telegram message)."""
    problems = proposal_problems(case.get("proposal"), case, inv)
    if problems:
        return {"status": "not_executable", "problems": problems}
    cfg = store.settings()["executor"]
    instruction = build_instruction(case)
    problems = executor.execution_problems(instruction, cfg)
    if problems:
        return {"status": "not_executable", "problems": problems}
    cid = f"architect:{case['id']}"
    if any(
        e["cluster_id"] == cid and e["status"] in executor.ACTIVE
        for e in store.executions(50)
    ):
        return {"status": "already_active"}
    close: Callable[[], None] = _no_close
    try:
        if read is None:
            read, close = direct_read()
        step = instruction["steps"][0]
        _check_old(
            config_edit.parse_config(
                read(config_edit.READ_TOOL, {"identifier": step["args"]["identifier"]})
            ),
            step["args"]["edits"],
        )
        instruction = config_edit.prepare(instruction, read)
    except config_edit.EditError as exc:
        return {"status": "not_executable", "problems": [str(exc)[:300]]}
    finally:
        close()
    problems = executor.execution_problems(instruction, cfg)
    if problems:
        return {"status": "not_executable", "problems": problems}
    exec_id = store.create_execution(
        cid,
        label=f"{case['id']} · {case['proposal']['identifier']}",
        members_hash=case["id"],
        instruction=instruction,
        instruction_hash=executor.instruction_hash(instruction),
        risk=case["proposal"]["risk"],
        summary=f"Architect ({case['model']}): {case['question'][:300]}",
        live=bool(cfg["live"]),
    )
    store.log_execution(exec_id, "requested", "waiting for your approval in Telegram")
    return {"status": "awaiting_approval", "id": exec_id}
