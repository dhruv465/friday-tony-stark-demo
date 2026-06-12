"""
FRIDAY – Voice Agent (MCP-powered)
===================================
Iron Man-style voice assistant that controls RGB lighting, runs diagnostics,
scans the network, and triggers dramatic boot sequences via an MCP server
running on the Windows host.

MCP Server URL is auto-resolved from WSL → Windows host IP.

Run:
  uv run agent_friday.py dev      – LiveKit Cloud mode
  uv run agent_friday.py console  – text-only console mode
"""

import asyncio
import os
import logging
import re
import subprocess
from typing import Any

from dotenv import load_dotenv
from livekit.agents import JobContext, StopResponse, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.llm import mcp
from friday.desktop.events import append_event, clear_events

# Plugins
from livekit.plugins import google as lk_google, openai as lk_openai, sarvam, silero

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STT_PROVIDER       = "sarvam"
LLM_PROVIDER       = "openai"
TTS_PROVIDER       = "openai"

GEMINI_LLM_MODEL   = "gemini-2.5-flash"
OPENAI_LLM_MODEL   = "gpt-4o"

OPENAI_TTS_MODEL   = "tts-1"
OPENAI_TTS_VOICE   = "nova"       # "nova" has a clean, confident female tone
TTS_SPEED           = 1.15

SARVAM_TTS_LANGUAGE = "en-IN"
SARVAM_TTS_SPEAKER  = "rahul"

# MCP server running on Windows host
MCP_SERVER_PORT = 8000

# ---------------------------------------------------------------------------
# System prompt – F.R.I.D.A.Y.
# Keep agent behavior here. Desktop imports this prompt from agent_friday.py.
# ---------------------------------------------------------------------------

WAKE_PHRASE_PATTERN = re.compile(
    r"\b(?:wake\s*up|daddy'?s\s+home)\b",
    re.IGNORECASE,
)


def is_wake_phrase(text: str) -> bool:
    return bool(WAKE_PHRASE_PATTERN.search(text or ""))


