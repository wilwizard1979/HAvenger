"""Jarvis the Janitor, phase 1: redaction, store, budget guard, cloud client."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from openjarvis.agents import cloud_client as cc
from openjarvis.agents.cloud_client import (
    BudgetExceeded,
    CloudClient,
    CloudUnavailable,
    cost_usd,
)
from openjarvis.agents.redaction import RedactionError, Redactor, redact_for_cloud
from openjarvis.agents.store import DEFAULT_PRICES, SettingsError, Store

# Made-up password in the shape of the real SMTP repair text. Not a credential.
FAKE_PW = "Zq7!fake-Pass_9xK"
SMTP_REPAIR = (
    "name: notifier_gmail\nplatform: smtp\nserver: smtp.gmail.com\nport: 587\n"
    "sender: jane.doe@example.com\nusername: jane.doe@example.com\n"
    f"password: {FAKE_PW}\n"
    "recipient:\n- jane.doe@example.com\n"
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for var in (
        "JARVIS_JANITOR_DRY_RUN",
        "JARVIS_TRIAGE_MODEL",
        "JARVIS_DIAG_MODEL",
        "JARVIS_DEEP_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    return Store(tmp_path / "janitor.db")


class FakeSDK:
    """Records what would have been sent; returns a canned reply."""

    def __init__(self, text="ok", usage=None):
        self.calls: list[dict] = []
        self._text = text
        self._usage = usage or SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=self._usage,
            stop_reason="end_turn",
        )


def live(store: Store, sdk: FakeSDK, clock=None) -> CloudClient:
    store.update_settings({"dry_run": False})
    kwargs = {"clock": clock} if clock else {}
    return CloudClient(store, sdk_client=sdk, **kwargs)


# --- redaction -------------------------------------------------------------


def test_smtp_password_line_never_reaches_cloud_payload(store):
    sdk = FakeSDK()
    live(store, sdk).complete(
        "triage",
        system="You triage Home Assistant issues.",
        messages=[{"role": "user", "content": f"Repair details:\n{SMTP_REPAIR}"}],
    )
    sent = json.dumps(sdk.calls[0])
    assert FAKE_PW not in sent
    assert "jane.doe@example.com" not in sent
    assert "<redacted>" in sent and "<email_1>" in sent


def test_secret_never_reaches_dry_run_payload_either(store):
    res = CloudClient(store).complete(
        "triage", system="s", messages=[{"role": "user", "content": SMTP_REPAIR}]
    )
    assert res.dry_run and FAKE_PW not in json.dumps(res.request)


def test_structured_repairs_drop_placeholders_and_secret_keys():
    obj = {
        "issue_id": "x",
        "translation_placeholders": {"config": SMTP_REPAIR},
        "smtp_password": FAKE_PW,
        "nested": [{"api_key": "abc123456789", "text": f"token: {FAKE_PW}"}],
    }
    out = json.dumps(Redactor().redact_obj(obj))
    assert (
        FAKE_PW not in out
        and "translation_placeholders" not in out
        and "abc123456789" not in out
    )


@pytest.mark.parametrize(
    "text, leaked",
    [
        ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
        ("api_key = 'ABCDEF123456'", "ABCDEF123456"),
        ("password: !secret smtp_pw_value", "smtp_pw_value"),
        ("url: https://user:s3cretpass@example.com/x", "s3cretpass"),
        ("GET /hook?token=abcdef123456&x=1", "abcdef123456"),
        ("http://192.168.1.2:9584/private_AbCdEf123456", "private_AbCdEf123456"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
        ("webhook /api/webhook/AbCdEf_123-xyz", "AbCdEf_123-xyz"),
    ],
)
def test_secret_shapes_are_removed(text, leaked):
    assert leaked not in Redactor().redact(text)


def test_personal_data_becomes_stable_placeholders_and_restores():
    r = Redactor(extra_terms=["Musterstrasse"])
    text = (
        "device 60:74:F4:E3:C8:75 and 60:74:f4:e3:c8:75 at lat: 50.12345 "
        "near 50.12345, 8.54321, owner person.jane_doe lives Musterstrasse 5"
    )
    out = r.redact(text)
    assert (
        "60:74" not in out
        and "50.12345" not in out
        and "jane_doe" not in out
        and "Musterstrasse" not in out
    )
    assert out.count("<mac_1>") == 2  # same device, same placeholder (case-insensitive)
    restored = r.restore(out)
    assert (
        "person.jane_doe" in restored
        and "Musterstrasse 5" in restored
        and "60:74:F4:E3:C8:75" in restored
    )


def test_redaction_is_idempotent_and_verify_catches_leaks():
    r = Redactor()
    once = r.redact_obj({"t": SMTP_REPAIR})
    r.verify(once)  # clean
    with pytest.raises(RedactionError):
        r.verify({"t": f"password: {FAKE_PW}"})


def test_client_refuses_to_send_if_redaction_is_bypassed(store, monkeypatch):
    sdk = FakeSDK()
    client = live(store, sdk)

    class Leaky(
        Redactor
    ):  # redaction silently does nothing; verify() must still catch it
        def redact(self, text):
            return text

        def redact_obj(self, obj):
            return obj

    monkeypatch.setattr(cc, "Redactor", Leaky)
    with pytest.raises(RedactionError):
        client.complete(
            "triage",
            system="s",
            messages=[{"role": "user", "content": f"password: {FAKE_PW}"}],
        )
    assert sdk.calls == []


def test_long_text_is_truncated():
    assert len(Redactor(max_chars=100).redact("x" * 500)) < 130
    assert "…[truncated]" in redact_for_cloud("y" * 30_000)


# --- settings and store ----------------------------------------------------


def test_settings_defaults_match_the_plan(store):
    s = store.settings()
    assert s["budget"] == {
        "monthly_limit_usd": 20.0,
        "pause_auto_escalation_pct": 75,
        "hard_stop_pct": 100,
    }
    assert s["schedule"]["frequency"] == "daily" and s["schedule"]["time"] == "07:30"
    assert s["dry_run"] is True and s["models"]["triage"] == "claude-haiku-5-5"
    assert set(s["prices"]) == {
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    }


def test_schedule_can_be_switched_to_weekly_friday_and_persists(store, tmp_path):
    store.update_settings(
        {
            "schedule": {
                "frequency": "weekly",
                "weekday": "FRI",
                "time": "08:00",
                "timezone": "Europe/Berlin",
            }
        }
    )
    again = Store(tmp_path / "janitor.db").settings()["schedule"]
    assert again == {
        "frequency": "weekly",
        "weekday": "fri",
        "time": "08:00",
        "timezone": "Europe/Berlin",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {
            "schedule": {
                "frequency": "hourly",
                "weekday": "fri",
                "time": "07:30",
                "timezone": "Europe/Berlin",
            }
        },
        {
            "schedule": {
                "frequency": "daily",
                "weekday": "fri",
                "time": "25:99",
                "timezone": "Europe/Berlin",
            }
        },
        {
            "schedule": {
                "frequency": "daily",
                "weekday": "fri",
                "time": "07:30",
                "timezone": "Mars/Base",
            }
        },
        {
            "budget": {
                "monthly_limit_usd": -1,
                "pause_auto_escalation_pct": 75,
                "hard_stop_pct": 100,
            }
        },
        {
            "budget": {
                "monthly_limit_usd": 20,
                "pause_auto_escalation_pct": 90,
                "hard_stop_pct": 80,
            }
        },
        {"dry_run": "no"},
        {"nonsense": 1},
    ],
)
def test_bad_settings_are_rejected(store, patch):
    with pytest.raises(SettingsError):
        store.update_settings(patch)


def test_issue_lifecycle_and_reports_and_proposals(store):
    a = store.upsert_issue(
        "fp1", category="repair", title="t", severity="error", now=100.0
    )
    b = store.upsert_issue(
        "fp1", category="repair", title="t", severity="error", now=200.0
    )
    assert (a["occurrences"], b["occurrences"], b["first_seen"], b["last_seen"]) == (
        1,
        2,
        100.0,
        200.0,
    )
    store.set_issue_state("fp1", "resolved")
    assert (
        store.upsert_issue("fp1", category="repair", title="t")["state"] == "open"
    )  # comes back
    store.add_report("attention", "Two things need attention.", {"n": 2})
    assert store.latest_report()["body"] == {"n": 2}
    pid = store.add_proposal(
        "fp1", "claude-sonnet-5-5", "d", "low", {"x": 1}, {"y": 1}, ["check"]
    )
    store.set_proposal_status(pid, "approved", telegram_message_id="42")
    p = store.proposals("approved")[0]
    assert p["change"] == {"x": 1} and p["telegram_message_id"] == "42"
    with pytest.raises(ValueError):
        store.set_proposal_status(pid, "bogus")


# --- cost, ledger, budget guard --------------------------------------------


def test_cost_matches_the_verified_price_table():
    usage = {
        "input_tokens": 10_000,
        "output_tokens": 2_000,
        "cache_read_input_tokens": 50_000,
        "cache_creation_input_tokens": 5_000,
    }
    sonnet = DEFAULT_PRICES["claude-sonnet-5-5"]
    assert cost_usd(sonnet, usage) == pytest.approx(
        (10_000 * 2 + 5_000 * 2.5 + 50_000 * 0.10 + 2_000 * 10) / 1e6
    )
    haiku = DEFAULT_PRICES["claude-haiku-5-5"]
    short = cost_usd(haiku, {"input_tokens": 1000, "output_tokens": 1000})
    long = cost_usd(haiku, {"input_tokens": 200_000, "output_tokens": 1000})
    assert short == pytest.approx((1000 * 0.10 + 1000 * 0.50) / 1e6)
    assert long == pytest.approx(
        (200_000 * 0.50 + 1000 * 2.50) / 1e6
    )  # >100k prompt tier


def test_live_call_writes_ledger_run_and_uses_prompt_caching(store):
    sdk = FakeSDK(text="hello")
    res = live(store, sdk).complete(
        "diagnose",
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}],
    )
    sent = sdk.calls[0]
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert (
        sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        and "cache_control" not in sent["tools"][0]
    )
    assert res.text == "hello" and res.cost_usd == pytest.approx(
        (1000 * 2 + 200 * 10) / 1e6
    )
    assert store.month_spend() == pytest.approx(res.cost_usd)
    assert store.runs(1)[0]["outcome"] == "ok"


def test_model_tier_env_overrides(store, monkeypatch):
    monkeypatch.setenv("JARVIS_TRIAGE_MODEL", "claude-haiku-5-5-test")
    assert CloudClient(store).model_for("triage") == "claude-haiku-5-5-test"


def _spend(store: Store, usd: float) -> None:
    store.add_usage(
        "claude-sonnet-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=usd,
    )


def test_budget_pauses_auto_escalation_at_75_but_not_manual_or_triage(store):
    _spend(store, 15.0)  # 75% of $20
    client = live(store, FakeSDK())
    with pytest.raises(BudgetExceeded, match="auto_escalation_paused"):
        client.complete(
            "diagnose", system="s", messages=[{"role": "user", "content": "x"}]
        )
    with pytest.raises(BudgetExceeded):
        client.complete("deep", system="s", messages=[{"role": "user", "content": "x"}])
    client.complete(
        "diagnose", system="s", messages=[{"role": "user", "content": "x"}], manual=True
    )
    client.complete("triage", system="s", messages=[{"role": "user", "content": "x"}])


def test_budget_hard_stop_at_100_blocks_everything(store):
    _spend(store, 20.0)
    sdk = FakeSDK()
    client = live(store, sdk)
    for tier in ("triage", "diagnose", "deep"):
        with pytest.raises(BudgetExceeded, match="hard_stop"):
            client.complete(
                tier,
                system="s",
                messages=[{"role": "user", "content": "x"}],
                manual=True,
            )
    assert sdk.calls == []
    assert store.runs(1)[0]["outcome"].startswith("blocked:")


def test_call_that_would_cross_the_limit_is_refused(store):
    _spend(store, 19.99)
    with pytest.raises(BudgetExceeded, match="would_exceed_limit"):
        live(store, FakeSDK()).complete(
            "deep",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=4096,
            manual=True,
        )


def test_new_month_resets_the_budget(store):
    import time as _t

    _spend(store, 20.0)
    next_month = _t.time() + 40 * 86400
    assert store.month_spend(next_month) == 0.0
    sdk = FakeSDK()
    live(store, sdk, clock=lambda: next_month).complete(
        "triage", system="s", messages=[{"role": "user", "content": "x"}]
    )
    assert len(sdk.calls) == 1


# --- dry run and key handling ----------------------------------------------


def test_dry_run_never_touches_network_key_or_keychain(store, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("keychain must not be read in dry-run"),
    )
    client = CloudClient(
        store, key_resolver=lambda: pytest.fail("key must not be resolved in dry-run")
    )
    res = client.complete(
        "triage", system="s", messages=[{"role": "user", "content": "x"}]
    )
    assert res.dry_run and res.cost_usd == 0.0 and store.month_spend() == 0.0
    assert store.runs(1)[0]["outcome"] == "dry_run"


def test_env_flag_forces_dry_run_even_if_setting_is_live(store, monkeypatch):
    store.update_settings({"dry_run": False})
    monkeypatch.setenv("JARVIS_JANITOR_DRY_RUN", "1")
    assert CloudClient(store).is_dry_run()


def test_key_never_comes_from_the_gui_env_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-GUIKEYGUIKEYGUIKEYGUIKEY")
    monkeypatch.delenv(cc.KEY_ENV, raising=False)
    monkeypatch.setattr(cc, "_read_keychain", lambda: "")
    with pytest.raises(CloudUnavailable):
        cc.resolve_api_key()


def test_workspace_id_stored_by_mistake_is_rejected_with_a_clear_message(monkeypatch):
    monkeypatch.delenv(cc.KEY_ENV, raising=False)
    monkeypatch.setattr(cc, "_read_keychain", lambda: "wrkspc_0123456789abcdef")
    with pytest.raises(CloudUnavailable, match="sk-ant-"):
        cc.resolve_api_key()


def test_key_from_dedicated_env_var_or_keychain(monkeypatch):
    monkeypatch.setenv(cc.KEY_ENV, "sk-ant-api03-ENVKEYENVKEYENVKEYENVKEY")
    assert cc.resolve_api_key().startswith("sk-ant-")
    monkeypatch.delenv(cc.KEY_ENV)
    monkeypatch.setattr(
        cc, "_read_keychain", lambda: "sk-ant-api03-CHAINCHAINCHAINCHAIN"
    )
    assert cc.resolve_api_key().endswith("CHAIN")


# --- API -------------------------------------------------------------------


def test_settings_and_status_endpoints(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.agents import store as store_mod
    from openjarvis.server.ha_agents_routes import ha_agents_router

    monkeypatch.setenv("JARVIS_JANITOR_DB", str(tmp_path / "api.db"))
    monkeypatch.delenv("JARVIS_JANITOR_DRY_RUN", raising=False)
    store_mod.reset_store_for_tests()
    app = FastAPI()
    app.include_router(ha_agents_router)
    http = TestClient(app)

    st = http.get("/v1/ha/agents/status").json()
    assert (
        st["dry_run"] is True
        and st["budget"]["limit_usd"] == 20.0
        and st["budget"]["state"] == "ok"
    )
    assert st["keychain_service"] == "jarvis-the-janitor"

    sched = {
        "frequency": "weekly",
        "weekday": "fri",
        "time": "08:00",
        "timezone": "Europe/Berlin",
    }
    assert (
        http.put("/v1/ha/agents/settings", json={"schedule": sched}).json()["schedule"]
        == sched
    )
    assert (
        http.put(
            "/v1/ha/agents/settings", json={"schedule": {**sched, "time": "99:99"}}
        ).status_code
        == 422
    )
    store_mod.reset_store_for_tests()
