"""Regression tests for tool selection during ``jarvis serve`` startup."""

from __future__ import annotations

import pytest

from openjarvis.cli.serve import _resolve_allowed_tools, _resolve_server_cors_origins
from openjarvis.core.config import JarvisConfig


@pytest.mark.parametrize(
    "configured",
    [
        "code_interpreter,file_read",
        ["code_interpreter", "file_read"],
    ],
)
def test_tools_enabled_is_used_by_serve(configured):
    config = JarvisConfig()
    config.tools.enabled = configured

    allowed, explicit = _resolve_allowed_tools(config)

    assert allowed == {"code_interpreter", "file_read"}
    assert explicit is True


def test_tools_enabled_takes_precedence_over_legacy_agent_tools():
    config = JarvisConfig()
    config.tools.enabled = "file_read"
    config.agent.tools = "calculator"

    allowed, explicit = _resolve_allowed_tools(config)

    assert allowed == {"file_read"}
    assert explicit is True


def test_agent_tools_remains_a_backward_compatible_fallback():
    config = JarvisConfig()
    config.agent.tools = "file_read"

    allowed, explicit = _resolve_allowed_tools(config)

    assert allowed == {"file_read"}
    assert explicit is True


def test_serve_defaults_tools_when_no_selection_is_configured():
    allowed, explicit = _resolve_allowed_tools(JarvisConfig())

    assert allowed == {"think", "calculator", "web_search"}
    assert explicit is False


def test_cors_environment_overrides_configured_defaults(monkeypatch):
    monkeypatch.setenv(
        "OPENJARVIS_CORS_ORIGINS",
        " https://frontend.example, https://admin.example ",
    )

    origins = _resolve_server_cors_origins(["http://localhost:5173"])

    assert origins == ["https://frontend.example", "https://admin.example"]


def test_cors_config_is_used_without_environment(monkeypatch):
    monkeypatch.delenv("OPENJARVIS_CORS_ORIGINS", raising=False)

    origins = _resolve_server_cors_origins(["http://localhost:5173"])

    assert origins == ["http://localhost:5173"]


def _write_skill(root, name):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_serve_loads_only_enabled_skills(tmp_path):
    from openjarvis.cli.serve import _load_skill_tools
    from openjarvis.core.events import EventBus

    _write_skill(tmp_path, "wanted-skill")
    _write_skill(tmp_path, "other-skill")
    config = JarvisConfig()
    config.skills.skills_dir = str(tmp_path)

    tools = _load_skill_tools(
        config, EventBus(), None, {"skill_wanted-skill", "file_read"}, set()
    )

    assert [t.spec.name for t in tools] == ["skill_wanted-skill"]
    assert "# wanted-skill" in tools[0].execute().content


def test_serve_skill_loading_never_duplicates_or_crashes(tmp_path):
    from openjarvis.cli.serve import _load_skill_tools
    from openjarvis.core.events import EventBus

    _write_skill(tmp_path, "wanted-skill")
    config = JarvisConfig()
    config.skills.skills_dir = str(tmp_path)
    allowed = {"skill_wanted-skill"}

    assert _load_skill_tools(config, EventBus(), None, allowed, allowed) == []

    config.skills.skills_dir = str(tmp_path / "missing")
    assert _load_skill_tools(config, EventBus(), None, allowed, set()) == []