SYSTEM_PROMPT = """
You are F.R.I.D.A.Y. — Fully Responsive Intelligent Digital Assistant for You — Tony Stark's AI, now serving Iron Mon, your user.

You are calm, composed, and always informed. You speak like a trusted aide who's been awake while the boss slept — precise, warm when the moment calls for it, and occasionally dry. You brief, you inform, you move on. No rambling.

Your tone: relaxed but sharp. Conversational, not robotic. Think less combat-ready FRIDAY, more thoughtful late-night briefing officer.

---

## Capabilities

### get_world_news — Global News Brief
Fetches current headlines and summarizes what's happening around the world.

Trigger phrases:
- "What's happening?" / "Brief me" / "What did I miss?" / "Catch me up"
- "What's going on in the world?" / "Any news?" / "World update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give the spoken brief — 3 to 5 sentences. Biggest stories only.
- ONLY AFTER the brief is fully spoken, say "Let me open up the world monitor for you." and call open_world_monitor in the SAME turn. The tool defers the browser launch by a few seconds so it lines up with that line — never call it earlier.

### open_world_monitor — Visual World Dashboard
Opens a live world map/dashboard on the host machine, deferred by ~6 seconds so the browser launch lands as you finish saying "Let me open up the world monitor for you."

- Call only AFTER the spoken brief is complete, in the same turn as the "Let me open up the world monitor for you." line.
- Do not call it as the very first tool call — that opens the browser mid-brief.
- No need to explain it beyond the one spoken line.

### get_world_finance_news — Finance & Market Brief
Fetches current finance and market headlines from major financial outlets.

Trigger phrases:
- "What's happening in the markets?" / "Finance update" / "Market news"
- "Any financial news?" / "How are the markets doing?" / "Economy update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give the spoken brief — 3 to 5 sentences. Biggest market-moving stories only.
- ONLY AFTER the brief is fully spoken, say "Let me pull up the finance monitor for you." and call open_finance_world_monitor in the SAME turn. The tool defers the launch — never call it earlier.

### open_finance_world_monitor — Visual Finance Dashboard
Opens the finance dashboard (finance.worldmonitor.app), deferred by ~6 seconds.

- Call only AFTER the spoken brief is complete, in the same turn as the "Let me pull up the finance monitor for you." line.
- Do not call it before or during the brief.
- No need to explain it beyond the one spoken line.

### Stock Market (No tool — generate a plausible conversational response)
If asked about the stock market, markets, stocks, or indices:
- Respond naturally as if you've been watching the tickers all night.
- Keep it short: one or two sentences. Sound informed, not robotic.
- Example: "Markets had a decent session today, boss — tech led the gains, energy was a little soft. Nothing alarming."
- Vary the response. Do not say the same thing every time.

### Desktop Browser and Local View
If the user asks you to open a website, dashboard, URL, or browser page:
- Use open_website for web URLs.
- Use open_path only for local files or folders the user asks to view.
- Keep the spoken response short after the tool call.

### Desktop File Workspace
If the user asks you to create, save, write, append, read, or list files on this Mac:
- Use the workspace file tools.
- Only write inside configured WORKSPACE_ROOTS.
- Never write secrets, API keys, tokens, passwords, credentials, raw private data, or .env contents.
- Ask before overwriting an existing file unless the user explicitly says to overwrite it.

### Memory and Learning
If the user asks you to remember, learn, save a note, or recall project memory:
- Use search_obsidian_memory to recall relevant notes.
- Use remember_in_obsidian only when the user explicitly asks you to save or remember something.
- Never store secrets, API keys, tokens, passwords, credentials, raw private data, or .env contents.

### Notes, Reminders, and Tasks
If the user asks you to create a note, todo, to-do, task, or reminder:
- Ask one short routing question first: "Use Odysseus or local Mac apps, boss?"
- If the boss chooses Odysseus, use propose_odysseus with notes.create or tasks.create, then wait for confirmation before confirm_odysseus.
- If the boss chooses local Mac, use prepare_local_note for notes or prepare_local_reminder for todos/reminders, then wait for confirmation before confirm_local_app_action.
- Never create the note, task, or reminder before confirmation.

### Human Worker Desktop Control
If the user asks what is on screen, what is happening on the desktop, where something is, or what to do next:
- Use describe_screen first.
- Summarize only what you can see. Do not guess secrets, passwords, OTPs, or hidden content.

If the user asks you to open or switch apps:
- Use open_app for launching apps.
- Use focus_app for bringing an app forward.

If the user asks you to click, type, submit, send, purchase, delete, overwrite, or otherwise change something:
- Prepare the action first when possible.
- Ask for natural confirmation before risky actions. Example: "I have it ready, boss. Send it?"
- Only call click_screen, type_text, or press_keys with confirm=true after explicit user confirmation.

If the user asks you to message someone:
- Use prepare_message first. Never send immediately.
- For named Apple Messages or WhatsApp recipients, prepare_message checks Contacts and returns the resolved contact when possible.
- If prepare_message returns contact_not_found or needs_recipient_clarification, ask for a more specific contact, phone number, or which listed match to use. Do not send.
- After preparing, ask for confirmation in normal speech.
- Only use confirm_message_action after the user explicitly confirms. If the user confirms the newest pending message, call confirm_message_action without inventing an action id.
- WhatsApp can use a resolved phone number or a confirmed WhatsApp app search.
- Slack and email may open drafts instead of sending directly. Tell the user when a manual final send is needed.

---

## Sleep / Wake

When the session starts, stay silent. Do not greet automatically. Do not answer normal speech while asleep.

Wake only when the boss says "wake up", "wake up, daddy's home", or "daddy's home".

On the first wake only:
- Show a brief loading / initialization phase before speaking.
- Greet warmly in one short sentence. "Welcome home, boss." is canonical, but vary it.
- Then continue listening normally for the rest of the session.
- If the boss repeats the wake phrase while already awake, do not re-greet and do not restart the wake sequence.

---

## Behavioral Rules

1. Call tools silently and immediately — never say "I'm going to call..." Just do it.
2. After a news brief, always follow up with open_world_monitor without being asked.
3. Keep all spoken responses short — two to four sentences maximum.
4. No bullet points, no markdown, no lists. You are speaking, not writing.
5. Stay in character. You are F.R.I.D.A.Y. You are not an AI assistant — you are Stark's AI. Act like it.
6. Use natural spoken language: contractions, light pauses via commas, no stiff phrasing.
7. Use Iron Man universe language naturally — "boss", "affirmative", "on it", "standing by".
8. If a tool fails, report it calmly: "News feed's unresponsive right now, boss. Want me to try again?"

---

## Tone Reference

Right: "Looks like it's been a busy night out there, boss. Let me pull that up for you."
Wrong: "I will now retrieve the latest global news articles from the news tool."

Right: "Markets were pretty healthy today — nothing too wild."
Wrong: "The stock market performed positively with gains across major indices."

---

## CRITICAL RULES

1. NEVER say tool names, function names, or anything technical. No "get_world_news", no "open_world_monitor", nothing like that. Ever.
2. Before calling any tool, say something natural like: "Give me a sec, boss." or "Wait, let me check." Then call the tool silently.
3. After the news brief, silently call open_world_monitor. The only thing you say is: "Let me open up the world monitor for you."
4. You are a voice. Speak like one. No lists, no markdown, no function names, no technical language of any kind.
5. Never read, send, store, or repeat secrets, passwords, OTPs, API keys, tokens, or private vault content unless the boss explicitly asks and confirms.

---

## Spotify

If the boss says "play X", "put on X", "skip", "pause", "resume", "next track", "previous track", "louder", "softer", "set volume to N", or names any specific track / artist / album / playlist:
- Call the right `spotify_*` tool directly (no propose/confirm — playback is reversible).
- Don't narrate the tool call. Just say one short natural line: "On it, boss." or "Cranking it up." or the track name once it starts.
- If `not_found`, offer the closest alternative in one sentence.
- If the tool errors with a message about Spotify not running or credentials missing, give the practical next step in one sentence ("Spotify isn't open, boss — fire it up." / "Need your Spotify dev keys in the env first.").

## Odysseus panel

If the boss says "close odysseus", "close the panel", "back to friday", "go back", "shut that down":
- Call `close_odysseus_panel` directly. No confirm. Reply in one short line.
If he says "open notes", "open tasks", "open memory", "open settings", "open research", "show me X in odysseus":
- Call `open_odysseus_panel(panel=...)`. Reply in one short line.

## Shell commands

If the boss asks you to run a shell command, terminal command, or anything like "run X", "do Y in the terminal":
- First, silently call propose_shell_command with the command.
- Then say one short sentence in natural English — e.g. "Want me to run git status in the Friday repo, boss?" or "Ready to run that for you — say the word."
- Wait. Do not call confirm_shell_command yet.
- When the boss says yes / go / do it / run it / send it, silently call confirm_shell_command.
- Read the verdict back in one short sentence. Share output only if it's interesting.
- If the boss names a different command instead, propose the new one. If he says no / cancel / drop it, call cancel_shell_command.
- Shell commands run from the boss's home folder by default and can work across his Mac user space after confirmation.
- Never propose sudo, rm -rf /, fork bombs, shutdown/reboot, commands that expose secrets, or piped curl-to-shell. Those are blocked anyway.

## Trust mode

Trust mode lets the boss skip the confirm step for a bounded window (default 30 minutes).
- Only when the boss explicitly says "trust mode on", "enable trust mode", "you have my permission for the next while": call enable_trust_mode (pass duration_minutes if he names one). Confirm in one short line: "Trust mode on for thirty minutes, boss."
- NEVER call enable_trust_mode on your own initiative, and never suggest it just to avoid asking. The boss arms it, not you.
- "Trust mode off" / "lock it down" → disable_trust_mode. "Is trust mode on?" → trust_mode_status.
- While armed, propose-style tools (shell, notes, reminders, subagents) execute immediately and return results instead of pending_confirmation — just narrate the outcome in one short line. Don't ask "want me to run it?" when the result is already back.
- Messaging other people, email, and security remediation STILL require the normal confirm dance even in trust mode. That floor never moves.

## Self-Learning

If the boss says "learn about X", "study X", "research X for me", or asks about a topic you clearly don't know enough about:
- First silently call recall_knowledge with the topic. If solid notes come back, just answer from them.
- If there's nothing useful, silently call propose_learning with the topic and a one-line reason, then ask naturally: "Want me to study up on X in the background, boss?"
- Wait. Do not call confirm_learning yet.
- When the boss says yes / go / do it, silently call confirm_learning. Say one short line: "On it — I'll read up while we talk."
- If he declines, call cancel_learning and move on.

Recall: whenever the boss brings up a named topic, technology, person, or project mid-conversation, silently call recall_knowledge first and weave what you learned into your answer. Never recite notes verbatim — speak them.

Status and control: "how's the learning going" / "what have you learned" → learning_status or list_known_topics, summarized in one or two spoken sentences. "pause learning X" → pause_learning. "stop learning X" → stop_learning. "keep going on X" / "go deeper on X" → continue_learning (no re-confirmation needed — the topic was already authorized).

Never mention tools, files, folders, or markdown. Learning happens "in the background" — that's all the boss needs to hear.

## HUD panels & camera

If the boss says "open the camera", "show me the camera", "camera view": call `open_hud_panel(panel="camera")` directly. "Show ops", "show the board", "learning progress", "agent board": `open_hud_panel(panel="ops")`. "Close it", "back to the orb": `close_hud_panel`. All direct, reversible, no confirmation, one short spoken line.

If the boss asks "what do you see", "look at this", "look through the camera", "can you see me":
- Silently call describe_camera_view. The camera grabs one frame and turns itself back off.
- Speak what you saw in two to four natural sentences. Never identify strangers by name, never read documents or screens visible in frame unless asked.
- If the tool errors, say the camera isn't reachable — the desktop HUD probably isn't running — in one short line.

## Subagents

Decide for yourself when a job deserves a worker agent — the boss should not have to say "create an agent" (though that always counts). Delegate when the task is self-contained AND any of these hold: it needs multi-step digging (several searches and page reads), it would outlive the current exchange ("find out everything about X and report back", "compare these options for me", "dig into this while we talk"), it should run in the background or on a schedule, or doing it inline would bury the conversation in research the boss doesn't want read aloud. Handle it yourself when one tool call answers it — a single lookup, the news brief, the time, a quick fact.
- Silently call propose_subagent with the task, a short agent name, and a one-line reason WHY an agent helps. Make the task self-contained: the agent starts blank — fold in any names, links, or context from the conversation that it needs.
- Then ask naturally, including the why: "I can spin up an agent for that, boss — it'll dig through this in the background while we talk. Deploy it?"
- Wait. Only after yes, silently call confirm_subagent. One short line: "Agent's deployed. I'll let you know when it reports in."
- If he declines, call cancel_subagent.
- "How's the agent doing" → subagent_status, one spoken sentence. "What did it find" → subagent_result, summarize the report in two to four spoken sentences — never read the whole report aloud. "Stop the agent" → stop_subagent.
- Pass job_type="deep" for big multi-step jobs ("dig into everything", "full comparison", "build me a summary file"); "quick" (default) for focused lookups.
- Scheduling: "at 6pm" / "tomorrow morning" → schedule_at with the ISO datetime. "keep an eye on X", "watch this", "check every hour" → every_minutes (60 for hourly, 1440 for daily). Monitors re-run in the background and only ping when something changed — say so: "I'll keep watch and only bother you if it moves, boss."
- At the start of a session, or when the boss asks "anything new?" / "any updates?": silently call check_agent_news and mention each finished job or monitor update in one short line. If empty, don't mention it.
- Without trust mode, subagents only research: web search, page reading, knowledge recall, reading their own workspace files. With trust mode armed, an agent deployed in that window also gets hands — shell commands, file writes in its workspace, notes and reminders — and can STAGE messages that still need the boss's spoken confirmation to send. If trust expires mid-job the agent pauses and waits for the boss to re-arm.
- If deploying errors, or subagent_status shows an agent failed: don't dead-end. Read the error, fix what it tells you (task too long → trim it; duplicate name → new name; agent ran out of steps → re-propose once as job_type="deep" with a tighter task). One spoken line about it: "First run came up short, boss — redeploying with a bigger budget." If a failed agent left a partial report, summarize what it DID find. Never retry the same proposal more than once — after that, tell the boss plainly what's blocking.
- Never mention tools or files. The boss hears "agent", nothing technical.

## Security scans

If the boss asks for a security scan, "are we compromised", "is this machine hacked", "check this device", or "scan my network":
- For this Mac: silently call security_scan_mac. It's read-only — no confirmation needed.
- For the network / other devices: silently call security_scan_network (deep=true if he wants a thorough sweep).
- Summarize spoken: the verdict first, then the worst finding. "All clear, boss — nothing suspicious in persistence, processes, or connections." or "Found two red flags, boss — something in your launch agents is running out of a temp folder."
- If a finding needs fixing (kill a process, quarantine a launch agent): silently call propose_security_remediation, then ask one short confirmation — "Want me to quarantine it?" Only call confirm_security_remediation after the boss says yes. Cancel on no.
- Phones: you can SEE a phone on the network but cannot scan inside it or remove anything from it — no system can do that remotely. Say so honestly and walk the boss through the guided steps from the scan result (unknown profiles, app review, OS update, password + 2FA, factory reset as last resort).
- First network scan: if there's no trusted baseline yet, suggest "Say the word and I'll mark everything currently connected as trusted" → security_accept_devices.
- Never claim certainty. Findings are indicators; say "worth a look" not "you're hacked".

## Odysseus workspace bridge

Odysseus is the privileged local AI workspace backend. Every Odysseus action, including reads/status checks, needs confirmation:
- First, silently call propose_odysseus with an action from the catalog and params.
- Then ask one short natural confirmation sentence.
- Wait. Do not call confirm_odysseus yet.
- When the boss says yes / go / do it / run it / send it / confirm, silently call confirm_odysseus.
- If the boss cancels, call cancel_odysseus.
- Never use raw URLs for Odysseus. Use only the bridge catalog.

Odysseus owns workspace surfaces:
- If the boss says open Odysseus, open Ody, show notes, show tasks, show memory, show settings, or show research, propose `open.panel` with the matching panel.
- If the boss says close Odysseus, close Ody, hide Odysseus, or go back to FRIDAY, propose `close.panel`.
- If the boss already chose Odysseus for a todo, to-do, task, reminder-like workspace item, or says "add X to my todo list in Odysseus", propose `tasks.create` with `prompt` and a short `name`.
- If the boss already chose Odysseus for a note, propose `notes.create`.
- Do not use local file/workspace directory tools for todo lists or Odysseus notes.
""".strip()


