"""Redaction for everything that leaves this Mac (cloud calls, files saved to Drive).

Pure Python on purpose: the repo's ``SecretScanner`` needs the Rust extension,
which is not built here. Rules, applied in order:

1. strip ANSI colour codes
2. secrets: ``password: x`` / ``"token": "x"`` / ``!secret name`` / ``Bearer x``
3. URLs: ``user:pass@`` credentials, ``?token=`` style query secrets, the
   private ha-mcp path, webhook ids
4. well-known key formats (``sk-ant-...``, GitHub, AWS, Slack, Stripe, ...)
5. personal data: e-mail addresses, MAC addresses, GPS coordinates,
   ``person.<name>`` entity ids and any configured extra terms (family names,
   street, town) -> stable placeholders that ``restore`` maps back locally
6. truncation

The pass is idempotent: running it on already redacted text changes nothing.
``verify`` relies on that as a last check before a payload is sent.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MAX_CHARS = 20_000

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_KEYWORDS = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|authorization|credential|client[_-]?secret|webhook[_-]?id"
)
_KEY = rf"[\w.-]*(?:{_KEYWORDS})[\w.-]*"

# key: "value"  /  "key": 'value'  /  key = "value"
_KV_QUOTED = re.compile(rf"""(?i)(\b{_KEY}["']?\s*[:=]\s*)(["'])(.*?)\2""")
# key: value (rest of the line, YAML / env style); skips values already redacted
_KV_BARE = re.compile(
    # The look-ahead sits right after [ \t]*, so backtracking cannot slip past an
    # already redacted "<redacted>" value (keeps the pass idempotent).
    rf"(?im)(\b{_KEY}[\"']?[ \t]*[:=][ \t]*)(?=[^\s\"'<\[{{])([^\n#]*[^\s#])"
)
_BANG_SECRET = re.compile(r"(!secret\s+)\S+")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")

_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_URL_QUERY = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|key|secret|password|auth|signature|sig)=)[^&\s\"']+"
)
_HA_MCP_PATH = re.compile(r"(?i)(https?://[^\s/\"']+:9584/)[^\s\"']+")
_WEBHOOK = re.compile(r"(?i)(/api/webhook/)[A-Za-z0-9_-]+")

# Reuse the repo's well-known formats when importable; fall back to a short list.
_FALLBACK_PATTERNS = {
    "anthropic_key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai_key": r"sk-[A-Za-z0-9_-]{20,}",
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "github_token": r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{36,}",
    "slack_token": r"xox[bpors]-[A-Za-z0-9\-]{10,}",
    "stripe_key": r"(?:sk|pk)_(?:test|live)_[A-Za-z0-9]{20,}",
    "private_key": r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)",  # noqa: E501
}


def _known_patterns() -> dict[str, re.Pattern[str]]:
    patterns = dict(_FALLBACK_PATTERNS)
    try:
        from openjarvis.security.scanner import SecretScanner

        for name, (rx, _lvl, _desc) in SecretScanner.PATTERNS.items():
            if name in ("password_assignment", "generic_api_key"):
                continue  # handled, more completely, by the key/value rules
            patterns.setdefault(name, rx)
    except Exception:  # noqa: BLE001 - scanner module needs optional extras
        pass
    return {name: re.compile(rx) for name, rx in patterns.items()}


_KNOWN = _known_patterns()

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_COORD_KV = re.compile(
    r"(?i)\b(latitude|longitude|lat|lon|lng)(\b[\"']?\s*[:=]\s*)-?\d{1,3}\.\d+"
)
_COORD_PAIR = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")
_PERSON = re.compile(r"\bperson\.([a-z0-9_]+)\b")

# Keys whose whole value must never be sent, whatever it looks like.
DROP_KEYS = frozenset({"translation_placeholders", "placeholders"})
_SECRET_KEY = re.compile(rf"(?i)^{_KEY}$")


def split_terms(items: Iterable[str]) -> list[str]:
    """One term per entry; a comma inside an entry separates terms; no duplicates."""
    out: list[str] = []
    for item in items:
        for part in str(item or "").split(","):
            part = part.strip()
            if part and part.lower() not in (o.lower() for o in out):
                out.append(part)
    return out


class RedactionError(RuntimeError):
    """A payload still contains something the redaction rules would remove."""


