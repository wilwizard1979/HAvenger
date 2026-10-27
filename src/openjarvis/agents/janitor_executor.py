"""Approval and execution of a Sonnet proposal. Nothing runs without Telegram.

Flow: ``request_approval`` snapshots the proposal's instruction (hash) and creates
an ``awaiting_approval`` execution; ONLY the Telegram bot calls ``decide``; after an
approval ``run_execution`` executes exactly the approved steps.

Safety rules, all enforced in Python (never by a model):

* the approved instruction is a frozen snapshot; a later edit cannot change it
* every step must use an allow-listed tool and a name not matching the deny regex
* ``executor.live`` is off by default: the run is a simulation that logs what it
  would do
* live runs need a recent backup (``require_backup``)
* Haiku is the operator, but it can only call ``run_step(index)`` for the next
  approved step, ``rollback`` or ``finish``. It never supplies a tool or arguments;
  Python takes those from the approved snapshot. Out-of-order calls are refused and
  three refusals abort the run and roll back.
* a failed step stops the run; the approved rollback steps run (also literally)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Callable

from openjarvis.agents import config_edit
from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudUnavailable,
)
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store

logger = logging.getLogger(__name__)

APPROVAL_TTL_S = 24 * 3600
MAX_STRIKES = 3
RESULT_CHARS = 1500
ACTIVE = ("awaiting_approval", "approved", "running")

OPERATOR_SYSTEM = (
    "You are the operator of an approved Home Assistant change. The steps are fixed "
    "and numbered from 0. You cannot choose tools or arguments: call run_step with "
    "the index of the NEXT step, in order, and read its result. If a step fails or "
    "its result shows the change did not work, call rollback once, then finish with "
    "ok=false. When every step worked, call finish with ok=true and a report of at "
    "most five short lines (what was done, what was checked, anything odd). Never "
    "repeat a step. Placeholders like <email_1> are redacted values: keep them."
)
OPERATOR_TOOLS = [
    {
        "name": "run_step",
        "description": "Run the next approved step.",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    },
    {
        "name": "rollback",
        "description": "Run the approved rollback steps.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "Finish the run with a short report.",
        "input_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "report": {"type": "string"}},
            "required": ["ok", "report"],
        },
    },
]


# -- validation -------------------------------------------------------------------
def instruction_hash(instruction: dict[str, Any]) -> str:
    raw = json.dumps(instruction, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def step_problems(step: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    out: list[str] = []
    tool = step.get("tool", "")
    if tool != config_edit.EDIT_TOOL and tool not in cfg["allowed_tools"]:
        out.append(f"tool {tool} is not allowed")
    args = step.get("args") or {}
    if tool == config_edit.EDIT_TOOL:  # a Python-handled step, see config_edit
        return config_edit.edit_problems(step, cfg["allowed_tools"])
    inner = args.get("name") if tool == "ha_call_write_tool" else None
    if tool == "ha_call_write_tool":
        if not isinstance(inner, str) or not inner:
            out.append("ha_call_write_tool needs args.name")
        elif re.search(cfg["deny_names_regex"], inner, re.IGNORECASE):
            out.append(f"{inner} matches the deny list")
        if not isinstance(args.get("arguments", {}), dict):
            out.append("args.arguments must be an object")
    return out


def execution_problems(instruction: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    steps = instruction.get("steps") or []
    if not steps:
        return ["the proposal has no executable steps"]
    problems: list[str] = []
    for kind in ("steps", "rollback_steps"):
        for i, st in enumerate(instruction.get(kind) or []):
            problems += [f"{kind}[{i}]: {p}" for p in step_problems(st, cfg)]
    return problems


def _read_only(tool: str, args: dict[str, Any]) -> str:
    from openjarvis.agents.escalation import ReadOnlyHA

    ha = ReadOnlyHA()
    try:
        return ha.call(tool, args)
    finally:
        ha.close()


# -- approval --------------------------------------------------------------------
def request_approval(
    store: Store,
    cluster_id: str,
    read: Callable[[str, dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    cfg = store.settings()["executor"]
    prop = store.plan_proposals().get(cluster_id)
    if prop is None:
        return {"status": "no_proposal"}
    from openjarvis.agents.planning import plan_clusters

    cluster = next((c for c in plan_clusters(store) if c["id"] == cluster_id), None)
    if cluster is None or cluster["members_hash"] != prop["members_hash"]:
        return {"status": "stale_proposal"}
    instruction = (prop["body"] or {}).get("instruction") or {}
    problems = execution_problems(instruction, cfg)
    if problems:
        return {"status": "not_executable", "problems": problems}
    if any(st.get("tool") == config_edit.EDIT_TOOL for st in instruction["steps"]):
        # read the current configs (read-only), build the diff and the restore steps
        try:
            instruction = config_edit.prepare(instruction, read or _read_only)
        except config_edit.EditError as exc:
            return {"status": "not_executable", "problems": [str(exc)[:300]]}
        problems = execution_problems(instruction, cfg)
        if problems:
            return {"status": "not_executable", "problems": problems}
    if any(
        e["cluster_id"] == cluster_id and e["status"] in ACTIVE
        for e in store.executions(50)
    ):
        return {"status": "already_active"}
    exec_id = store.create_execution(
        cluster_id,
        label=(f"{tid} · " if (tid := store.open_task_id(cluster_id)) else "")
        + cluster["label"],
        members_hash=cluster["members_hash"],
        instruction=instruction,
        instruction_hash=instruction_hash(instruction),
        risk=prop["body"].get("risk", "medium"),
        summary=prop["body"].get("summary", "")
        + (
            "\n\nBased on your findings: " + prop["body"]["findings_used"][:400]
            if prop["body"].get("findings_used")
            else ""
        ),
        live=bool(cfg["live"]),
    )
    store.log_execution(exec_id, "requested", "waiting for your approval in Telegram")
    return {"status": "awaiting_approval", "id": exec_id}


def cancel(store: Store, exec_id: int) -> dict[str, Any]:
    e = store.execution(exec_id)
    if e is None:
        return {"status": "unknown"}
    if e["status"] != "awaiting_approval":
        return {"status": "not_cancellable"}
    store.update_execution(exec_id, status="cancelled", finished_at=time.time())
    store.log_execution(exec_id, "cancelled", "cancelled in the UI")
    return {"status": "cancelled"}


def decide(
    store: Store, exec_id: int, hash8: str, approve: bool, *, now: float | None = None
) -> dict[str, Any]:
    """Called by the Telegram bot only (there is no HTTP route for this)."""
    now = now if now is not None else time.time()
    e = store.execution(exec_id)
    if e is None:
        return {"status": "unknown"}
    if e["status"] != "awaiting_approval":
        return {"status": "not_waiting", "current": e["status"]}
    if not e["instruction_hash"].startswith(hash8):
        return {"status": "hash_mismatch"}
    if now - e["created_at"] > APPROVAL_TTL_S:
        store.update_execution(exec_id, status="expired", finished_at=now)
        store.log_execution(exec_id, "expired", "approval window (24 h) passed")
        return {"status": "expired"}
    if not approve:
        store.update_execution(exec_id, status="rejected", finished_at=now)
        store.log_execution(exec_id, "rejected", "rejected in Telegram")
        return {"status": "rejected"}
    store.update_execution(exec_id, status="approved", approved_at=now)
    store.log_execution(exec_id, "approved", "approved in Telegram")
    return {"status": "approved"}


# -- running -------------------------------------------------------------------------
class WriteHA:
    """Calls the approved tool on ha-mcp. The write gate of ha-mcp (its own
    Telegram approval, up to 10 minutes) stays in place on top of ours."""

    def __init__(self, timeout_s: float = 660) -> None:
        self._timeout = timeout_s
        self._client: Any = None

    def _open(self) -> Any:
        if self._client is None:
            from openjarvis.mcp.client import MCPClient
            from openjarvis.mcp.transport import StreamableHTTPTransport
            from openjarvis.server.ha_dashboard_routes import _load_server

            srv = _load_server()
            self._client = MCPClient(
                StreamableHTTPTransport(
                    url=srv["url"],
                    token=srv.get("token"),
                    request_timeout=self._timeout,
                )
            )
            self._client.initialize()
        return self._client

    def call(self, tool: str, args: dict[str, Any]) -> str:
        raw = self._open().call_tool(tool, args)
        text = "".join(c.get("text", "") for c in raw.get("content", []))
        if raw.get("isError"):
            raise RuntimeError(text[:300] or "tool reported an error")
        return text

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass


class Runner:
    """Holds the approved steps and enforces order. The only thing that touches HA."""

    def __init__(
        self,
        store: Store,
        exec_id: int,
        instruction: dict[str, Any],
        ha: Any,
        live: bool,
    ) -> None:
        self.store, self.id, self.ha, self.live = store, exec_id, ha, live
        self.steps = instruction["steps"]
        self.rollback_steps = instruction.get("rollback_steps") or []
        self.next = 0
        self.strikes = 0
        self.failed = False
        self.rolled_back = False

    def _do(self, label: str, step: dict[str, Any]) -> tuple[bool, str]:
        shown = Redactor.redact_secrets(f"{step['tool']} {json.dumps(step['args'])}")
        if step["tool"] == config_edit.EDIT_TOOL:
            return self._do_edit(label, step, shown)
        if not self.live:
            self.store.log_execution(self.id, f"simulated {label}", shown)
            return True, "simulated: nothing was sent to Home Assistant"
        self.store.log_execution(self.id, f"running {label}", shown)
        try:
            out = self.ha.call(step["tool"], step["args"])
        except Exception as exc:  # noqa: BLE001
            msg = Redactor.redact_secrets(str(exc))[:300]
            self.store.log_execution(self.id, f"failed {label}", msg)
            return False, f"error: {msg}"
        out = Redactor.redact_secrets(out)[:RESULT_CHARS]
        self.store.log_execution(self.id, f"done {label}", out[:300])
        return True, out

    def _do_edit(
        self, label: str, step: dict[str, Any], shown: str
    ) -> tuple[bool, str]:
        diff = "\n".join((step.get("preview") or {}).get("diff") or [])
        if not self.live:
            self.store.log_execution(
                self.id, f"simulated {label}", Redactor.redact_secrets(diff or shown)
            )
            return True, "simulated: nothing was sent to Home Assistant"
        self.store.log_execution(self.id, f"running {label}", shown[:300])
        try:
            out = config_edit.run_live(step, self.ha.call)
        except Exception as exc:  # noqa: BLE001
            msg = Redactor.redact_secrets(str(exc))[:300]
            self.store.log_execution(self.id, f"failed {label}", msg)
            return False, f"error: {msg}"
        self.store.log_execution(self.id, f"done {label}", out[:300])
        return True, out

    def run_step(self, index: Any) -> str:
        if self.failed:
            return "refused: a step failed; call rollback or finish"
        if index != self.next or not isinstance(index, int):
            self.strikes += 1
            self.store.log_execution(
                self.id, "refused", f"asked for step {index!r}, next is {self.next}"
            )
            return f"refused: the next step is {self.next}"
        if self.next >= len(self.steps):
            return "refused: all steps are done; call finish"
        ok, out = self._do(f"step {index}", self.steps[index])
        if ok:
            self.next += 1
        else:
            self.failed = True
        return out

    def rollback(self) -> str:
        if self.rolled_back:
            return "refused: rollback already ran"
        self.rolled_back = True
        if not self.rollback_steps:
            self.store.log_execution(self.id, "rollback", "no rollback steps approved")
            return "no rollback steps were approved"
        results = []
        for i, st in enumerate(self.rollback_steps):
            ok, out = self._do(f"rollback {i}", st)
            results.append(out[:200])
            if not ok:
                break
        return " | ".join(results)

    @property
    def complete(self) -> bool:
        return not self.failed and self.next >= len(self.steps)


def backup_ok(store: Store, now: float) -> tuple[bool, str]:
    from openjarvis.agents.watchdog import parse_ts

    last = parse_ts(store.facts().get("backup_last_success"))
    limit = store.settings()["thresholds"]["backup_age_hours"]
    if last is None:
        return False, "the time of the last successful backup is unknown"
    age = (now - last) / 3600
    if age > limit:
        return (
            False,
            f"the last successful backup is {age:.0f} h old (limit {limit:g} h)",
        )
    return True, ""


def _finish(store: Store, exec_id: int, status: str, why: str) -> dict[str, Any]:
    store.update_execution(exec_id, status=status, finished_at=time.time())
    store.log_execution(exec_id, status, why)
    return {"status": status, "detail": why}


def run_execution(
    store: Store,
    exec_id: int,
    *,
    client: CloudClient | None = None,
    ha_factory: Callable[[float], Any] = WriteHA,
    now: float | None = None,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    e = store.execution(exec_id)
    if e is None or e["status"] != "approved":
        return {"status": "not_approved"}
    cfg = store.settings()["executor"]
    instruction = e["instruction"]
    # the snapshot must still be what was approved, and still be allowed
    if instruction_hash(instruction) != e["instruction_hash"]:
        return _finish(store, exec_id, "failed", "the approved instruction changed")
    problems = execution_problems(instruction, cfg)
    if problems:
        return _finish(store, exec_id, "failed", "; ".join(problems)[:300])

    live = bool(cfg["live"])
    store.update_execution(exec_id, status="running", started_at=now, live=int(live))
    store.log_execution(exec_id, "start", "LIVE" if live else "SIMULATION")
    if live and cfg["require_backup"]:
        ok, why = backup_ok(store, now)
        if not ok:
            return _finish(store, exec_id, "failed", f"no safe backup: {why}")

    ha = ha_factory(float(cfg["step_timeout_s"])) if live else None
    runner = Runner(store, exec_id, instruction, ha, live)
    try:
        if cfg["operator"] == "haiku" and live:
            report = _operate_with_haiku(
                store, exec_id, runner, client or CloudClient(store)
            )
        else:
            report = _operate_in_python(runner)
    except (BudgetExceeded, CloudUnavailable) as exc:
        if runner.next and not runner.complete:
            runner.rollback()
        return _finish(store, exec_id, "failed", f"operator unavailable: {exc}"[:300])
    finally:
        if ha is not None:
            ha.close()

    if runner.complete and not runner.rolled_back:
        status = "done" if live else "simulated"
    elif runner.rolled_back:
        status = "rolled_back"
    else:
        status = "failed"
    return _finish(store, exec_id, status, report)


def _operate_in_python(runner: Runner) -> str:
    for i in range(len(runner.steps)):
        out = runner.run_step(i)
        if runner.failed:
            runner.rollback()
            return f"step {i} failed: {out[:200]}"
    return f"all {len(runner.steps)} steps {'run' if runner.live else 'simulated'}"


def _operate_with_haiku(
    store: Store, exec_id: int, runner: Runner, client: CloudClient
) -> str:
    plan = [
        {"index": i, "purpose": s.get("purpose", ""), "verify": s.get("verify", "")}
        for i, s in enumerate(runner.steps)
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps({"steps": plan})}
    ]
    report = ""
    for _ in range(len(runner.steps) * 2 + 4):
        res = client.complete(
            "triage",
            system=OPERATOR_SYSTEM,
            messages=messages,
            max_tokens=500,
            tools=OPERATOR_TOOLS,
            manual=True,
            purpose=f"execute:{exec_id}",
        )
        if res.dry_run:
            raise CloudUnavailable("cloud dry-run is on: Haiku cannot operate")
        uses = [b for b in res.blocks if b["type"] == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": res.raw_blocks})
        results, done = [], False
        for u in uses:
            name, inp = u["name"], u["input"] or {}
            if name == "run_step":
                out = runner.run_step(inp.get("index"))
            elif name == "rollback":
                out = runner.rollback()
            elif name == "finish":
                report = str(inp.get("report", ""))[:600]
                done = True
                out = "ok"
            else:
                out = "refused: unknown tool"
                runner.strikes += 1
            results.append(
                {"type": "tool_result", "tool_use_id": u["id"], "content": out}
            )
        messages.append({"role": "user", "content": results})
        if runner.strikes >= MAX_STRIKES:
            store.log_execution(exec_id, "abort", "too many refused calls")
            if runner.next and not runner.rolled_back:
                runner.rollback()
            return "aborted: the operator kept asking for steps out of order"
        if done:
            break
    if runner.failed and not runner.rolled_back:
        runner.rollback()  # Python never leaves a failed run half-way
    if not report:
        report = f"{runner.next} of {len(runner.steps)} steps done" + (
            " (rolled back)" if runner.rolled_back else ""
        )
    return report