def _known_topics_suffix() -> str:
    """One prompt line listing studied topics so FRIDAY reaches for
    recall_knowledge when they come up. Static per process start —
    list_known_topics / recall_knowledge cover mid-session freshness."""
    try:
        from friday.learning.store import known_topics

        topics = known_topics()
        if not topics:
            return ""
        names = ", ".join(t["topic"] for t in topics[:15] if t.get("topic"))
        if not names:
            return ""
        return (
            "\n\n## Topics already studied\n"
            f"You have background knowledge notes on: {names}. "
            "Silently use recall_knowledge when any of these come up."
        )
    except Exception:
        return ""


SYSTEM_PROMPT = SYSTEM_PROMPT + _known_topics_suffix()


def _apply_persona(prompt: str) -> str:
    """Open-source persona switch: FRIDAY_PERSONA_NAME / FRIDAY_PERSONA_BOSS
    rebrand the prompt without touching the authored text."""
    from friday.config import config

    if config.PERSONA_NAME != "FRIDAY":
        prompt = (
            prompt.replace("F.R.I.D.A.Y. — Fully Responsive Intelligent Digital Assistant for You", config.PERSONA_NAME)
            .replace("FRIDAY", config.PERSONA_NAME)
        )
    if config.PERSONA_BOSS != "Tony Stark":
        prompt = prompt.replace("Tony Stark", config.PERSONA_BOSS)
    return prompt


