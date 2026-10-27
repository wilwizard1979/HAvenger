"""Small, checked edits of an automation's config, applied by Python.

A model never writes a whole config. It names an automation and a few edits
(``{"path": "actions.2.action", "set": "notify.mobile_app_x"}``). When the owner is
asked for approval, Python has already read the CURRENT config (read-only), applied
the edits itself and stored a before/after diff plus hashes inside the approved
instruction. At run time Python re-reads the config; if it is not exactly what was
approved, nothing is written. The write goes through ``ha_config_set_automation``,
which only works when the owner added that tool to ``executor.allowed_tools`` (and
Home Assistant's own approval still applies on top).
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from typing import Any, Callable

EDIT_TOOL = "janitor_edit_automation"
WRITE_TOOL = "ha_config_set_automation"
READ_TOOL = "ha_config_get_automation"
MAX_EDITS = 10
DIFF_LINES = 60
PATH = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*$")


class EditError(ValueError):
    """The edit cannot be prepared or applied; nothing was changed."""


def digest(config: Any) -> str:
    raw = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_config(text: str) -> dict[str, Any]:
    """The config out of a ``ha_config_get_automation`` reply."""
    try:
        body = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise EditError("the automation config could not be read") from exc
    if not isinstance(body, dict):
        raise EditError("the automation config could not be read")
    if body.get("error") or body.get("success") is False:
        raise EditError(str(body.get("error") or body.get("message"))[:200])
    config = body.get("config", body)
    if not isinstance(config, dict) or not config:
        raise EditError("the automation config is empty")
    return config


def _walk(node: Any, parts: list[str], create_last: bool) -> tuple[Any, str]:
    for i, part in enumerate(parts[:-1]):
        node = _child(node, part, ".".join(parts[: i + 1]))
    last = parts[-1]
    if isinstance(node, list):
        if not last.isdigit() or int(last) >= len(node):
            raise EditError(f"index {last} does not exist")
    elif isinstance(node, dict):
        if last not in node and not create_last:
            raise EditError(f"{'.'.join(parts)} does not exist")
    else:
        raise EditError(f"{'.'.join(parts[:-1])} is not a container")
    return node, last


def _child(node: Any, part: str, where: str) -> Any:
    if isinstance(node, list):
        if not part.isdigit() or int(part) >= len(node):
            raise EditError(f"{where} does not exist")
        return node[int(part)]
    if isinstance(node, dict) and part in node:
        return node[part]
    raise EditError(f"{where} does not exist")


def apply_edits(config: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
    out = copy.deepcopy(config)
    for e in edits:
        path = e.get("path")
        if not isinstance(path, str) or not PATH.match(path):
            raise EditError("bad path")
        parts = path.split(".")
        if ("set" in e) == ("delete" in e):
            raise EditError(f"{path}: give exactly one of set / delete")
        node, last = _walk(out, parts, create_last="set" in e)
        if "set" in e:
            if isinstance(node, list):
                node[int(last)] = e["set"]
            else:
                node[last] = e["set"]
        else:
            if isinstance(node, list):
                del node[int(last)]
            else:
                del node[last]
    return out


def diff_lines(before: Any, after: Any) -> list[str]:
    a = json.dumps(before, indent=2, sort_keys=True).splitlines()
    b = json.dumps(after, indent=2, sort_keys=True).splitlines()
    lines = list(difflib.unified_diff(a, b, "before", "after", n=1, lineterm=""))
    return lines[:DIFF_LINES] + (["…"] if len(lines) > DIFF_LINES else [])


def edit_problems(step: dict[str, Any], allowed_tools: list[str]) -> list[str]:
    out: list[str] = []
    if WRITE_TOOL not in allowed_tools:
        out.append(
            f"editing automations is not enabled: add {WRITE_TOOL} to the allowed tools"
        )
    args = step.get("args") or {}
    if not isinstance(args.get("identifier"), str) or not args["identifier"]:
        out.append("args.identifier is required")
    if "restore_config" in args:
        if not isinstance(args["restore_config"], dict):
            out.append("args.restore_config must be an object")
        return out
    edits = args.get("edits")
    if not isinstance(edits, list) or not 0 < len(edits) <= MAX_EDITS:
        out.append(f"args.edits needs 1 to {MAX_EDITS} edits")
    else:
        for e in edits:
            if not isinstance(e, dict) or not PATH.match(str(e.get("path", ""))):
                out.append("an edit has a bad path")
            elif ("set" in e) == ("delete" in e):
                out.append(f"{e.get('path')}: give exactly one of set / delete")
    return out


def prepare(
    instruction: dict[str, Any], read: Callable[[str, dict[str, Any]], str]
) -> dict[str, Any]:
    """Read the current configs, add the diff and hashes, add restore steps.

    Returns a new instruction; raises ``EditError`` if any config cannot be read
    or an edit does not apply.
    """
    steps = copy.deepcopy(instruction.get("steps") or [])
    restores: list[dict[str, Any]] = []
    for st in steps:
        if st.get("tool") != EDIT_TOOL:
            continue
        args = st["args"]
        if "restore_config" in args:
            continue
        before = parse_config(read(READ_TOOL, {"identifier": args["identifier"]}))
        after = apply_edits(before, args["edits"])
        if after == before:
            raise EditError(f"{args['identifier']}: the edits change nothing")
        st["preview"] = {
            "before_hash": digest(before),
            "after_hash": digest(after),
            "diff": diff_lines(before, after),
        }
        restores.append(
            {
                "tool": EDIT_TOOL,
                "args": {"identifier": args["identifier"], "restore_config": before},
                "purpose": f"restore the original {args['identifier']}",
                "verify": "config equals the one before the change",
                "preview": {
                    "before_hash": digest(after),
                    "after_hash": digest(before),
                    "diff": [],
                },
            }
        )
    rollback = restores[::-1] + [
        r
        for r in (instruction.get("rollback_steps") or [])
        if r.get("tool") != EDIT_TOOL
    ]
    return {**instruction, "steps": steps, "rollback_steps": rollback}


def _edits_present(config: dict[str, Any], edits: list[dict[str, Any]]) -> bool:
    for e in edits:
        parts = e["path"].split(".")
        node: Any = config
        try:
            for part in parts[:-1]:
                node = _child(node, part, e["path"])
            last = node[int(parts[-1])] if isinstance(node, list) else node[parts[-1]]
        except (EditError, KeyError, IndexError, ValueError, TypeError):
            if "delete" in e:
                continue
            return False
        if "delete" in e or last != e["set"]:
            return False
    return True


def run_live(step: dict[str, Any], call: Callable[[str, dict[str, Any]], str]) -> str:
    """Apply one prepared edit. Writes only if the config is exactly as approved."""
    args, prev = step["args"], step.get("preview") or {}
    current = parse_config(call(READ_TOOL, {"identifier": args["identifier"]}))
    if digest(current) != prev.get("before_hash"):
        raise EditError("the automation changed since you approved it; nothing written")
    new = (
        args["restore_config"]
        if "restore_config" in args
        else apply_edits(current, args["edits"])
    )
    if digest(new) != prev.get("after_hash"):
        raise EditError("the edit no longer gives the approved result; nothing written")
    call(WRITE_TOOL, {"identifier": args["identifier"], "config": new})
    check = parse_config(call(READ_TOOL, {"identifier": args["identifier"]}))
    # Home Assistant may normalise a config it stores, so check the edited paths
    # (not the whole hash) for ordinary edits
    if "edits" in args and not _edits_present(check, args["edits"]):
        raise EditError("the config read back does not show the approved edit")
    return f"{args['identifier']} updated and verified ({prev.get('after_hash')})"
