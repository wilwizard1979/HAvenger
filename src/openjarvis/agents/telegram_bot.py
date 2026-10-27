"""Jarvis the Janitor's own Telegram bot (separate token, one allowed user).

* sends the digest, alerts (never inside quiet hours) and approval requests
* the ONLY place where an approval is accepted: an inline button pressed by the
  allowed user on a message this bot sent; the button carries the hash of the
  approved instruction
* long-polls ``getUpdates`` from a thread; no extra dependency (urllib)

The token comes from ``JARVIS_JANITOR_TELEGRAM_TOKEN`` or the Keychain item
``jarvis-janitor-telegram``. It is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from openjarvis.agents import janitor_executor as executor
from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store, get_store

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "jarvis-janitor-telegram"
TOKEN_ENV = "JARVIS_JANITOR_TELEGRAM_TOKEN"
POLL_TIMEOUT_S = 20
MAX_TEXT = 3800


class TelegramError(RuntimeError):
    pass


def resolve_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        try:
            out = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    os.environ.get("USER", ""),
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            token = out.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise TelegramError("no Janitor Telegram token in the Keychain") from exc
    if ":" not in token:
        raise TelegramError("the stored value is not a Telegram bot token")
    return token


class TelegramApi:
    """Bot API over HTTPS. Errors never contain the token."""

    def __init__(self, token_resolver: Callable[[], str] = resolve_token) -> None:
        self._resolve = token_resolver
        self._token: str | None = None

    def call(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
        if self._token is None:
            self._token = self._resolve()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.load(exc).get("description", "")
            except Exception:  # noqa: BLE001
                pass
            raise TelegramError(f"{method}: HTTP {exc.code} {detail}"[:200]) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'failed')}"[:200])
        return data["result"]


def _short(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 20] + "\n…[shortened]"


def approval_text(e: dict[str, Any], live: bool) -> str:
    ins = e["instruction"]
    lines = [
        f"Approval needed #{e['id']}: {e['label']}",
        f"Risk: {e['risk']} · Mode: "
        + ("LIVE, changes Home Assistant" if live else "SIMULATION (nothing is sent)"),
        "",
        e["summary"] or "",
        "",
    ]
    if ins.get("preconditions"):
        lines.append("Before: " + "; ".join(ins["preconditions"]))
    lines.append(f"Steps ({len(ins['steps'])}), run exactly in this order:")
    for i, st in enumerate(ins["steps"]):
        diff = (st.get("preview") or {}).get("diff")
        if diff:
            lines.append(f"{i}. EDIT {st['args'].get('identifier')}, exact changes:")
            lines += [f"   {d}" for d in diff[:40]]
        else:
            lines.append(f"{i}. {st['tool']} {json.dumps(st['args'])[:300]}")
        if st.get("purpose"):
            lines.append(f"   why: {st['purpose']}")
    if ins.get("rollback_steps"):
        lines.append(
            f"Rollback ({len(ins['rollback_steps'])} steps) runs if a step fails."
        )
    lines.append("")
    lines.append("Approve only if you read the steps. This approval is valid for 24 h.")
    return _short(Redactor.redact_secrets("\n".join(lines)))


class JanitorBot:
    def __init__(
        self,
        store: Store | None = None,
        api: TelegramApi | None = None,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._store = store
        self.api = api or TelegramApi()
        self._spawn = run_in_background or self._thread
        self.username: str | None = None
        self.last_poll: float | None = None
        self.last_error: str | None = None

    @property
    def store(self) -> Store:
        return self._store or get_store()

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="janitor-exec", daemon=True).start()

    # -- settings --
    def _cfg(self) -> dict[str, Any]:
        return self.store.settings()["telegram"]

    def _chat(self) -> int:
        return int(self._cfg()["allowed_user_id"])

    # -- sending --
    def send(self, text: str, *, markup: dict[str, Any] | None = None) -> int:
        params: dict[str, Any] = {
            "chat_id": self._chat(),
            "text": _short(text),
            "disable_web_page_preview": True,
        }
        if markup:
            params["reply_markup"] = markup
        return int(self.api.call("sendMessage", params)["message_id"])

    def send_unsolicited(self, text: str, now: float | None = None) -> bool:
        """Digest / alerts: never during quiet hours, critical ones included."""
        from openjarvis.agents.watchdog import is_quiet_hours

        now = now if now is not None else time.time()
        if is_quiet_hours(now, self.store.settings()):
            return False
        self.send(text)
        return True

    def send_digest(self) -> dict[str, Any]:
        report = self.store.latest_report()
        if not report:
            return {"status": "no_report"}
        readout = self.store.latest_readout()
        text = report["summary_text"]
        if readout and readout["guard_ok"]:
            text += "\n\n" + readout["text"]
        self.send(text)
        return {"status": "sent", "report_id": report["id"]}

    def request_approval(self, exec_id: int) -> dict[str, Any]:
        e = self.store.execution(exec_id)
        if e is None or e["status"] != "awaiting_approval":
            return {"status": "not_waiting"}
        h = e["instruction_hash"][:8]
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"ap:{exec_id}:{h}"},
                    {"text": "Reject", "callback_data": f"rj:{exec_id}:{h}"},
                ]
            ]
        }
        mid = self.send(approval_text(e, bool(e["live"])), markup=markup)
        self.store.update_execution(exec_id, telegram_message_id=str(mid))
        self.store.log_execution(
            exec_id, "sent", f"approval request sent (message {mid})"
        )
        return {"status": "sent", "message_id": mid}

    # -- receiving --
    def poll_once(self, timeout: int = POLL_TIMEOUT_S) -> int:
        offset = int(self.store.facts().get("tg_offset", 0))
        updates = self.api.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 10,
        )
        self.last_poll = time.time()
        for u in updates:
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                logger.warning("Janitor bot update failed", exc_info=True)
            offset = max(offset, int(u["update_id"]) + 1)
        if updates:
            self.store.set_facts({"tg_offset": offset})
        return len(updates)

    def handle(self, update: dict[str, Any]) -> None:
        allowed = self._chat()
        if "callback_query" in update:
            cq = update["callback_query"]
            if int(cq.get("from", {}).get("id", 0)) != allowed:
                return  # not the owner: ignore silently
            self._on_button(cq)
            return
        msg = update.get("message") or {}
        if int(msg.get("from", {}).get("id", 0)) != allowed:
            return
        text = (msg.get("text") or "").strip().lower().split("@")[0]
        if text in ("/start", "/help"):
            self.send(
                "Jarvis the Janitor. Commands: /status (latest digest), "
                "/plan (top tasks), /help. Approvals arrive here as buttons; "
                "nothing else can approve a change."
            )
        elif text == "/status":
            self.send_digest()
        elif text == "/plan":
            self.send(self._plan_text())
        # anything else is ignored on purpose

    def _plan_text(self) -> str:
        from openjarvis.agents.planning import plan_clusters

        tasks = plan_clusters(self.store)[:8]
        if not tasks:
            return "No open tasks."
        return "Top tasks (quick wins first):\n" + "\n".join(
            f"- {t['label']} [{t['severity']}, {t['effort']}, {t['count']} issues]"
            for t in tasks
        )

    def _on_button(self, cq: dict[str, Any]) -> None:
        data = str(cq.get("data", ""))
        parts = data.split(":")
        if len(parts) != 3 or parts[0] not in ("ap", "rj") or not parts[1].isdigit():
            self.api.call(
                "answerCallbackQuery",
                {"callback_query_id": cq["id"], "text": "Unknown button"},
            )
            return
        exec_id, approve = int(parts[1]), parts[0] == "ap"
        out = executor.decide(self.store, exec_id, parts[2], approve)
        note = {
            "approved": "Approved. Running now.",
            "rejected": "Rejected.",
            "expired": "This approval expired.",
            "hash_mismatch": "This request no longer matches. Nothing was done.",
            "not_waiting": f"Already {out.get('current', 'handled')}.",
            "unknown": "Unknown request.",
        }.get(out["status"], out["status"])
        self.api.call(
            "answerCallbackQuery", {"callback_query_id": cq["id"], "text": note}
        )
        msg = cq.get("message") or {}
        if msg.get("message_id"):
            try:
                self.api.call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": self._chat(),
                        "message_id": msg["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            except TelegramError:
                pass
        if out["status"] == "approved":
            self._spawn(lambda: self._run_and_report(exec_id))
        elif out["status"] in ("rejected", "expired"):
            self.send(f"#{exec_id}: {note}")

    def _run_and_report(self, exec_id: int) -> None:
        out = executor.run_execution(self.store, exec_id)
        e = self.store.execution(exec_id) or {}
        head = f"#{exec_id} {e.get('label', '')}: "
        words = {
            "done": "complete.",
            "simulated": "simulation complete (nothing was sent).",
        }.get(out["status"], f"{out['status'].upper()}.")
        try:
            self.send(
                head
                + words
                + "\n"
                + Redactor.redact_secrets(str(out.get("detail", "")))
            )
        except TelegramError as exc:
            logger.warning("Could not report execution %s: %s", exec_id, exc)

    # -- identity --
    def check(self) -> dict[str, Any]:
        try:
            me = self.api.call("getMe", {}, timeout=15)
        except TelegramError as exc:
            self.last_error = str(exc)
            return {"configured": False, "error": str(exc)}
        self.username = me.get("username")
        self.last_error = None
        return {"configured": True, "username": self.username}


class BotService:
    """Runs ``poll_once`` in a loop on a worker thread; started with the server."""

    def __init__(self, bot: JanitorBot | None = None) -> None:
        self.bot = bot or JanitorBot()
        self._task: asyncio.Task[None] | None = None
        self.running = False

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, self.bot.check)
        if not info.get("configured"):
            logger.warning("Janitor Telegram bot not started: %s", info.get("error"))
            return
        self.running = True
        logger.info("Janitor Telegram bot @%s polling", self.bot.username)
        while True:
            if not self.bot._cfg()["enabled"]:
                await asyncio.sleep(30)
                continue
            try:
                await loop.run_in_executor(None, self.bot.poll_once)
                self.bot.last_error = None
            except TelegramError as exc:
                self.bot.last_error = str(exc)
                await asyncio.sleep(15)

    def start(self) -> bool:
        if os.environ.get("JARVIS_TELEGRAM", "1").strip().lower() in (
            "0",
            "false",
            "off",
        ):
            return False
        if self._task and not self._task.done():
            return True
        self._task = asyncio.get_running_loop().create_task(self._loop())
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        self.running = False
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def status(self) -> dict[str, Any]:
        cfg = self.bot._cfg()
        return {
            "enabled": cfg["enabled"],
            "running": self.running,
            "username": self.bot.username,
            "allowed_user_id": cfg["allowed_user_id"],
            "last_poll": self.bot.last_poll,
            "last_error": self.bot.last_error,
        }


_service: BotService | None = None


def get_bot_service() -> BotService:
    global _service
    if _service is None:
        _service = BotService()
    return _service


def reset_bot_for_tests() -> None:
    global _service
    _service = None


__all__ = ["JanitorBot", "BotService", "TelegramApi", "get_bot_service", "urllib"]