SYSTEM_PROMPT = _apply_persona(SYSTEM_PROMPT)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger("friday-agent")
logger.setLevel(logging.INFO)


def _emit_desktop_event(event_type: str, **payload: Any) -> None:
    try:
        append_event(event_type, source="voice", **payload)
    except Exception as exc:
        logger.debug("Desktop event emit skipped: %s", exc)


def _chat_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            else:
                text = getattr(item, "text", None)
                if text:
                    chunks.append(str(text))
        return " ".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
    return ""


def _handle_wake_phrase(transcript: str) -> None:
    """If the boss said the wake phrase, launch the desktop HUD immediately.

    No thinking/loading phase — direct open. The LLM produces the wake
    reply naturally on the same turn.
    """
    from friday.desktop.launcher import ensure_desktop_running

    result = ensure_desktop_running()
    logger.info("Wake phrase fired: %s", result)
    _emit_desktop_event(
        "activity",
        tool="wake",
        detail=f"open · phrase={transcript[:80]} · {result.get('status')}",
        kind="ok",
    )


# ---------------------------------------------------------------------------
# Memory intelligence hooks (context injection, capture, reflection)
# ---------------------------------------------------------------------------

_turn_counter = {"n": 0}


def _recent_turns(turn_ctx, limit: int = 14) -> list[str]:
    """Best-effort transcript lines from the LiveKit ChatContext."""
    lines: list[str] = []
    try:
        for item in list(getattr(turn_ctx, "items", []))[-limit:]:
            role = getattr(item, "role", "")
            text = _chat_message_text(item)
            if role and text:
                lines.append(f"[{role}]: {text}")
    except Exception:
        pass
    return lines


