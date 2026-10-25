"""The only module that talks to the Anthropic API for Jarvis the Janitor.

Nothing else imports ``anthropic``. Every call goes through ``CloudClient.complete``:

1. redact the whole request and ``verify`` it is clean (``RedactionError`` otherwise)
2. budget guard (monthly limit, pause automatic escalation at 75%, hard stop at 100%)
3. dry-run: record the call and return the redacted request, no key lookup, no network
4. live: prompt caching on the system prompt and tools, then cost into the ledger

The API key is never taken from ``ANTHROPIC_API_KEY`` (that one belongs to the GUI).
It comes from ``JARVIS_JANITOR_ANTHROPIC_API_KEY`` or the macOS Keychain item
``jarvis-the-janitor``.
"""

from __future__ import annotations

import copy
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openjarvis.agents.redaction import Redactor
from openjarvis.agents.store import Store, get_store

KEYCHAIN_SERVICE = "jarvis-the-janitor"
KEY_ENV = "JARVIS_JANITOR_ANTHROPIC_API_KEY"
TIERS = ("triage", "diagnose", "deep")
_TIER_ENV = {
    "triage": "JARVIS_TRIAGE_MODEL",
    "diagnose": "JARVIS_DIAG_MODEL",
    "deep": "JARVIS_DEEP_MODEL",
}
_AUTO_PAUSED_TIERS = ("diagnose", "deep")
_CHARS_PER_TOKEN = 3.5


class BudgetExceeded(RuntimeError):
    """The monthly budget guard refused a cloud call."""


class CloudUnavailable(RuntimeError):
    """No key, no price for the model, or the SDK is missing."""


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str
    spend: float
    limit: float
    pct: float

    @property
    def state(self) -> str:
        return "ok" if self.allowed else self.reason


@dataclass
class CloudResult:
    text: str
    blocks: list[dict[str, Any]]
    model: str
    tier: str
    usage: dict[str, int]
    cost_usd: float
    dry_run: bool
    stop_reason: str | None = None
    request: dict[str, Any] | None = field(default=None, repr=False)
    # every block as the API returned it (thinking blocks included, text and tool
    # inputs restored): send these back unchanged as the assistant turn of a loop
    raw_blocks: list[dict[str, Any]] = field(default_factory=list, repr=False)


def default_thinking(model: str) -> tuple[dict[str, Any] | None, str | None]:
    """(thinking, effort) that keep thinking from eating the output budget.

    Haiku 5.5 may switch thinking off; Sonnet 5.5 rejects ``disabled`` and uses
    ``between_tools`` instead; Opus 5.5 cannot switch it off, so its effort is
    set to ``medium``. Other models keep the API defaults.
    """
    if model.startswith("claude-haiku-5"):
        return {"type": "disabled"}, None
    if model.startswith("claude-sonnet-5-5"):
        return {"type": "between_tools"}, None
    if model.startswith("claude-opus-5-5"):
        return None, "medium"
    return None, None


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def cost_usd(prices: dict[str, Any], usage: dict[str, int]) -> float:
    """USD for one call. ``usage`` uses Anthropic's field names."""
    prompt = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    row = prices
    if prices.get("long_over") and prompt > prices["long_over"]:
        row = {**prices, **prices.get("long", {})}
    return (
        usage.get("input_tokens", 0) * row["input"]
        + usage.get("cache_creation_input_tokens", 0) * row["cache_write"]
        + usage.get("cache_read_input_tokens", 0) * row["cache_read"]
        + usage.get("output_tokens", 0) * row["output"]
    ) / 1_000_000


def _read_keychain() -> str:
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
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloudUnavailable(
            f"No Janitor API key: Keychain item '{KEYCHAIN_SERVICE}' not readable"
        ) from exc
    return out.stdout.strip()


def resolve_api_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip() or _read_keychain()
    if not key.startswith("sk-ant-"):
        # A Workspace ID ("wrkspc_...") or other id was stored instead of a key.
        raise CloudUnavailable(
            "The stored Janitor key is not an Anthropic API key "
            "(it should start with 'sk-ant-'). "
            "Create a key in the Console and store it again."
        )
    return key


