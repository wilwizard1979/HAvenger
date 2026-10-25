"""``house_status``: Jarvis answers questions about the house from the Janitor.

Qwen (the chat model) calls this tool; the answer comes from the plain-English
house status file, written by Python from real data, and is number-checked. The
tool only reads: it cannot change anything in Home Assistant.
"""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("house_status")
class HouseStatusTool(BaseTool):
    tool_id = "house_status"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="house_status",
            description=(
                "How the house and Home Assistant are doing (problems, backups, "
                "updates, what to fix first). Pass the user's question. Reply with "
                "the answer as it is returned, do not add anything."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question, e.g. 'how is the backup?'",
                    }
                },
                "required": ["question"],
            },
            category="home",
        )

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.agents.house import ask_house
        from openjarvis.agents.store import get_store

        question = str(params.get("question", "")).strip() or "How is the house doing?"
        out = ask_house(get_store(), question)
        if out["status"] == "disabled":
            return ToolResult(
                tool_name="house_status",
                content="The house status feature is switched off in the settings.",
                success=False,
            )
        if out["status"] == "unavailable":
            return ToolResult(
                tool_name="house_status",
                content=out.get("status_text", ""),
                success=True,
            )
        return ToolResult(tool_name="house_status", content=out["answer"], success=True)


__all__ = ["HouseStatusTool"]
