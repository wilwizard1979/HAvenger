"""What Jarvis the Architect sends to Sonnet/Opus for one request, and what it costs.

Never the whole automations file. Python picks:

1. the target automation(s), at most ``MAX_TARGETS``, as compact YAML;
2. the entities they use, one line each (name, area, state);
3. the other automations that touch the same entities, at most ``MAX_RELATED`` as
   compact YAML cut down to the parts that mention the shared entities, with an
   impact line; the rest only as a count;
4. a fixed rules block.

Disabled automations are never sent (they are counted so the page can say so).
The size is estimated before any call; over ``TOKEN_CAP`` the request is refused
and the user is asked for a narrower question.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from openjarvis.agents.architect import ENTITY_IN_TEXT, all_entities, entities_in

MAX_TARGETS = 3
MAX_EXACT = 8  # automations that use an entity named in the question
MAX_RELATED = 6
MAX_ENTITY_LINES = 30
MAX_SCENES = 10
TOKEN_CAP = 16_000
CHARS_PER_TOKEN = 3.5  # same rough rule as the cloud client
OUTPUT_TOKENS = 1200  # a diff plus a short explanation

CREATE_WORDS = (
    "create",
    "new automation",
    "build an automation",
    "build a new",
    "write an automation",
    "make an automation",
    "make a new automation",
    "set up an automation",
    "add an automation",
    "add a new automation",
)
CHANGE_WORDS = (
    "change",
    "extend",
    "shorten",
    "reduce",
    "increase",
    "decrease",
    "modify",
    "update",
    "fix",
    "adjust",
    "replace",
    "rename",
    "propose",
    "recommend",
    "instead of",
    "should be",
    "make it",
    "lengthen",
)

RULES = """You review Home Assistant automations. You do not execute anything.
Answer in this order:
1. What I understood (one sentence).
2. Automations and entities touched.
3. The change as a diff (before and after), changing as little as possible.
4. Impact on the related automations (conflict, double trigger, loop, or none).
5. Risk (low, medium or high) and why.
6. How to undo it.
Use only the automations and entities provided. If something is missing, say so;
never invent entity ids. Do not touch secrets. For a new automation, use only
entities from the list and deliver it disabled until approved. Change only the
requested automation; if another one must change too, suggest it separately.
Placeholders like <email_1> are redacted values: keep them unchanged."""

_NOISE = (
    "metadata",
)  # nested "id" (trigger ids) must stay: choose/conditions use them


def classify(question: str) -> str:
    """'create', 'change' or 'lookup' (the page lets the user override it)."""
    q = " " + re.sub(r"\s+", " ", question.lower()) + " "
    if any(w in q for w in CREATE_WORDS):
        return "create"
    if any(re.search(rf"\b{re.escape(w)}", q) for w in CHANGE_WORDS):
        return "change"
    return "lookup"


def _clean(node: Any) -> Any:
    """Drop empty values and defaults so the YAML is short."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _NOISE or (k == "enabled" and v is True):
                continue
            v = _clean(v)
            if v in ({}, [], "", None):
                continue
            out[k] = v
        return out
    if isinstance(node, list):
        return [x for x in (_clean(v) for v in node) if x not in ({}, [], "", None)]
    return node


def compact_yaml(config: dict[str, Any], only: set[str] | None = None) -> str:
    """Config as short YAML. With ``only``, keep just the blocks that mention them."""
    cfg = _clean({k: v for k, v in config.items() if k != "id"})  # the numeric id
    if only:
        for part in ("triggers", "conditions", "actions"):
            items = cfg.get(part)
            if not isinstance(items, list):
                continue
            keep = [x for x in items if only & entities_in(x) or _mentions(x, only)]
            skipped = len(items) - len(keep)
            if skipped:
                keep.append(f"... {skipped} more not shown")
            cfg[part] = keep
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True).rstrip()


def _mentions(node: Any, ids: set[str]) -> bool:
    text = json.dumps(node)
    return any(i in text for i in ids)


def _words(question: str) -> list[str]:
    from openjarvis.agents.architect import STOP_WORDS

    return [
        w
        for w in re.findall(r"[a-z0-9_]{3,}", question.lower())
        if w not in STOP_WORDS and w not in CHANGE_WORDS and w not in CREATE_WORDS
    ]


