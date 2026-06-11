"""
Configuration — load environment variables and app-wide settings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


_LEGACY_KNOWLEDGE_DIR = "/Users/dhruvsmac/Desktop/SecBrain/friday-knowledge"


def _default_knowledge_dir() -> str:
    """Portable default with a back-compat carve-out: existing installs
    that already grew a knowledge folder at the legacy location keep it."""
    if Path(_LEGACY_KNOWLEDGE_DIR).is_dir():
        return _LEGACY_KNOWLEDGE_DIR
    return str(Path.home() / ".friday" / "knowledge")


class Config:
    # Server identity
    SERVER_NAME: str = os.getenv("SERVER_NAME", "Friday")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Assistant persona. "FRIDAY" and the Stark framing are Marvel IP —
    # rename via env before any public deployment.
    PERSONA_NAME: str = os.getenv("FRIDAY_PERSONA_NAME", "FRIDAY")
    PERSONA_BOSS: str = os.getenv("FRIDAY_PERSONA_BOSS", "Tony Stark")

    # External API keys (add as needed)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    SEARCH_API_KEY: str = os.getenv("SEARCH_API_KEY", "")
    ODYSSEUS_BASE_URL: str = os.getenv("ODYSSEUS_BASE_URL", "http://127.0.0.1:7870")
    ODYSSEUS_BRIDGE_TOKEN: str = os.getenv("ODYSSEUS_BRIDGE_TOKEN", "")

    # Persistent memory — Obsidian vault on disk.
    OBSIDIAN_VAULT_PATH: str = os.getenv(
        "OBSIDIAN_VAULT_PATH",
        str(Path.home() / "FridayVault"),
    )
    MEMORY_FOLDER: str = os.getenv("MEMORY_FOLDER", "Memory")

    # Self-learning engine — background research jobs.
    FRIDAY_KNOWLEDGE_DIR: str = os.getenv("FRIDAY_KNOWLEDGE_DIR", _default_knowledge_dir())
    FRIDAY_LEARNER_MODEL: str = os.getenv("FRIDAY_LEARNER_MODEL", "gpt-4o-mini")
    FRIDAY_LEARNER_MAX_ROUNDS: int = int(os.getenv("FRIDAY_LEARNER_MAX_ROUNDS", "5"))
    FRIDAY_LEARNER_MAX_PAGES_PER_ROUND: int = int(
        os.getenv("FRIDAY_LEARNER_MAX_PAGES_PER_ROUND", "4")
    )
    FRIDAY_LEARNER_FETCH_DELAY_S: float = float(
        os.getenv("FRIDAY_LEARNER_FETCH_DELAY_S", "2.0")
    )
    FRIDAY_LEARNER_MAX_CONCURRENT: int = int(
        os.getenv("FRIDAY_LEARNER_MAX_CONCURRENT", "2")
    )

    # Computer-access sandbox roots used by imported memory/computer helpers.
    WORKSPACE_ROOTS: list[str] = [
        p.strip()
        for chunk in os.getenv("WORKSPACE_ROOTS", str(Path.home())).split(os.pathsep)
        for p in chunk.split(",")
        if p.strip()
    ]


config = Config()