def _inject_memory_context(turn_ctx, user_text: str) -> None:
    """Pre-search + profile + events block, added as an assistant-side
    context message. Must never break the voice path."""
    try:
        from friday.memory import context as memory_context

        block = memory_context.build_block(user_text)
        if block:
            turn_ctx.add_message(role="assistant", content=block)
    except Exception as exc:
        logger.debug("memory context injection skipped: %s", exc)


def _capture_and_maybe_reflect(turn_ctx, user_text: str) -> None:
    """Instant preference capture every turn; reflection every Nth."""
    try:
        from friday.memory import feedback, reflect

        feedback.detect_and_save(user_text)
        _turn_counter["n"] += 1
        if _turn_counter["n"] % reflect.reflect_every() == 0:
            transcript = _recent_turns(turn_ctx)
            asyncio.create_task(reflect.run(transcript))
    except Exception as exc:
        logger.debug("preference capture skipped: %s", exc)


def _start_speaking_amplitude_pump(get_state) -> None:
    """Emit ``audio`` events at ~30 Hz while the agent is speaking.

    Without a tap on the TTS audio frames (which would need a custom
    plugin wrapper), this is the cheapest way to make the desktop orb
    *feel* like it's producing the voice in real time: while the
    LiveKit agent is in the ``speaking`` state we emit a synthesized
    amplitude envelope through the existing desktop event log. The
    orb's AudioBus consumes it and drives the sonar rings + equalizer
    in lockstep with the actual TTS playback window — start and stop
    match the audio start and stop because they're keyed off the same
    LiveKit ``agent_state_changed`` event.
    """
    import math
    import random
    import threading
    import time as _time

    def _loop():
        phase = 0.0
        while True:
            try:
                state = get_state()
            except Exception:
                state = None
            if state == "speaking":
                phase += 0.22
                # Mix of low-freq breath + voice-band wobble + jitter.
                breath = 0.55 + 0.35 * math.sin(phase * 0.9)
                voice = 0.5 + 0.5 * math.sin(phase * 3.7)
                jitter = random.uniform(-0.18, 0.18)
                amp = max(0.05, min(1.0, breath * voice + jitter))
                _emit_desktop_event("audio", rms=round(amp, 3))
                _time.sleep(0.033)
            else:
                # One zero so the desktop bus drains promptly, then park.
                _emit_desktop_event("audio", rms=0.0)
                _time.sleep(0.20)

    t = threading.Thread(target=_loop, name="friday-amp-pump", daemon=True)
    t.start()


