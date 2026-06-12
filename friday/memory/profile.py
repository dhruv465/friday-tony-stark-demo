"""
User profile primitives — the `- **field**: value` bullet list in
Profile/about_user.md. Extracted from friday/tools/memory.py so the
voice-agent hooks (feedback capture, reflection, context injection) can
read/write the profile without going through MCP.
"""

from __future__ import annotations

from pathlib import Path

from friday.memory import vault


PROFILE_REL = "Profile/about_user.md"


def _profile_path(root: Path | None) -> Path:
    base = root if root is not None else vault.vault_root()
    return base / PROFILE_REL


def split_frontmatter(text: str) -> tuple[str, str]:
    """Strip a leading YAML frontmatter block (---...---) if present.
    Returns (body, frontmatter_block)."""
    if not text.startswith("---"):
        return text, ""
    end = text.find("\n---", 3)
    if end < 0:
        return text, ""
    fm_end = text.find("\n", end + 4)
    if fm_end < 0:
        return "", text
    return text[fm_end + 1 :].lstrip("\n"), text[: fm_end + 1]


def profile_text(root: Path | None = None) -> str:
    """The profile body without frontmatter, or ''."""
    path = _profile_path(root)
    if not path.is_file():
        return ""
    body, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    return body.strip()


def get_field(field: str, root: Path | None = None) -> str | None:
    prefix = f"- **{field}**:"
    for line in profile_text(root).splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def update_field(field: str, value: str, root: Path | None = None) -> None:
    """Insert or replace one `- **field**: value` bullet."""
    field = field.strip()
    value = value.strip()
    if not field or not value:
        return
    path = _profile_path(root)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    body, _ = split_frontmatter(existing)
    bullet = f"- **{field}**: {value}"
    lines = body.splitlines()
    prefix = f"- **{field}**:"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = bullet
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(bullet)
    new_body = "\n".join(lines).rstrip() + "\n"
    frontmatter = f"---\ntags: [profile]\nupdated: {vault.now_iso()}\n---\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + new_body, encoding="utf-8")
