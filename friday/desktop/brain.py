"""Brain — connects the desktop UI to OpenAI + FRIDAY's MCP tools.

Runs each chat turn on a worker thread so the orb never stutters. Emits
Qt signals for: state changes (idle/thinking/speaking), tool activity,
and final assistant text.

Falls back gracefully if OPENAI_API_KEY isn't set — the UI still runs,
the orb still breathes, and FRIDAY replies with a dry "I'm dark, boss."
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import traceback
from typing import Any

from PySide6.QtCore import QObject, Signal

# Reuse the existing tool server.
from mcp.server.fastmcp import FastMCP
from agent_friday import SYSTEM_PROMPT
from friday.tools import register_all_tools

from .audio_bus import AudioBus


_MAX_TOOL_ITERS = 8
_MAX_TOOL_OUTPUT_CHARS = 6000


class Brain(QObject):
    state_changed   = Signal(str)            # "idle" | "thinking" | "speaking"
    activity        = Signal(str, str, str)  # tool, detail, kind ("info"/"ok"/"err")
    assistant_text  = Signal(str)            # final assistant reply
    error           = Signal(str)

    def __init__(self, model: str = "gpt-4o", parent=None):
        super().__init__(parent)
        self.model = model
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        # Build our own MCP instance so we don't need the SSE server
        # running in another process.
        self._mcp = FastMCP(name="Friday-Desktop")
        register_all_tools(self._mcp)
        self._tools_schema = self._build_tools_schema()

        # OpenAI client (only if key is present).
        self._client = None
        if os.getenv("OPENAI_API_KEY"):
            try:
                from openai import OpenAI
                self._client = OpenAI()
            except Exception as exc:
                self.error.emit(f"OpenAI client init failed: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, user_text: str) -> None:
        """Kick off a chat turn on a background thread."""
        t = threading.Thread(target=self._run_turn, args=(user_text,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_tools_schema(self) -> list[dict[str, Any]]:
        tools = asyncio.run(self._mcp.list_tools())
        out: list[dict[str, Any]] = []
        for t in tools:
            params = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "").strip()[:1024],
                    "parameters": params,
                },
            })
        return out

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP tool synchronously and stringify the result."""
        result = asyncio.run(self._mcp.call_tool(name, arguments))
        # FastMCP may return a list of ContentBlock objects, or a dict.
        if isinstance(result, dict):
            return json.dumps(result, default=str)[:_MAX_TOOL_OUTPUT_CHARS]
        chunks: list[str] = []
        for block in result or []:
            text = getattr(block, "text", None)
            if text is not None:
                chunks.append(text)
            else:
                chunks.append(str(block))
        return "\n".join(chunks)[:_MAX_TOOL_OUTPUT_CHARS]

    def _run_turn(self, user_text: str) -> None:
        try:
            self.history.append({"role": "user", "content": user_text})
            self.state_changed.emit("thinking")

            if self._client is None:
                self.assistant_text.emit(
                    "I'm dark, boss — no OPENAI_API_KEY in the environment. "
                    "Set it and we're back online."
                )
                self.state_changed.emit("idle")
                return

            for _ in range(_MAX_TOOL_ITERS):
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=self.history,
                    tools=self._tools_schema,
                    tool_choice="auto",
                    temperature=0.7,
                )
                msg = resp.choices[0].message
                # Append assistant message (with any tool_calls).
                assistant_entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                }
                if msg.tool_calls:
                    assistant_entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                self.history.append(assistant_entry)

                if not msg.tool_calls:
                    text = (msg.content or "").strip()
                    self.state_changed.emit("speaking")
                    # Drive a believable speaking envelope through the
                    # audio bus — roughly proportional to reply length.
                    spoken_chars = max(20, len(text))
                    AudioBus.instance().push_synthetic(
                        duration_s=min(8.0, spoken_chars * 0.045),
                        intensity=0.85,
                    )
                    self.assistant_text.emit(text or "…")
                    self.state_changed.emit("idle")
                    return

                # Dispatch each tool call.
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    self.activity.emit(name, _summarise(args), "info")
                    try:
                        output = self._call_tool(name, args)
                        kind = "ok"
                    except Exception as exc:
                        output = f"Tool error: {exc}"
                        kind = "err"
                    self.activity.emit(name, _summarise(args, output), kind)
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": output,
                    })

            # Hit the iteration cap.
            self.assistant_text.emit(
                "Hit the tool-loop ceiling, boss — let's reset and try again."
            )
            self.state_changed.emit("idle")

        except Exception as exc:
            self.error.emit(f"{exc}\n{traceback.format_exc()[:2000]}")
            self.state_changed.emit("idle")


def _summarise(args: dict[str, Any], output: str | None = None) -> str:
    """Compact one-line description of a tool call for the activity feed."""
    if not args:
        head = "—"
    else:
        bits = []
        for k, v in list(args.items())[:3]:
            sv = str(v)
            if len(sv) > 40:
                sv = sv[:37] + "…"
            bits.append(f"{k}={sv}")
        head = ", ".join(bits)
    if output is not None:
        n = len(output)
        head += f"  ·  {n} chars"
    return head