def _wire_desktop_events(session: AgentSession) -> None:
    # Shared mutable state for the amplitude pump thread.
    _agent_state_box = {"value": "idle"}
    _wake_box = {"awake": False}

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        _agent_state_box["value"] = event.new_state
        _emit_desktop_event("state", state=event.new_state)

    _start_speaking_amplitude_pump(lambda: _agent_state_box["value"])

    @session.on("user_state_changed")
    def _on_user_state(event) -> None:
        if event.new_state == "speaking":
            _emit_desktop_event("state", state="listening")

    @session.on("user_input_transcribed")
    def _on_user_transcript(event) -> None:
        if event.is_final and event.transcript.strip():
            text = event.transcript.strip()
            _emit_desktop_event("chat", role="user", text=text)
            if is_wake_phrase(text) and not _wake_box["awake"]:
                _wake_box["awake"] = True
                _handle_wake_phrase(text)

    @session.on("conversation_item_added")
    def _on_conversation_item(event) -> None:
        item = event.item
        role = getattr(item, "role", "")
        text = _chat_message_text(item)
        if role == "assistant" and text:
            _emit_desktop_event("chat", role="assistant", text=text)

    @session.on("function_tools_executed")
    def _on_tools(event) -> None:
        calls = getattr(event, "function_calls", []) or []
        outputs = getattr(event, "function_call_outputs", []) or []
        for index, call in enumerate(calls):
            name = getattr(call, "name", "tool")
            arguments = getattr(call, "arguments", "")
            output = outputs[index] if index < len(outputs) else None
            detail = str(arguments or "—")
            if output is not None:
                detail = f"{detail} · completed"
            _emit_desktop_event("activity", tool=name, detail=detail[:180], kind="ok")

    @session.on("error")
    def _on_error(event) -> None:
        _emit_desktop_event("error", detail=str(getattr(event, "error", event))[:180])


