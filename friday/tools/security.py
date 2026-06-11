"""Security suite MCP tools — Mac audit, LAN scan, gated remediation.

FRIDAY v3 phase 2 (agreed realistic scope):

1. **Mac audit** (`security_scan_mac`) — read-only sweep of the persistence
   and runtime surfaces malware actually uses on macOS without root:
   LaunchAgents/LaunchDaemons plists, crontab, suspicious process
   locations, deleted-executable processes, /etc/hosts tampering, odd
   listeners and connections to known-bad ports. No state changes.
2. **Network scan** (`security_scan_network`) — discover devices on the
   LAN (ARP table, optional async TCP probe of common ports) and flag
   devices that aren't in the accepted baseline. FRIDAY can see a phone
   ON the network but cannot scan INSIDE it — iOS/Android sandboxing.
   For a suspect phone the report includes guided remediation steps
   instead of pretending to clean it remotely.
3. **Remediation broker** — anything that changes state (kill a process,
   unload + quarantine a launch agent) goes through the same
   propose/confirm dance as ``friday.tools.shell``. Quarantine moves the
   plist into ``~/.friday-quarantine/<ts>/`` — reversible by moving it
   back, never deleted.

The device baseline lives at ``<FRIDAY_KNOWLEDGE_DIR>/_security/`` —
machine-managed state, ignored by the learning engine (no job.json).
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - declared dep, guard anyway
    psutil = None

from friday.security import trust


PENDING_TTL_SECONDS = 120
PENDING_SECURITY_ACTIONS: dict[str, "PendingRemediation"] = {}

REMEDIATION_ACTIONS = ("kill_process", "quarantine_launch_agent")

# Persistence dirs reachable without sudo. /System is SIP-protected, skip.
LAUNCH_DIRS = (
    "~/Library/LaunchAgents",
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
)

# Executables living here are a classic unsigned-malware tell.
SUSPICIOUS_EXEC_DIRS = (
    "/tmp",
    "/private/tmp",
    "/Users/Shared",
    str(Path.home() / "Downloads"),
)

# Hidden dot-folders that are normal developer/app tooling, not malware.
# A hidden folder NOT on this list is only ever a "warn" — plenty of legit
# tools live in dot-dirs, so hidden alone is never "high".
KNOWN_DOTDIR_TOOLING = frozenset({
    ".local", ".bun", ".cargo", ".rustup", ".nvm", ".npm", ".pnpm", ".yarn",
    ".deno", ".pyenv", ".rbenv", ".gem", ".go", ".gradle", ".m2", ".sdkman",
    ".asdf", ".volta", ".dotnet", ".composer", ".docker", ".orbstack",
    ".vscode", ".vscode-insiders", ".cursor", ".codeium", ".claude",
    ".ollama", ".homebrew", ".cache", ".venv", ".virtualenvs", ".poetry",
    ".rvm", ".krew", ".tfenv", ".fnm",
})

# Common C2 / backdoor defaults. Heuristic, not proof.
SUSPICIOUS_REMOTE_PORTS = {1337, 4444, 5555, 6666, 6667, 31337}

COMMON_PROBE_PORTS = (22, 80, 443, 445, 3389, 5900, 8080, 62078)

DEFAULT_HOSTS_ENTRIES = {
    ("127.0.0.1", "localhost"),
    ("255.255.255.255", "broadcasthost"),
    ("::1", "localhost"),
}

RECENT_PERSISTENCE_DAYS = 7

_ARP_LINE = re.compile(
    r"^(?P<name>\S+)\s+\((?P<ip>[0-9.]+)\)\s+at\s+(?P<mac>[0-9a-f:]+|\(incomplete\))",
    re.IGNORECASE,
)

PHONE_GUIDANCE = (
    "Phones can't be scanned from outside — the OS sandbox blocks it. "
    "Guided steps for a suspect phone: 1) check Settings > General > VPN & "
    "Device Management for unknown profiles and remove them, 2) review "
    "installed apps and delete anything unrecognized, 3) update the OS, "
    "4) change Apple ID / Google account password + enable 2FA, 5) as a "
    "last resort do a factory reset and restore only from a clean backup."
)


@dataclass
class PendingRemediation:
    action_id: str
    action: str
    target: str
    reason: str
    created_at: float


@dataclass
class Finding:
    severity: str  # "info" | "warn" | "high"
    category: str
    detail: str
    remediation: str = ""

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def _need_psutil() -> str | None:
    if psutil is None:
        return "Security subsystem offline, boss — psutil isn't installed."
    return None


# ---------------------------------------------------------------------------
# Mac audit collectors (all read-only)
# ---------------------------------------------------------------------------

def _plist_program(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return ""
    program = data.get("Program")
    if not program:
        args = data.get("ProgramArguments") or []
        program = args[0] if args else ""
    return str(program or "")


def _path_suspicion(path: str) -> str | None:
    """Return "high", "warn", or None for an executable path.

    high — runs out of /tmp, /Users/Shared, or ~/Downloads (classic
    unsigned-malware drop zones). warn — runs from a hidden dot-folder
    that isn't recognized developer tooling. Hidden alone is never high:
    uv, bun, vscode, cargo and friends all live in dot-dirs.
    """
    if not path:
        return None
    expanded = str(Path(path).expanduser())
    if any(expanded.startswith(d + "/") or expanded == d for d in SUSPICIOUS_EXEC_DIRS):
        return "high"
    for part in Path(expanded).parts:
        if part.startswith(".") and part not in (".", "..") and part not in KNOWN_DOTDIR_TOOLING:
            return "warn"
    return None


def audit_launch_items(dirs: tuple[str, ...] = LAUNCH_DIRS) -> list[Finding]:
    findings: list[Finding] = []
    now = time.time()
    for raw in dirs:
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            continue
        for plist in sorted(folder.glob("*.plist")):
            program = _plist_program(plist)
            recent = False
            try:
                recent = now - plist.stat().st_mtime < RECENT_PERSISTENCE_DAYS * 86400
            except OSError:
                pass
            suspicion = _path_suspicion(program)
            if suspicion:
                findings.append(
                    Finding(
                        suspicion,
                        "persistence",
                        f"Launch item {plist} runs {program!r} from a suspicious location.",
                        f"quarantine_launch_agent target={plist}",
                    )
                )
            elif recent:
                findings.append(
                    Finding(
                        "warn",
                        "persistence",
                        f"Launch item {plist} was added/modified in the last "
                        f"{RECENT_PERSISTENCE_DAYS} days (runs {program or 'unknown'}).",
                        f"quarantine_launch_agent target={plist} if unrecognized",
                    )
                )
    return findings


def audit_crontab() -> list[Finding]:
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    findings = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        severity = "high" if ("curl" in line or "/tmp/" in line) else "warn"
        findings.append(
            Finding(
                severity,
                "persistence",
                f"Crontab entry: {line[:160]}",
                "remove with `crontab -e` if unrecognized",
            )
        )
    return findings


def audit_processes() -> list[Finding]:
    if psutil is None:
        return []
    findings = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = proc.info["exe"] or ""
            name = proc.info["name"] or "?"
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        suspicion = _path_suspicion(exe)
        if suspicion:
            findings.append(
                Finding(
                    suspicion,
                    "process",
                    f"Process {name} (pid {pid}) runs from suspicious path {exe}.",
                    f"kill_process target={pid}",
                )
            )
        elif exe.endswith(" (deleted)"):
            findings.append(
                Finding(
                    "high",
                    "process",
                    f"Process {name} (pid {pid}) executable was deleted from disk.",
                    f"kill_process target={pid}",
                )
            )
    return findings


def audit_connections() -> list[Finding]:
    if psutil is None:
        return []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return [
            Finding(
                "info",
                "network",
                "Connection table needs elevated access — skipped.",
            )
        ]
    findings = []
    for conn in conns:
        if conn.status == "ESTABLISHED" and conn.raddr:
            if conn.raddr.port in SUSPICIOUS_REMOTE_PORTS:
                name = "?"
                if conn.pid:
                    try:
                        name = psutil.Process(conn.pid).name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                findings.append(
                    Finding(
                        "high",
                        "network",
                        f"{name} (pid {conn.pid or '—'}) connected to "
                        f"{conn.raddr.ip}:{conn.raddr.port} — port matches known "
                        "backdoor defaults.",
                        f"kill_process target={conn.pid}" if conn.pid else "",
                    )
                )
    return findings


def audit_hosts_file(path: str = "/etc/hosts") -> list[Finding]:
    findings = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entry = (parts[0], parts[1])
        if entry not in DEFAULT_HOSTS_ENTRIES:
            severity = "warn"
            detail = f"Non-default /etc/hosts entry: {line[:120]}"
            # Redirecting security vendors / Apple domains is a malware tell.
            if any(s in line for s in ("apple.com", "icloud.com", "google.com", "microsoft.com")):
                severity = "high"
                detail += " — redirects a major vendor domain."
            findings.append(Finding(severity, "hosts", detail, "review and remove if unrecognized"))
    return findings


def run_mac_audit() -> dict:
    findings = (
        audit_launch_items()
        + audit_crontab()
        + audit_processes()
        + audit_connections()
        + audit_hosts_file()
    )
    counts = {"high": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    verdict = (
        "compromise indicators found"
        if counts["high"]
        else ("items worth reviewing" if counts["warn"] else "clean")
    )
    _save_last_scan(
        "mac",
        {
            "verdict": verdict,
            "counts": counts,
            "top_findings": [
                f.detail for f in findings if f.severity in ("high", "warn")
            ][:5],
        },
    )
    return {
        "status": "ok",
        "verdict": verdict,
        "counts": counts,
        "findings": [f.as_dict() for f in findings],
        "scanned": ["launch items", "crontab", "processes", "connections", "/etc/hosts"],
        "note": "Read-only audit, no sudo: system daemons under SIP were not inspected.",
    }


# ---------------------------------------------------------------------------
# LAN scan
# ---------------------------------------------------------------------------

def parse_arp_table(text: str) -> list[dict]:
    devices = []
    for line in text.splitlines():
        match = _ARP_LINE.match(line.strip())
        if not match:
            continue
        mac = match.group("mac").lower()
        if mac == "(incomplete)":
            continue
        ip = match.group("ip")
        if ip.endswith(".255") or ip.startswith("224.") or ip.startswith("239."):
            continue  # broadcast / multicast noise
        devices.append({"ip": ip, "mac": mac, "name": match.group("name")})
    return devices


def _arp_devices() -> list[dict]:
    try:
        result = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_arp_table(result.stdout)


async def _probe_port(ip: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _probe_device(ip: str, ports: tuple[int, ...]) -> list[int]:
    results = await asyncio.gather(*(_probe_port(ip, p) for p in ports))
    return [port for port, open_ in zip(ports, results) if open_]


def _security_dir() -> Path:
    from friday.learning.store import knowledge_root

    folder = knowledge_root() / "_security"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _baseline_path() -> Path:
    return _security_dir() / "known_devices.json"


def _save_last_scan(kind: str, summary: dict) -> None:
    """Persist the latest scan summary for the HUD ops widgets. Best-effort."""
    try:
        path = _security_dir() / "last_scan.json"
        data: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        summary["at"] = datetime.now().isoformat(timespec="seconds")
        data[kind] = summary
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _load_baseline() -> dict:
    path = _baseline_path()
    if not path.is_file():
        return {"macs": {}, "accepted_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"macs": {}, "accepted_at": None}


def _save_baseline(baseline: dict) -> None:
    path = _baseline_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def run_network_scan(deep: bool = False) -> dict:
    devices = _arp_devices()
    baseline = _load_baseline()
    known = baseline.get("macs", {})
    has_baseline = bool(known)

    for device in devices:
        device["known"] = device["mac"] in known
        if device["known"]:
            device["label"] = known[device["mac"]]
        try:
            device["hostname"] = socket.getfqdn(device["ip"])
        except OSError:
            device["hostname"] = device["ip"]

    if deep:
        probes = await asyncio.gather(
            *(_probe_device(d["ip"], COMMON_PROBE_PORTS) for d in devices)
        )
        for device, open_ports in zip(devices, probes):
            device["open_ports"] = open_ports

    unknown = [d for d in devices if not d["known"]]
    _save_last_scan(
        "network",
        {
            "device_count": len(devices),
            "unknown_count": len(unknown) if has_baseline else 0,
        },
    )
    return {
        "status": "ok",
        "device_count": len(devices),
        "devices": devices,
        "unknown_devices": unknown if has_baseline else [],
        "baseline_accepted": has_baseline,
        "baseline_hint": (
            None
            if has_baseline
            else "No trusted baseline yet — run security_accept_devices once on a "
            "day you trust everything on the network."
        ),
        "phone_guidance": PHONE_GUIDANCE,
        "note": "ARP-table discovery; devices in deep sleep may not appear.",
    }


def accept_devices(labels: dict | None = None) -> dict:
    devices = _arp_devices()
    if not devices:
        return {"status": "error", "error": "No devices visible in the ARP table."}
    baseline = _load_baseline()
    macs = baseline.get("macs", {})
    labels = labels or {}
    for device in devices:
        macs[device["mac"]] = labels.get(device["mac"]) or labels.get(device["ip"]) or device["name"]
    baseline["macs"] = macs
    baseline["accepted_at"] = datetime.now().isoformat(timespec="seconds")
    _save_baseline(baseline)
    return {"status": "ok", "trusted_devices": len(macs)}


# ---------------------------------------------------------------------------
# Remediation broker (propose / confirm, mirrors friday.tools.shell)
# ---------------------------------------------------------------------------

def _prune_expired() -> None:
    now = time.time()
    expired = [
        aid for aid, act in PENDING_SECURITY_ACTIONS.items()
        if now - act.created_at > PENDING_TTL_SECONDS
    ]
    for aid in expired:
        PENDING_SECURITY_ACTIONS.pop(aid, None)


def _latest_pending_id() -> str:
    _prune_expired()
    if not PENDING_SECURITY_ACTIONS:
        raise KeyError("No pending security remediation.")
    return max(
        PENDING_SECURITY_ACTIONS.values(), key=lambda a: a.created_at
    ).action_id


def clear_pending_security_actions() -> None:
    PENDING_SECURITY_ACTIONS.clear()


def propose_remediation(action: str, target: str, reason: str = "") -> dict:
    if action not in REMEDIATION_ACTIONS:
        raise ValueError(
            f"Unknown remediation action {action!r}. Allowed: {REMEDIATION_ACTIONS}"
        )
    target = target.strip()
    if not target:
        raise ValueError("Empty remediation target.")
    if action == "kill_process":
        pid = int(target)  # raises ValueError on junk
        if pid <= 1 or pid == os.getpid():
            raise PermissionError("Refusing to target that pid.")
    if action == "quarantine_launch_agent":
        allowed_roots = [Path(d).expanduser().resolve() for d in LAUNCH_DIRS]
        try:
            # Resolve symlinks and ".." BEFORE the containment check so a
            # crafted path can't escape the launch item folders.
            path = Path(target).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"No such launch item: {target}") from exc
        if path.suffix != ".plist" or not any(
            root in path.parents for root in allowed_roots
        ):
            raise PermissionError(
                "Quarantine only works on .plist files inside the launch item folders."
            )
        if not path.is_file():
            raise ValueError(f"No such launch item: {path}")
        target = str(path)

    action_id = uuid.uuid4().hex
    PENDING_SECURITY_ACTIONS[action_id] = PendingRemediation(
        action_id=action_id,
        action=action,
        target=target,
        reason=reason.strip(),
        created_at=time.time(),
    )
    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "action": action,
        "target": target,
        "reason": reason.strip(),
        "expires_in_seconds": PENDING_TTL_SECONDS,
    }


def _do_kill_process(pid: int) -> dict:
    if psutil is None:
        return {"status": "error", "error": "psutil unavailable"}
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        proc.terminate()
        try:
            proc.wait(timeout=3)
            outcome = "terminated"
        except psutil.TimeoutExpired:
            proc.kill()
            outcome = "killed"
    except psutil.NoSuchProcess:
        return {"status": "error", "error": f"No such process: {pid}"}
    except psutil.AccessDenied:
        return {"status": "error", "error": f"Access denied terminating pid {pid}."}
    return {"status": "executed", "result": f"{name} (pid {pid}) {outcome}."}


def _do_quarantine_launch_agent(target: str) -> dict:
    path = Path(target).expanduser()
    if not path.is_file():
        return {"status": "error", "error": f"No such launch item: {path}"}
    # Best-effort unload first (user domain; system dirs may need sudo —
    # the file move below still disables it at next boot).
    try:
        subprocess.run(
            ["launchctl", "unload", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    quarantine = Path.home() / ".friday-quarantine" / datetime.now().strftime("%Y%m%d-%H%M%S")
    quarantine.mkdir(parents=True, exist_ok=True)
    destination = quarantine / path.name
    try:
        shutil.move(str(path), str(destination))
    except (OSError, PermissionError) as exc:
        return {"status": "error", "error": f"Could not move {path}: {exc}"}
    return {
        "status": "executed",
        "result": f"Launch item unloaded and quarantined to {destination}. "
        "Reversible: move the file back and `launchctl load` it.",
    }


def confirm_remediation(action_id: str | None = None) -> dict:
    if not action_id:
        action_id = _latest_pending_id()
    action = PENDING_SECURITY_ACTIONS.get(action_id)
    if action is None:
        raise KeyError(f"No pending security remediation: {action_id}")
    if time.time() - action.created_at > PENDING_TTL_SECONDS:
        PENDING_SECURITY_ACTIONS.pop(action_id, None)
        raise TimeoutError(f"Pending security remediation expired: {action_id}")
    PENDING_SECURITY_ACTIONS.pop(action_id, None)

    if action.action == "kill_process":
        result = _do_kill_process(int(action.target))
    else:
        result = _do_quarantine_launch_agent(action.target)
    result.update({"action_id": action_id, "action": action.action, "target": action.target})
    return result


def cancel_remediation(action_id: str | None = None) -> dict:
    if not action_id:
        try:
            action_id = _latest_pending_id()
        except KeyError:
            return {"status": "noop", "reason": "no pending security remediation"}
    removed = PENDING_SECURITY_ACTIONS.pop(action_id, None)
    return {"status": "cancelled" if removed else "noop", "action_id": action_id}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(mcp):
    @mcp.tool()
    def security_scan_mac() -> dict:
        """
        Read-only security audit of this Mac: launch agents/daemons,
        crontab, suspicious processes, connections to known-bad ports,
        and /etc/hosts tampering. Use when the boss asks for a security
        scan, "are we compromised", or "is this machine hacked".
        Changes nothing — remediation goes through propose_security_remediation.
        """
        err = _need_psutil()
        if err:
            return {"status": "error", "error": err}
        return run_mac_audit()

    @mcp.tool()
    async def security_scan_network(deep: bool = False) -> dict:
        """
        Discover devices on the local network (ARP table) and flag any not
        in the trusted baseline. deep=true also probes common ports on each
        device. Use for "scan my network", "any unknown devices",
        "is my wifi safe". Phones can be SEEN but not scanned internally —
        the result includes guided steps for a suspect phone.
        """
        return await run_network_scan(deep=deep)

    @mcp.tool()
    def security_accept_devices(labels: dict | None = None) -> dict:
        """
        Mark every device currently on the network as trusted (the
        baseline). Optional labels map mac/ip -> friendly name. Run once on
        a day the boss trusts everything connected; later scans flag
        newcomers.
        """
        return accept_devices(labels=labels)

    @mcp.tool()
    def propose_security_remediation(action: str, target: str, reason: str = "") -> dict:
        """
        Stage a security fix for the boss to confirm. Actions:
        kill_process (target=pid) or quarantine_launch_agent (target=plist
        path from the audit). Never executes without confirmation.
        """
        return propose_remediation(action=action, target=target, reason=reason)

    @mcp.tool()
    def confirm_security_remediation(action_id: str | None = None) -> dict:
        """
        Execute the most recently proposed security remediation. Quarantine
        is reversible (file moved, never deleted). Only call after the boss
        confirms.
        """
        return confirm_remediation(action_id=action_id)

    @mcp.tool()
    def cancel_security_remediation(action_id: str | None = None) -> dict:
        """Drop a pending security remediation without executing it."""
        return cancel_remediation(action_id=action_id)

    @mcp.tool()
    def enable_trust_mode(duration_minutes: int | None = None) -> dict:
        """
        Arm trust mode so Tier 1 actions (shell, notes, subagents, app
        control) run without per-action confirmation. ONLY call this when
        the boss explicitly says "trust mode on" / "enable trust mode" —
        never arm it on your own initiative. Auto-expires (default 30 min,
        max 240). Messaging other people and security remediation still
        always require confirmation.
        """
        return trust.arm(duration_minutes=duration_minutes)

    @mcp.tool()
    def disable_trust_mode() -> dict:
        """
        Disarm trust mode immediately ("trust mode off"). Every risky
        action goes back to propose/confirm.
        """
        return trust.disarm()

    @mcp.tool()
    def trust_mode_status() -> dict:
        """
        Report whether trust mode is armed and how long until it expires.
        Use when the boss asks "is trust mode on?".
        """
        return trust.status()