class Redactor:
    """One redaction session: placeholders stay stable across calls."""

    def __init__(
        self, extra_terms: Iterable[str] = (), max_chars: int = MAX_CHARS
    ) -> None:
        self._max = max_chars
        self._terms = split_terms(extra_terms)
        self._maps: dict[str, dict[str, str]] = {}
        self._reverse: dict[str, str] = {}

    # -- placeholders -----------------------------------------------------
    def _placeholder(self, kind: str, original: str) -> str:
        """One placeholder per value (any case); restore gives the first spelling."""
        bucket = self._maps.setdefault(kind, {})
        key = original.lower()
        if key not in bucket:
            ph = f"<{kind}_{len(bucket) + 1}>"
            bucket[key] = ph
            self._reverse[ph] = original
        return bucket[key]

    def restore(self, text: str) -> str:
        """Map placeholders in model output back to the real values (local only)."""
        for ph in sorted(self._reverse, key=len, reverse=True):
            text = text.replace(ph, self._reverse[ph])
        return text

    def restore_obj(self, obj: Any) -> Any:
        """``restore`` for every string in a JSON-like structure (tool inputs)."""
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, list):
            return [self.restore_obj(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        return obj

    # -- text -------------------------------------------------------------
    @staticmethod
    def redact_secrets(text: str) -> str:
        """Secrets only (keys, passwords, tokens, URL credentials). For messages to
        the user's own Telegram bot, where entity ids must stay readable."""
        t = _ANSI.sub("", text)
        t = Redactor._secret_rules(t)
        for name, rx in _KNOWN.items():
            t = rx.sub(f"[REDACTED:{name}]", t)
        return t

    @staticmethod
    def _secret_rules(t: str) -> str:
        t = _KV_QUOTED.sub(
            lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(2)}", t
        )
        t = _KV_BARE.sub(lambda m: f"{m.group(1)}<redacted>", t)
        t = _BANG_SECRET.sub(r"\1<redacted>", t)
        t = _BEARER.sub(r"\1<redacted>", t)
        t = _URL_CREDS.sub(r"\1<redacted>@", t)
        t = _URL_QUERY.sub(r"\1<redacted>", t)
        t = _HA_MCP_PATH.sub(r"\1<redacted>", t)
        return _WEBHOOK.sub(r"\1<redacted>", t)

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        t = _ANSI.sub("", text)

        t = _KV_QUOTED.sub(
            lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(2)}", t
        )
        t = _KV_BARE.sub(lambda m: f"{m.group(1)}<redacted>", t)
        t = _BANG_SECRET.sub(r"\1<redacted>", t)
        t = _BEARER.sub(r"\1<redacted>", t)

        t = _URL_CREDS.sub(r"\1<redacted>@", t)
        t = _URL_QUERY.sub(r"\1<redacted>", t)
        t = _HA_MCP_PATH.sub(r"\1<redacted>", t)
        t = _WEBHOOK.sub(r"\1<redacted>", t)
        for name, rx in _KNOWN.items():
            t = rx.sub(f"[REDACTED:{name}]", t)

        t = _EMAIL.sub(lambda m: self._placeholder("email", m.group(0)), t)
        t = _MAC.sub(lambda m: self._placeholder("mac", m.group(0)), t)
        t = _COORD_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<coord>", t)
        t = _COORD_PAIR.sub("<coords>", t)
        t = _PERSON.sub(
            lambda m: "person." + self._placeholder("person", m.group(1)), t
        )
        for term in self._terms:
            # letters and digits bound a name; "_" and "." do not, so a name inside an
            # entity id (smoke_alarm_<name>_status) is hidden as well
            t = re.sub(
                rf"(?i)(?<![A-Za-z0-9<]){re.escape(term)}(?![A-Za-z0-9>])",
                lambda m: self._placeholder("name", m.group(0)),
                t,
            )

        if len(t) > self._max:
            t = t[: self._max] + " …[truncated]"
        return t

    # -- structures -------------------------------------------------------
    def redact_obj(self, obj: Any) -> Any:
        """Redact every string in a JSON-like structure and drop secret-ish keys."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return [self.redact_obj(v) for v in obj]
        if isinstance(obj, dict) and obj.get("type") in (
            "thinking",
            "redacted_thinking",
        ):
            return obj  # opaque, signed by the API: must go back unchanged
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                key = str(k)
                if key in DROP_KEYS:
                    continue
                if _SECRET_KEY.match(key) and isinstance(v, (str, int, float)):
                    out[key] = "<redacted>"
                else:
                    out[key] = self.redact_obj(v)
            return out
        return obj

    def verify(self, obj: Any) -> None:
        """Raise ``RedactionError`` if redacting ``obj`` again would change it."""
        again = Redactor(self._terms, self._max)
        if again.redact_obj(obj) != obj:
            raise RedactionError("payload still contains content that must be redacted")


def redact_for_cloud(text: str, extra_terms: Iterable[str] = ()) -> str:
    """One-shot helper for text that is saved to Drive or logged."""
    return Redactor(extra_terms).redact(text)