def find_targets(
    inv: dict[str, Any], question: str, picked: list[str] | None = None
) -> list[dict[str, Any]]:
    """The automations the request is about: the picked ones, else the best matches.

    Only enabled automations count. An entity id in the question wins over words.
    """
    items = [it for it in inv.get("items", []) if it.get("readable")]
    by_id = {it["entity_id"]: it for it in items}
    if picked:  # a disabled automation is never sent, even when picked
        return [by_id[p] for p in picked if p in by_id and not by_id[p]["disabled"]][
            :MAX_TARGETS
        ]
    enabled = [it for it in items if not it["disabled"]]
    ids = {m for m in ENTITY_IN_TEXT.findall(question.lower()) if "." in m}
    exact = [it for it in enabled if ids & all_entities(it)]
    if exact:  # the user named the entity: those automations, nothing guessed
        exact.sort(
            key=lambda it: (
                not (ids & set(it["entities"])),  # direct users before scene users
                -len(ids & all_entities(it)),
                it["alias"].lower(),
            )
        )
        return exact[:MAX_EXACT]
    words = _words(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for it in enabled:
        score = 0
        alias = it["alias"].lower()
        score += 6 * len(ids & set(it["entities"]))
        score += 3 * sum(1 for w in words if w in alias)
        score += sum(1 for w in words if any(w in e for e in it["entities"]))
        if score:
            scored.append((score, it))
    scored.sort(key=lambda s: (-s[0], s[1]["alias"].lower()))
    if not scored:
        return []
    best = scored[0][0]  # a weak match next to a strong one is not a target
    return [it for sc, it in scored if sc * 2 >= best][:MAX_TARGETS]


def _acts_on(config: dict[str, Any], entity: str) -> bool:
    return entity in (set().union(*(entities_in(a) for a in config.get("actions", []))))


def related(
    inv: dict[str, Any], targets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """(shown, hidden count, disabled count) of automations sharing entities.

    Sharing counts through scenes too: two automations that set the same lamp,
    one directly and one by a scene, are related.
    """
    target_ids = {t["entity_id"] for t in targets}
    shared_by_target = (
        set().union(*(all_entities(t) for t in targets)) if targets else set()
    )
    rows: list[dict[str, Any]] = []
    disabled = 0
    for it in inv.get("items", []):
        if it["entity_id"] in target_ids or not it.get("readable"):
            continue
        shared = sorted(shared_by_target & all_entities(it))
        if not shared:
            continue
        if it["disabled"]:
            disabled += 1
            continue
        via = it.get("via", {})
        acts = [e for e in shared if e in it["entities"] and _acts_on(it["config"], e)]
        acts_via = [
            f"{e} via {sc}"
            for e in shared
            for sc in via.get(e, [])
            if e not in it["entities"] and _acts_on(it["config"], sc)
        ]
        scenes = sorted({sc for e in shared for sc in via.get(e, [])})
        rows.append(
            {
                "item": it,
                "shared": shared,
                "acts": acts,
                "acts_via": acts_via,
                "scenes": scenes,
            }
        )
    rows.sort(
        key=lambda r: (
            -(len(r["acts"]) + len(r["acts_via"])),
            -len(r["shared"]),
            r["item"]["alias"].lower(),
        )
    )
    return rows[:MAX_RELATED], max(0, len(rows) - MAX_RELATED), disabled


def _entity_line(eid: str, info: dict[str, dict[str, str]]) -> str:
    i = info.get(eid, {})
    bits = [b for b in (i.get("name"), i.get("area"), f"state {i.get('state')}") if b]
    return f"- {eid}: " + ", ".join(bits)


def build_context(
    inv: dict[str, Any],
    question: str,
    picked: list[str] | None = None,
    intent: str = "",
) -> dict[str, Any]:
    """The text for the model plus size, cost and what was left out."""
    intent = intent if intent in ("lookup", "change", "create") else classify(question)
    targets = find_targets(inv, question, picked)
    info = inv.get("entity_info", {})
    shown, hidden, disabled = related(inv, targets)
    parts = [f"REQUEST ({intent}): {question.strip()}", ""]
    if not targets and intent != "create":
        parts.append("No matching automation was found.")
    for t in targets:
        parts += [
            f"## Target: {t['alias']} ({t['entity_id']}, {t['state']}, "
            f"last run {t.get('last_triggered') or 'never'})",
            compact_yaml(t["config"]),
            "",
        ]
    ent = sorted({e for t in targets for e in t["entities"]})
    if ent:
        parts.append("## Entities used by the target")
        parts += [_entity_line(e, info) for e in ent[:MAX_ENTITY_LINES]]
        if len(ent) > MAX_ENTITY_LINES:
            parts.append(f"... and {len(ent) - MAX_ENTITY_LINES} more")
        parts.append("")
    scene_ids = sorted({sc for t in targets for sc in t.get("scenes", [])})
    if scene_ids:
        parts.append("## Scenes the target activates (entities they set)")
        scenes = inv.get("scenes", {})
        for sc in scene_ids[:MAX_SCENES]:
            members = scenes.get(sc, {}).get("members", [])
            parts.append(
                f"- {sc} ({scenes.get(sc, {}).get('name', '')}): "
                + ", ".join(members[:6])
                + (f" and {len(members) - 6} more" if len(members) > 6 else "")
            )
        parts.append("")
    if shown:
        parts.append("## Other enabled automations using the same entities")
    for r in shown:
        it = r["item"]
        bits = []
        if r["acts"]:
            bits.append("also ACTS on " + ", ".join(r["acts"]))
        if r["acts_via"]:
            bits.append("also sets " + ", ".join(r["acts_via"][:4]))
        impact = "; ".join(bits) or "only reads " + ", ".join(r["shared"][:3])
        parts += [
            f"### {it['alias']} ({it['entity_id']}) - impact: {impact}",
            compact_yaml(it["config"], set(r["shared"]) | set(r["scenes"])),
            "",
        ]
    if hidden:
        parts.append(f"({hidden} more enabled automations use the same entities.)")
    if intent == "create":
        words = _words(question)
        known = [
            e
            for e, i in sorted(info.items())
            if any(w in e or w in i.get("name", "").lower() for w in words)
        ][:MAX_ENTITY_LINES]
        parts.append("## Entities that may fit the request (use only these)")
        parts += [_entity_line(e, info) for e in known] or ["(none found by name)"]
    body = "\n".join(parts).rstrip() + "\n"
    tokens = int((len(RULES) + len(body)) / CHARS_PER_TOKEN)
    return {
        "intent": intent,
        "text": body,
        "rules": RULES,
        "tokens": tokens,
        "over_cap": tokens > TOKEN_CAP,
        "targets": [
            {"entity_id": t["entity_id"], "alias": t["alias"]} for t in targets
        ],
        "related": [
            {"entity_id": r["item"]["entity_id"], "alias": r["item"]["alias"]}
            for r in shown
        ],
        "related_hidden": hidden,
        "disabled_left_out": disabled,
        "picked_disabled": [
            p
            for p in (picked or [])
            if any(it["entity_id"] == p and it["disabled"] for it in inv["items"])
        ],
    }
