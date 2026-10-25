"""L1 Qwen job: read the deterministic report aloud. Local, free, no analysis.

Qwen only rephrases the report text for the voice. A guard keeps it honest: every
number in the spoken text must appear in the report, otherwise the report text
itself is stored instead and the read-out is flagged.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Any, Callable

from openjarvis.agents.store import Store

DEFAULT_MODEL = "qwen3.5:4b"
MAX_CHARS = 600
_NUM = re.compile(r"\d+(?:[.,]\d+)?")

SYSTEM = (
    "Read this Home Assistant status report aloud in at most three short, plain "
    "sentences. Say only what the report says. Copy every number exactly as "
    "written in the report; if the report says 'Quick wins: 1', say one quick "
    "win. Do not add, change, round or compute any number, and do not give advice."
)
RETRY = (
    "That was not faithful: the number(s) {bad} are not in the report. Read the "
    "report again and use only these numbers: {allowed}."
)


_UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_WORD = re.compile(r"[a-z]+")


def _word_numbers(text: str) -> set[str]:
    """Numbers spelled out ("three", "forty one") as digit strings."""
    out: set[str] = set()
    words = _WORD.findall(text.lower().replace("-", " "))
    i = 0
    while i < len(words):
        w = words[i]
        if w in _TENS:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if 1 <= _UNITS.get(nxt, 0) <= 9:
                out.add(str(_TENS[w] + _UNITS[nxt]))
                i += 2
                continue
            out.add(str(_TENS[w]))
        elif w in _UNITS:
            out.add(str(_UNITS[w]))
        elif w == "hundred":
            out.add("100")
        i += 1
    return out


def numbers(text: str) -> set[str]:
    digits = {n.replace(",", ".") for n in _NUM.findall(text)}
    # "20.00" and "20" are the same figure
    digits |= {n.rstrip("0").rstrip(".") for n in digits if "." in n}
    return digits | _word_numbers(text)


def guard(spoken: str, source: str) -> bool:
    return (
        bool(spoken.strip())
        and len(spoken) <= MAX_CHARS
        and numbers(spoken) <= numbers(source)
    )


def ollama_generate(model: str, system: str, text: str) -> dict[str, Any]:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if not host.startswith("http"):
        host = f"http://{host}"
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "keep_alive": os.environ.get("JARVIS_KEEP_ALIVE", "15m"),
        "options": {"temperature": 0, "num_predict": 220},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - local host
        d = json.load(resp)
    return {
        "text": (d.get("message") or {}).get("content", ""),
        "tokens_in": int(d.get("prompt_eval_count", 0) or 0),
        "tokens_out": int(d.get("eval_count", 0) or 0),
    }


def run_readout(
    store: Store,
    *,
    generate: Callable[[str, str, str], dict[str, Any]] = ollama_generate,
    now: float | None = None,
) -> dict[str, Any]:
    report = store.latest_report()
    if not report:
        return {"status": "no_report"}
    model = os.environ.get("JARVIS_READOUT_MODEL", DEFAULT_MODEL)
    source = report["summary_text"]
    t0 = time.monotonic()
    try:
        out = generate(model, SYSTEM, source)
        bad = numbers(out["text"]) - numbers(source)
        if bad:  # one correction round: tell it which numbers were invented
            retry = RETRY.format(
                bad=", ".join(sorted(bad)), allowed=", ".join(sorted(numbers(source)))
            )
            second = generate(model, SYSTEM, f"{source}\n\n{retry}")
            out = {
                "text": second["text"],
                "tokens_in": out["tokens_in"] + second["tokens_in"],
                "tokens_out": out["tokens_out"] + second["tokens_out"],
            }
    except Exception as exc:  # noqa: BLE001 - Qwen being off must not break anything
        store.record_run(
            "L1", model, outcome="error", purpose=str(exc)[:120], cost_usd=0.0
        )
        return {"status": "unavailable", "detail": str(exc)[:200]}
    seconds = time.monotonic() - t0
    spoken = out["text"].strip()
    ok = guard(spoken, source)
    rid = store.add_readout(
        report_id=report["id"] if "id" in report else None,
        model=model,
        tokens_in=out["tokens_in"],
        tokens_out=out["tokens_out"],
        seconds=seconds,
        text=spoken if ok else source,
        guard_ok=ok,
        note=""
        if ok
        else "Qwen changed or invented a number; the report text is used instead",
        ts=now,
    )
    store.record_run(
        "L1",
        model,
        outcome="ok" if ok else "guard_failed",
        purpose="readout",
        input_tokens=out["tokens_in"],
        output_tokens=out["tokens_out"],
        cost_usd=0.0,
    )
    return {
        "status": "ok" if ok else "guard_failed",
        "id": rid,
        "seconds": round(seconds, 1),
    }