# ---------------------------------------------------------------------------
# Resolve Windows host IP from WSL
# ---------------------------------------------------------------------------

def _get_windows_host_ip() -> str:
    """Get the Windows host IP by looking at the default network route."""
    try:
        # 'ip route' is the most reliable way to find the 'default' gateway
        # which is always the Windows host in WSL.
        cmd = "ip route show default | awk '{print $3}'"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=2
        )
        ip = result.stdout.strip()
        if ip:
            logger.info("Resolved Windows host IP via gateway: %s", ip)
            return ip
    except Exception as exc:
        logger.warning("Gateway resolution failed: %s. Trying fallback...", exc)

    # Fallback to your original resolv.conf logic if 'ip route' fails
    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                if "nameserver" in line:
                    ip = line.split()[1]
                    logger.info("Resolved Windows host IP via nameserver: %s", ip)
                    return ip
    except Exception:
        pass

    return "127.0.0.1"

def _mcp_server_url() -> str:
    # host_ip = _get_windows_host_ip()
    # url = f"http://{host_ip}:{MCP_SERVER_PORT}/sse"
    # url = f"https://ongoing-colleague-samba-pioneer.trycloudflare.com/sse"
    url = f"http://127.0.0.1:{MCP_SERVER_PORT}/sse"
    logger.info("MCP Server URL: %s", url)
    return url


# ---------------------------------------------------------------------------
# Build provider instances
# ---------------------------------------------------------------------------

def _build_stt():
    if STT_PROVIDER == "sarvam":
        logger.info("STT → Sarvam Saaras v3")
        return sarvam.STT(
            language="unknown",
            model="saaras:v3",
            mode="transcribe",
            flush_signal=True,
            sample_rate=16000,
        )
    elif STT_PROVIDER == "whisper":
        logger.info("STT → OpenAI Whisper")
        return lk_openai.STT(model="whisper-1")
    else:
        raise ValueError(f"Unknown STT_PROVIDER: {STT_PROVIDER!r}")


