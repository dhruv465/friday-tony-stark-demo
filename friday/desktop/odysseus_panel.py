"""Embedded Odysseus web panel for the FRIDAY HUD."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStackedLayout, QVBoxLayout, QWidget

from . import theme


PANEL_PATHS = {
    "home": "/",
    "chat": "/",
    "notes": "/notes",
    "tasks": "/tasks",
    "memory": "/memory",
    "settings": "/settings",
    "research": "/research",
    "calendar": "/calendar",
    "compare": "/compare",
    "gallery": "/gallery",
    "cookbook": "/cookbook",
}


def normalize_odysseus_base_url(value: str | None = None) -> str:
    raw = (value or os.getenv("ODYSSEUS_BASE_URL") or "http://127.0.0.1:7870").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("ODYSSEUS_BASE_URL must use http or https.")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Odysseus HUD embed only allows localhost targets.")
    return raw


def odysseus_panel_url(panel: str = "home", base_url: str | None = None) -> str:
    key = (panel or "home").strip().lower().lstrip("/")
    if not key:
        key = "home"
    if key not in PANEL_PATHS:
        raise ValueError(f"Unknown Odysseus panel: {panel}")
    return normalize_odysseus_base_url(base_url) + PANEL_PATHS[key]


class OdysseusPanel(QWidget):
    """Container around QWebEngineView with a lightweight fallback label."""

    close_requested = Signal()

    def __init__(self, parent=None, base_url: str | None = None):
        super().__init__(parent)
        self.base_url = normalize_odysseus_base_url(base_url)
        self.current_panel = "home"
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("OdysseusPanel")
        self.setStyleSheet(
            f"#OdysseusPanel {{ background: rgba(2,4,8,230); "
            f"border-left: 1px solid rgba(0,180,210,70); "
            f"border-right: 1px solid rgba(0,180,210,70); }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        chrome = QHBoxLayout()
        chrome.setContentsMargins(14, 8, 10, 8)
        chrome.setSpacing(8)
        title = QLabel("ODYSSEUS")
        title.setStyleSheet(
            f"color: {theme.TEXT_FAINT.name()}; font-family: {theme.FONT_HUD}; "
            "font-size: 10px; font-weight: 700; letter-spacing: 3px; "
            "background: transparent;"
        )
        close = QPushButton("✕  CLOSE")
        close.setToolTip("Close Odysseus panel · return to FRIDAY core")
        close.setCursor(Qt.PointingHandCursor)
        close.setFixedSize(86, 26)
        close.setStyleSheet(
            "QPushButton {"
            f"color: {theme.TEXT_BRIGHT.name()};"
            "background: rgba(255,70,95,60);"
            "border: 1px solid rgba(255,70,95,160);"
            "border-radius: 4px;"
            f"font-family: {theme.FONT_HUD};"
            "font-size: 11px;"
            "font-weight: 700;"
            "letter-spacing: 1.5px;"
            "padding: 0 8px;"
            "}"
            "QPushButton:hover { background: rgba(255,70,95,110); border-color: rgba(255,70,95,220); }"
        )
        close.clicked.connect(self.close_requested.emit)
        chrome.addWidget(title)
        chrome.addStretch(1)
        chrome.addWidget(close)
        outer.addLayout(chrome)

        body = QWidget(self)
        body.setAttribute(Qt.WA_StyledBackground, True)
        body.setStyleSheet("background: transparent;")
        layout = QStackedLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self._layout = layout
        outer.addWidget(body, 1)

        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView

            self.view = QWebEngineView(self)
            self.view.setContextMenuPolicy(Qt.NoContextMenu)
            self._layout.addWidget(self.view)
            self._layout.setCurrentWidget(self.view)
            self.open_panel("home")
        except Exception as exc:
            self.view = None
            label = QLabel(
                "ODYSSEUS PANEL UNAVAILABLE\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "Install/enable PySide6 QtWebEngine, then restart FRIDAY.",
                self,
            )
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(
                f"color: {theme.TEXT_DIM.name()}; font-family: {theme.FONT_HUD}; "
                "font-size: 12px; padding: 32px;"
            )
            self._layout.addWidget(label)
            self._layout.setCurrentWidget(label)

    def open_panel(self, panel: str = "home") -> str:
        url = odysseus_panel_url(panel, self.base_url)
        self.current_panel = (panel or "home").strip().lower().lstrip("/") or "home"
        if self.view is not None:
            self.view.setUrl(QUrl(url))
        return url

    def reload_panel(self) -> None:
        if self.view is not None:
            self.view.reload()
