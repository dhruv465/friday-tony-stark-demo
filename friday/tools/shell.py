"""Shell command MCP tools — propose / confirm broker.

FRIDAY proposes a shell command; the user says yes (in chat); FRIDAY
confirms. Mirrors the propose/confirm pattern used by
``friday.tools.messaging`` so the agent's UX is consistent across
risky surfaces.

Safety layers, top to bottom:

1. **Confirm gate** — ``confirm_shell_command`` must be called after
   ``propose_shell_command`` returns ``pending_confirmation``. The
   boss confirms verbally / textually per his stated UX preference.
2. **Home cwd** — commands run from the user's home folder by default
   so FRIDAY can work across the boss's Mac user space. Override with
   ``FRIDAY_SHELL_CWD``.
3. **Denylist patterns.** Rejection of dangerous
   *argument* fragments (`rm -rf /`, redirects into block devices,
   command substitution syntax, env-var-prefix injection, etc.).
4. **Secret pattern reject** — refuses commands whose text looks
   like it's echoing an API key or token (reuses
   ``friday.tools.desktop.SECRET_PATTERNS``).
5. **Privilege-escalation reject** — sudo/su/doas/pkexec are never
   allowed.
6. **Timeout + capture** — 20s hard timeout, stdout/stderr captured
   and truncated to 4 KB each.

This tool is **not** a substitute for a real sandbox. Confirmed commands
can read and write files the caller can access. Treat
it as direct local user execution with a confirmation broker, not as
containment.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from friday.security import trust
from friday.tools.desktop import SECRET_PATTERNS


PENDING_TTL_SECONDS = 90
PENDING_SHELL_ACTIONS: dict[str, "PendingShell"] = {}

DEFAULT_TIMEOUT_S = 20
MAX_OUTPUT_CHARS = 4000

# Optional argv[0] allowlist. By default FRIDAY can stage most programs
# from /Users/dhruvsmac, but setting FRIDAY_SHELL_REQUIRE_ALLOWLIST=1
# tightens execution back to this list.
DEFAULT_ALLOWLIST = frozenset({
    # File / dir inspection
    "ls", "cat", "head", "tail", "wc", "stat", "file", "tree",
    "pwd", "echo", "date", "uname", "hostname", "whoami",
    # Search
    "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "locate",
    # VCS (read-only)
    "git",
    # Python toolchain (read-only)
    "uv", "pip", "pipx",
    # Project tooling (read-only)
    "make", "npm", "pnpm", "yarn",
    # Inspection
    "which", "type", "df", "du", "lsof", "ps", "top",
    # Misc text
    "diff", "sort", "uniq", "cut", "awk", "sed",
    "tr", "tee", "column", "jq", "yq",
})

# Argv[0] hard-block for privilege escalation. This wins over optional
# allowlist settings unconditionally.
PRIVILEGE_BLOCKED_PROGS = frozenset({
    "sudo", "doas", "pkexec", "su",
})

DENYLIST_PATTERNS = (
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\brm\s+(-[a-z]*r[a-z]*\s+)?(/\s*|/\s*$|/\s+[a-z*])", re.IGNORECASE),
    re.compile(r"\brm\s+-rf?\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r":\(\)\s*\{", re.IGNORECASE),               # :(){:|:&};:
    re.compile(r">\s*/dev/(sd|nvme|disk)", re.IGNORECASE),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.IGNORECASE),
    re.compile(r"\bchown\b|\bchmod\b\s+777", re.IGNORECASE),
    re.compile(r"\bcurl\b[^|]*\|\s*(sh|bash|zsh)\b", re.IGNORECASE),
    re.compile(r"\bwget\b[^|]*\|\s*(sh|bash|zsh)\b", re.IGNORECASE),
    # Command substitution and process subs that shlex wouldn't catch.
    re.compile(r"\$\("),
    re.compile(r"<\("),
)


def _effective_allowlist() -> frozenset[str]:
    raw = os.getenv("FRIDAY_SHELL_ALLOWLIST", "").strip()
    if not raw:
        return DEFAULT_ALLOWLIST
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    # Privilege escalation can never enter the allowlist via env.
    return frozenset(parts - PRIVILEGE_BLOCKED_PROGS)


def _normalised_prog(argv0: str) -> str:
    """Return the basename of argv[0], stripped, lowercased."""
    if not argv0:
        raise ValueError("Empty program.")
    if "/" in argv0 or "\\" in argv0:
        raise PermissionError(
            "Reference the program by name, not a path — `git`, not `/usr/bin/git`."
        )
    if "=" in argv0:
        # Bash-style env-var prefix: FOO=bar cmd. shlex puts the whole
        # `FOO=bar` chunk in argv[0]; we never want to execute that.
        raise PermissionError("Environment-variable prefixes aren't allowed in shell commands.")
    return argv0.strip().lower()


@dataclass
class PendingShell:
    action_id: str
    command: str
    cwd: str
    reason: str
    created_at: float


def _reject_secret_command(command: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(command):
            raise ValueError("Refusing to run a command that looks like it embeds a secret.")


def _reject_denylisted(command: str) -> None:
    stripped = command.strip()
    if not stripped:
        raise ValueError("Empty command.")
    for pat in DENYLIST_PATTERNS:
        if pat.search(stripped):
            raise PermissionError(
                "That command is on the hard-refusal list, boss — not happening."
            )
    # No shell metacharacters. Even though we exec via argv (shell=False),
    # we still refuse these so the LLM can't trick the boss into
    # confirming what looks like a single command but is actually two.
    if any(token in stripped for token in (";", "&&", "||", "`", "$(", ">&", "|", ">", "<")):
        raise ValueError(
            "Chained / piped / redirected commands aren't supported. "
            "Propose one command at a time."
        )


def _enforce_allowlist(argv0: str) -> str:
    prog = _normalised_prog(argv0)
    if prog in PRIVILEGE_BLOCKED_PROGS:
        raise PermissionError(
            f"`{prog}` is hard-blocked — privilege escalation is never allowed."
        )
    if os.getenv("FRIDAY_SHELL_REQUIRE_ALLOWLIST", "").strip().lower() not in {"1", "true", "yes"}:
        return prog
    allow = _effective_allowlist()
    if prog not in allow:
        raise PermissionError(
            f"`{prog}` isn't on the shell allowlist. Allowed: {sorted(allow)[:14]}…"
        )
    return prog


def _prune_expired() -> None:
    now = time.time()
    expired = [
        aid for aid, act in PENDING_SHELL_ACTIONS.items()
        if now - act.created_at > PENDING_TTL_SECONDS
    ]
    for aid in expired:
        PENDING_SHELL_ACTIONS.pop(aid, None)


def _latest_pending_id() -> str:
    _prune_expired()
    if not PENDING_SHELL_ACTIONS:
        raise KeyError("No pending shell command.")
    return max(
        PENDING_SHELL_ACTIONS.values(),
        key=lambda a: a.created_at,
    ).action_id


def _truncate(s: str) -> str:
    if len(s) <= MAX_OUTPUT_CHARS:
        return s
    return s[:MAX_OUTPUT_CHARS] + f"\n…[truncated {len(s) - MAX_OUTPUT_CHARS} chars]"


def _resolve_cwd() -> str:
    """Return the working directory for confirmed shell commands."""
    override = os.getenv("FRIDAY_SHELL_CWD", "").strip()
    if override:
        target = Path(override).expanduser().resolve()
    else:
        target = Path.home().resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(f"Cannot access shell working dir: {exc}")
    return str(target)


def propose_shell(command: str, reason: str = "") -> dict:
    """Validate + register a pending shell command. Does NOT run it."""
    command = command.strip()
    _reject_secret_command(command)
    _reject_denylisted(command)
    # Try a dry parse so we fail early on malformed quoting.
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Could not parse command: {exc}")
    if not argv:
        raise ValueError("Empty command.")

    # Allowlist + hard-block gate. Final defence before we stage anything.
    prog = _enforce_allowlist(argv[0])

    action_id = uuid.uuid4().hex
    cwd = _resolve_cwd()
    PENDING_SHELL_ACTIONS[action_id] = PendingShell(
        action_id=action_id,
        command=command,
        cwd=cwd,
        reason=reason.strip(),
        created_at=time.time(),
    )
    # Tier 1: trust mode runs the validated command immediately. The
    # denylist / allowlist / secret gates above already ran — trust mode
    # never widens WHAT can run, only skips the confirm round-trip.
    trusted = trust.trusted_short_circuit(confirm_shell, action_id)
    if trusted is not None:
        return trusted

    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "command": command,
        "program": prog,
        "cwd": cwd,
        "reason": reason.strip(),
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }


def confirm_shell(action_id: str | None = None) -> dict:
    """Run the most recent pending command (or the one identified by id)."""
    if not action_id:
        action_id = _latest_pending_id()

    action = PENDING_SHELL_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending shell command: {action_id}")
    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_SHELL_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending shell command expired: {action_id}")

    argv = shlex.split(action.command)
    # Re-validate at confirm time. If the allowlist tightened between
    # propose and confirm, the staged command is rejected.
    try:
        _enforce_allowlist(argv[0])
        _reject_denylisted(action.command)
    except (PermissionError, ValueError) as exc:
        PENDING_SHELL_ACTIONS.pop(action_id, None)
        return {
            "status": "blocked",
            "action_id": action_id,
            "command": action.command,
            "error": str(exc),
        }
    start = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=action.cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_S,
            check=False,
        )
        out, err, rc = result.stdout, result.stderr, result.returncode
        timed_out = False
    except FileNotFoundError as exc:
        PENDING_SHELL_ACTIONS.pop(action_id, None)
        return {
            "status": "error",
            "action_id": action_id,
            "command": action.command,
            "error": f"Command not found: {exc.filename}",
            "duration_s": round(time.monotonic() - start, 3),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
        err = exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        rc = -1
        timed_out = True
    finally:
        PENDING_SHELL_ACTIONS.pop(action_id, None)

    return {
        "status": "executed" if not timed_out else "timeout",
        "action_id": action_id,
        "command": action.command,
        "cwd": action.cwd,
        "returncode": rc,
        "stdout": _truncate(out or ""),
        "stderr": _truncate(err or ""),
        "duration_s": round(time.monotonic() - start, 3),
        "timed_out": timed_out,
    }


def cancel_shell(action_id: str | None = None) -> dict:
    if not action_id:
        try:
            action_id = _latest_pending_id()
        except KeyError:
            return {"status": "noop", "reason": "no pending shell command"}
    removed = PENDING_SHELL_ACTIONS.pop(action_id, None)
    return {
        "status": "cancelled" if removed else "noop",
        "action_id": action_id,
    }


def register(mcp):
    @mcp.tool()
    def propose_shell_command(command: str, reason: str = "") -> dict:
        """
        Stage a shell command for the boss to confirm. Use BEFORE running
        anything. Returns a pending action_id; call confirm_shell_command
        after the boss says yes / go / run it.
        """
        return propose_shell(command=command, reason=reason)

    @mcp.tool()
    def confirm_shell_command(action_id: str | None = None) -> dict:
        """
        Run the most recently proposed shell command. Captures stdout +
        stderr (truncated), 20s hard timeout, runs from WORKSPACE_ROOTS[0].
        """
        return confirm_shell(action_id=action_id)

    @mcp.tool()
    def cancel_shell_command(action_id: str | None = None) -> dict:
        """Drop a pending shell proposal without running it."""
        return cancel_shell(action_id=action_id)