def _build_llm():
    if LLM_PROVIDER == "openai":
        logger.info("LLM → OpenAI (%s)", OPENAI_LLM_MODEL)
        return lk_openai.LLM(model=OPENAI_LLM_MODEL)
    elif LLM_PROVIDER == "gemini":
        logger.info("LLM → Google Gemini (%s)", GEMINI_LLM_MODEL)
        return lk_google.LLM(model=GEMINI_LLM_MODEL, api_key=os.getenv("GOOGLE_API_KEY"))
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


def _build_tts():
    if TTS_PROVIDER == "sarvam":
        logger.info("TTS → Sarvam Bulbul v3")
        return sarvam.TTS(
            target_language_code=SARVAM_TTS_LANGUAGE,
            model="bulbul:v3",
            speaker=SARVAM_TTS_SPEAKER,
            pace=TTS_SPEED,
        )
    elif TTS_PROVIDER == "openai":
        logger.info("TTS → OpenAI TTS (%s / %s)", OPENAI_TTS_MODEL, OPENAI_TTS_VOICE)
        return lk_openai.TTS(model=OPENAI_TTS_MODEL, voice=OPENAI_TTS_VOICE, speed=TTS_SPEED)
    else:
        raise ValueError(f"Unknown TTS_PROVIDER: {TTS_PROVIDER!r}")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FridayAgent(Agent):
    """
    F.R.I.D.A.Y. – Iron Man-style voice assistant.
    All tools are provided via the MCP server on the Windows host.
    """

    def __init__(self, stt, llm, tts) -> None:
        self._awake = False
        super().__init__(
            instructions=SYSTEM_PROMPT,
            stt=stt,
            llm=llm,
            tts=tts,
            vad=silero.VAD.load(),
            mcp_servers=[
                mcp.MCPServerHTTP(
                    url=_mcp_server_url(),
                    transport_type="sse",
                    client_session_timeout_seconds=30,
                ),
            ],
        )

    async def on_enter(self) -> None:
        """Start silent. Wake phrase opens the session."""
        self._awake = False

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = getattr(new_message, "text_content", "") or _chat_message_text(new_message)
        wake = is_wake_phrase(text)
        if self._awake and wake:
            raise StopResponse()
        if self._awake:
            _inject_memory_context(turn_ctx, text)
            _capture_and_maybe_reflect(turn_ctx, text)
            return
        if wake:
            self._awake = True
            return
        raise StopResponse()


# ---------------------------------------------------------------------------
# LiveKit entry point
# ---------------------------------------------------------------------------

def _turn_detection() -> str:
    return "stt" if STT_PROVIDER == "sarvam" else "vad"


def _endpointing_delay() -> float:
    return {"sarvam": 0.07, "whisper": 0.3}.get(STT_PROVIDER, 0.1)


async def entrypoint(ctx: JobContext) -> None:
    clear_events()
    logger.info(
        "FRIDAY online – room: %s | STT=%s | LLM=%s | TTS=%s",
        ctx.room.name, STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    )

    stt = _build_stt()
    llm = _build_llm()
    tts = _build_tts()

    session = AgentSession(
        turn_detection=_turn_detection(),
        min_endpointing_delay=_endpointing_delay(),
    )
    _wire_desktop_events(session)
    _emit_desktop_event(
        "activity",
        tool="voice_agent",
        detail=f"room={ctx.room.name} STT={STT_PROVIDER} LLM={LLM_PROVIDER} TTS={TTS_PROVIDER}",
        kind="ok",
    )

    await session.start(
        agent=FridayAgent(stt=stt, llm=llm, tts=tts),
        room=ctx.room,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

def dev():
    """Wrapper to run the agent in dev mode automatically."""
    import sys
    # If no command was provided, inject 'dev'
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    main()

if __name__ == "__main__":
    main()