class CloudClient:
    def __init__(
        self,
        store: Store | None = None,
        *,
        sdk_client: Any | None = None,
        key_resolver: Callable[[], str] = resolve_api_key,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or get_store()
        self._sdk = sdk_client
        self._key_resolver = key_resolver
        self._clock = clock

    # -- configuration ----------------------------------------------------
    def model_for(self, tier: str) -> str:
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        return os.environ.get(_TIER_ENV[tier]) or self.store.settings()["models"][tier]

    def is_dry_run(self) -> bool:
        env = os.environ.get("JARVIS_JANITOR_DRY_RUN")
        return (
            _truthy(env) if env is not None else bool(self.store.settings()["dry_run"])
        )

    # -- budget -----------------------------------------------------------
    def budget_decision(
        self, tier: str, *, manual: bool = False, extra_cost: float = 0.0
    ) -> BudgetDecision:
        s = self.store.settings()["budget"]
        limit = float(s["monthly_limit_usd"])
        spend = self.store.month_spend(self._clock())
        pct = (spend / limit * 100) if limit > 0 else 100.0
        if limit <= 0 or pct >= s["hard_stop_pct"]:
            return BudgetDecision(False, "hard_stop", spend, limit, pct)
        if spend + extra_cost > limit:
            return BudgetDecision(False, "would_exceed_limit", spend, limit, pct)
        if (
            pct >= s["pause_auto_escalation_pct"]
            and tier in _AUTO_PAUSED_TIERS
            and not manual
        ):
            return BudgetDecision(False, "auto_escalation_paused", spend, limit, pct)
        return BudgetDecision(True, "ok", spend, limit, pct)

    def estimate_cost(self, tier: str, input_chars: int, max_tokens: int) -> float:
        """Worst-case cost (all input uncached, full output) for a button label."""
        model = self.model_for(tier)
        row = self.store.settings()["prices"].get(model)
        if not row:
            return 0.0
        return cost_usd(
            row,
            {
                "input_tokens": int(input_chars / _CHARS_PER_TOKEN),
                "output_tokens": max_tokens,
            },
        )

    # -- the call ---------------------------------------------------------
    def complete(
        self,
        tier: str,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        manual: bool = False,
        purpose: str = "",
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
    ) -> CloudResult:
        model = self.model_for(tier)
        settings = self.store.settings()
        redactor = Redactor(settings["extra_redaction_terms"])

        safe_messages = redactor.redact_obj(copy.deepcopy(messages))
        safe_system = redactor.redact(system)
        redactor.verify(
            {"system": safe_system, "messages": safe_messages}
        )  # tools are code-defined

        est = self.estimate_cost(
            tier, len(safe_system) + len(str(safe_messages)), max_tokens
        )
        decision = self.budget_decision(tier, manual=manual, extra_cost=est)
        if not decision.allowed:
            self.store.record_run(
                tier,
                model,
                outcome=f"blocked:{decision.reason}",
                purpose=purpose,
                dry_run=int(self.is_dry_run()),
                cost_usd=0.0,
            )
            raise BudgetExceeded(
                f"{decision.reason}: spent ${decision.spend:.2f} of "
                f"${decision.limit:.2f} this month"
            )

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": safe_system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": safe_messages,
        }
        # Claude 5 models think by default, and thinking tokens count against
        # max_tokens: callers pick the thinking mode and effort per job.
        d_thinking, d_effort = default_thinking(model)
        thinking = thinking if thinking is not None else d_thinking
        effort = effort if effort is not None else d_effort
        if thinking is not None:
            request["thinking"] = thinking
        if effort is not None:
            request["output_config"] = {"effort": effort}
        if tools:
            cached_tools = copy.deepcopy(tools)
            cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
            request["tools"] = cached_tools

        if self.is_dry_run():
            self.store.record_run(
                tier, model, outcome="dry_run", purpose=purpose, dry_run=1, cost_usd=0.0
            )
            return CloudResult("", [], model, tier, {}, 0.0, True, request=request)

        prices = settings["prices"].get(model)
        if not prices:
            raise CloudUnavailable(
                f"No verified price for {model}; add it under settings.prices first"
            )
        started = self._clock()
        resp = self._client().messages.create(**request)
        usage = {
            "input_tokens": int(getattr(resp.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(resp.usage, "output_tokens", 0) or 0),
            "cache_read_input_tokens": int(
                getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            ),
            "cache_creation_input_tokens": int(
                getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            ),
        }
        cost = cost_usd(prices, usage)
        self.store.add_usage(
            model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_input_tokens"],
            cache_write_tokens=usage["cache_creation_input_tokens"],
            cost_usd=cost,
            ts=self._clock(),
        )
        self.store.record_run(
            tier,
            model,
            started=started,
            outcome="ok",
            purpose=purpose,
            dry_run=0,
            cost_usd=cost,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_input_tokens"],
            cache_write_tokens=usage["cache_creation_input_tokens"],
        )

        blocks: list[dict[str, Any]] = []
        raw: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for b in resp.content:
            kind = getattr(b, "type", "")
            dumped = (
                b.model_dump(exclude_none=True)
                if hasattr(b, "model_dump")
                else {"type": kind}
            )
            if kind == "text":
                t = redactor.restore(b.text)
                text_parts.append(t)
                blocks.append({"type": "text", "text": t})
                raw.append({**dumped, "text": t})
            elif kind == "tool_use":
                use = {
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": redactor.restore_obj(b.input),
                }
                blocks.append(use)
                raw.append(use)
            else:  # thinking / redacted_thinking: keep exactly as returned
                raw.append(dumped)
        return CloudResult(
            "".join(text_parts),
            blocks,
            model,
            tier,
            usage,
            cost,
            False,
            stop_reason=getattr(resp, "stop_reason", None),
            raw_blocks=raw,
        )

    def _client(self) -> Any:
        if self._sdk is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise CloudUnavailable(
                    "The 'anthropic' package is not installed"
                ) from exc
            self._sdk = anthropic.Anthropic(api_key=self._key_resolver())
        return self._sdk
