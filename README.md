# F.R.I.D.A.Y. — Tony Stark Demo

> *"Fully Responsive Intelligent Digital Assistant for You"*

A Tony Stark-inspired AI assistant split into two cooperating pieces:

| Component | What it is |
|-----------|-----------|
| **MCP Server** (`uv run friday`) | A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes tools (news, web search, system info, …) over SSE. Think of it as the Stark Industries backend — it does the actual work. |
| **Voice Agent** (`uv run friday_voice`) | A [LiveKit Agents](https://github.com/livekit/agents) voice pipeline that listens to your microphone, reasons with an LLM (OpenAI `gpt-4o` by default), and speaks back with OpenAI TTS — all while pulling tools from the MCP server in real time. |
| **Desktop App** (`uv run friday_desktop`) | A frameless PySide6 window with animated FRIDAY orb, chat transcript, and live operational feed. It can chat directly, and it mirrors live voice-agent transcript/state/tool events. |

Demo: [Instagram reel](https://www.instagram.com/p/DW2HjYtkwg_/)

[![Demo Video Guide](https://img.youtube.com/vi/mMY9swqe3BI/maxresdefault.jpg)](https://www.youtube.com/watch?v=mMY9swqe3BI)

---

## How it works

```
Microphone ──► STT (Sarvam Saaras v3)
                    │
                    ▼
             LLM (OpenAI gpt-4o)     ◄──────► MCP Server (FastMCP / SSE)
                    │                              ├─ get_world_news
                    ▼                              ├─ open_world_monitor
             TTS (OpenAI nova)                     ├─ search_web
                    │                              └─ …more tools
                    ▼
             Speaker / LiveKit room
```

The voice agent connects to the MCP server via SSE at `http://127.0.0.1:8000/sse` (auto-resolved to the Windows host IP when running inside WSL).

---

## Project structure

```
friday-tony-stark-demo/
├── server.py           # uv run friday  → starts the MCP server (SSE on :8000)
├── agent_friday.py     # uv run friday_voice → starts the LiveKit voice agent
├── pyproject.toml
├── .env.example        # copy → .env and fill in your keys
│
└── friday/             # MCP server package
    ├── config.py       # env-var loading & app-wide settings
    ├── tools/          # MCP tools (callable by the LLM)
    │   ├── web.py      # search_web, fetch_url, get_world_news, open_world_monitor
    │   ├── desktop.py  # browser opening, safe file IO, memory notes
    │   ├── mac_worker.py # app launching, screen vision, guarded click/type/keys
    │   ├── messaging.py  # confirmed Messages/WhatsApp/Slack/email actions
    │   ├── memory.py   # Obsidian-backed profile/facts/projects/preferences tools
    │   ├── diagnostics.py # CPU/memory/disk/process/network diagnostics
    │   ├── system.py   # get_current_time, get_system_info
    │   └── utils.py    # format_json, word_count
    ├── memory/         # Obsidian vault helpers
    ├── desktop/        # PySide6 desktop UI
    ├── prompts/        # MCP prompt templates (summarize, explain_code, …)
    └── resources/      # MCP resources exposed to clients (friday://info)
```

---

## Quick start

### 1. Prerequisites

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv) — `pip install uv` or `curl -Lsf https://astral.sh/uv/install.sh | sh`
- A [LiveKit Cloud](https://cloud.livekit.io) project (free tier works)

### 2. Clone & install

```bash
git clone https://github.com/SAGAR-TAMANG/friday-tony-stark-demo.git
cd friday-tony-stark-demo
uv sync          # creates .venv and installs all dependencies
```

### 3. Set up environment

```bash
cp .env.example .env
# Open .env and fill in your API keys (see the section below)
```

### 4. Run — two terminals

**Terminal 1 — MCP server** (must start first)

```bash
uv run friday
```

Starts the FastMCP server on `http://127.0.0.1:8000/sse`. The voice agent connects here to fetch its tools.

**Terminal 2 — Voice agent**

```bash
uv run friday_voice
```

Starts the LiveKit voice agent in **dev mode** — it joins a LiveKit room and begins listening. Open the [LiveKit Agents Playground](https://agents-playground.livekit.io) and connect to your room to talk to FRIDAY.

---

## `uv run friday` vs `uv run friday_voice`

| Command | Entry point | What it does |
|---------|------------|--------------|
| `uv run friday` | `server.py → main()` | Launches the **FastMCP server** over SSE transport on port 8000. This is the "brain backend" — it registers all tools, prompts, and resources that the LLM can call. |
| `uv run friday_voice` | `agent_friday.py → dev()` | Launches the **LiveKit voice agent**. It builds the STT / LLM / TTS pipeline, connects to your LiveKit room, and wires up the MCP server as a tool source. The `dev()` wrapper auto-injects the `dev` CLI flag so you don't have to type it manually. |
| `uv run friday_desktop` | `friday.desktop.app → main()` | Launches the desktop UI. It can run standalone, and when `uv run friday_voice` is active it mirrors live voice transcript, state, and tool events. |

> Both processes must run **simultaneously**. The voice agent calls the MCP server in real time whenever it needs a tool (e.g. fetching news).

---

## Environment variables

Copy `.env.example` → `.env` and fill in the values below.

| Variable | Required | Where to get it |
|----------|----------|----------------|
| `LIVEKIT_URL` | ✅ | [LiveKit Cloud dashboard](https://cloud.livekit.io) → your project URL |
| `LIVEKIT_API_KEY` | ✅ | LiveKit Cloud → API Keys |
| `LIVEKIT_API_SECRET` | ✅ | LiveKit Cloud → API Keys |
| `GROQ_API_KEY` | optional | [console.groq.com](https://console.groq.com) — only needed if you switch `LLM_PROVIDER` to `"groq"` |
| `SARVAM_API_KEY` | ✅ (default STT) | [dashboard.sarvam.ai](https://dashboard.sarvam.ai) |
| `OPENAI_API_KEY` | ✅ (default LLM + TTS) | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `DEEPGRAM_API_KEY` | optional | [console.deepgram.com](https://console.deepgram.com) |
| `GOOGLE_APPLICATION_CREDENTIALS` | optional | GCP service-account JSON path — only for `STT_PROVIDER = "google"` |
| `GOOGLE_API_KEY` | optional | [aistudio.google.com](https://aistudio.google.com/projects) — only needed if you switch `LLM_PROVIDER` to `"gemini"` |
| `OBSIDIAN_VAULT_PATH` | optional | Path to the Obsidian/SecBrain vault used by memory tools |
| `MEMORY_FOLDER` | optional | Defaults to `Memory`; folder created inside the vault for imported memory tools |
| `WORKSPACE_ROOTS` | optional | Colon- or comma-separated folders the desktop file tools may read/write |
| `OPENAI_VISION_MODEL` | optional | Defaults to `gpt-4o`; used by screen description tools |
| `FRIDAY_REQUIRE_CONFIRMATION` | optional | Defaults to `true`; risky desktop actions require confirmation |
| `FRIDAY_SCREENSHOT_DIR` | optional | Defaults to `/tmp/friday-screens`; stores temporary screenshots |
| `FRIDAY_DESKTOP_EVENT_LOG` | optional | Defaults to `/tmp/friday-desktop-events.jsonl`; live bridge between voice agent and desktop UI |
| `SUPABASE_URL` | optional | [supabase.com](https://supabase.com) — for the ticketing tool |
| `SUPABASE_API_KEY` | optional | Supabase project → API settings |
| `FRIDAY_PERSONA_NAME` / `FRIDAY_PERSONA_BOSS` | optional | Rebrand the assistant persona (FRIDAY/Stark naming is Marvel IP — change before public deployments) |
| `FRIDAY_TRUST_DEFAULT_MINUTES` | optional | Trust-mode arm window when no duration is spoken (default 30, max 240) |
| `FRIDAY_SUBAGENT_MODEL` | optional | LLM for background agents (default `gpt-4o-mini`) |
| `FRIDAY_EXECUTOR_MAX_STEPS_QUICK` / `FRIDAY_EXECUTOR_MAX_STEPS_DEEP` | optional | Step budgets for quick (15) and deep (40) agent jobs |
| `FRIDAY_EXECUTOR_FILE_ROOTS` | optional | Comma-separated extra folders agents may read/write besides their own workspace |
| `FRIDAY_EXECUTOR_PAUSE_MAX_S` | optional | How long a paused agent waits for trust mode to be re-armed (default 600s) |
| `FRIDAY_SCHEDULER_TICK_S` | optional | Scheduler poll interval for delayed/recurring agents (default 30s) |
| `FRIDAY_KNOWLEDGE_DIR` | optional | Folder for learning jobs, agent workspaces, and trust state (default `~/.friday/knowledge`) |

---

## Mac automation permissions

For the human-worker tools, macOS may prompt for:

- **Screen Recording** — needed for `describe_screen`.
- **Accessibility** — needed for confirmed click/type/key actions.
- **Automation** — needed for Messages/System Events/WhatsApp control.

Risky actions are prepared first and require confirmation before execution.

Messages use the same confirmation broker: `prepare_message` creates a pending action, then `confirm_message_action` sends only after explicit confirmation. For named Apple Messages recipients, `prepare_message` searches Contacts first and includes the resolved contact in the preview; ambiguous or missing contacts do not create a pending send. WhatsApp resolves Contacts phone numbers first, then sends through the WhatsApp URL flow after confirmation; if no phone is available, it can use a confirmed WhatsApp app search. If no `action_id` is passed, confirmation executes the newest pending message, so a natural "yes, send it" works.

The LiveKit voice agent starts asleep and stays silent. Say `wake up, daddy's home` or `daddy's home` to wake FRIDAY; normal speech before that is ignored.

---

## Desktop app

Run:

```bash
uv run friday_desktop
```

This opens a local FRIDAY window with animated orb, transcript, text input, and tool-call activity feed. It works standalone, and also mirrors the live voice agent when `uv run friday_voice` is running.

---

## Trust mode & background agents

Friday v2 turns the assistant into a permissioned executor:

- **Trust mode** — say *"trust mode on"* and Tier 1 actions (shell commands,
  notes/reminders, deploying agents) run immediately instead of asking for
  per-action confirmation. It auto-expires (default 30 min) and *"trust mode
  off"* disarms instantly. Hard floor: messaging other people, email, and
  security remediation **always** require explicit confirmation, even armed.
  All shell hardening (denylist, no sudo, no pipes/chaining) still applies —
  trust mode skips the confirmation round-trip, never the validation.
- **Background agents** — *"create an agent to compare X and Y"* deploys a
  worker that researches in the background and leaves a markdown report.
  Agents deployed while trust mode is armed also get hands: `run_shell`,
  workspace file writes, Apple notes/reminders, and message *staging* (sends
  still need your spoken yes). If trust expires mid-job the agent pauses and
  waits for re-arm.
- **Scheduling & monitors** — *"do this at 6pm"* parks a one-shot job;
  *"keep an eye on X, check every hour"* creates a recurring monitor that
  re-runs, diffs its report against the previous run, and only notifies
  (HUD + macOS notification + spoken on next interaction) when something
  actually changed.
- **Learning from outcomes** — every finished job writes a searchable outcome
  note; future agents with similar tasks get those lessons injected into
  their prompt.

Agent state lives under `FRIDAY_KNOWLEDGE_DIR/_agents/<slug>/` (report,
workspace, outcome note); trust state under `_trust/state.json`.

---

## Memory, diagnostics, and web

The merged project includes two memory surfaces:

- `remember_in_obsidian` / `search_obsidian_memory` store explicit notes under the SecBrain project folder.
- `save_memory`, `update_profile`, `recall_recent`, `search_memory`, `read_note`, and `list_notes` use an imported `Memory/` folder inside `OBSIDIAN_VAULT_PATH`.

Diagnostics tools are read-only:

- `run_diagnostics`
- `top_processes`
- `network_scan`

`search_web` now uses DuckDuckGo HTML results instead of returning a stub.

---

## Switching providers

Open `agent_friday.py` and change the provider constants at the top:

```python
STT_PROVIDER = "sarvam"   # "sarvam" | "whisper"
LLM_PROVIDER = "openai"   # "openai" | "gemini"
TTS_PROVIDER = "openai"   # "openai" | "sarvam"
```

---

## Adding a new tool

1. Create or open a file in `friday/tools/`
2. Define a `register(mcp)` function and decorate tools with `@mcp.tool()`
3. Import and call `register(mcp)` inside `friday/tools/__init__.py`

The MCP server will pick it up on next start.

---

## Tech stack

- **[FastMCP](https://github.com/jlowin/fastmcp)** — MCP server framework
- **[LiveKit Agents](https://github.com/livekit/agents)** — real-time voice pipeline
- **Sarvam Saaras v3** — STT (Indian-English optimised)
- **OpenAI** (`gpt-4o`) — LLM
- **OpenAI TTS** (`nova` voice) — TTS
- **[uv](https://github.com/astral-sh/uv)** — fast Python package manager

---

## License

MIT — see [LICENSE](LICENSE).
