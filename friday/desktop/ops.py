"""Ops widgets — learning, subagents, security, camera status.

Two faces of the same data:

- ``OpsRail`` — compact tile stack that sits above the activity feed in
  the right column. Always visible, refreshed every 4 s from disk state
  (learning ``job.json``, subagent ``agent.json``, security
  ``last_scan.json``). Clicking a tile expands the full center view.
- ``OpsPanel`` — full center-stack view with the same data at detail
  level (per-topic rows, per-agent rows, last scan findings).

All reads are best-effort: the HUD must never crash because a state file
is mid-write or a module import fails.
"""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import theme

logger = logging.getLogger("friday.desktop.ops")

STATUS_COLORS = {
    "running": "HUD_CYAN",
    "complete": "HUD_GREEN",
    "paused": "HUD_AMBER",
    "stalled": "HUD_AMBER",
    "stopped": "TEXT_DIM",
    "failed": "HUD_RED",
}


def _color(name: str):
    return getattr(theme, STATUS_COLORS.get(name, "TEXT_DIM"), theme.TEXT_DIM)


def _hud(text: str, color, size: float = 10.0, spacing: float = 1.5, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    weight = "600" if bold else "500"
    lbl.setStyleSheet(
        f"color: {color.name()}; font-family: {theme.FONT_HUD}; "
        f"font-size: {size}px; font-weight: {weight}; "
        f"letter-spacing: {spacing}px; background: transparent;"
    )
    return lbl


# ---------------------------------------------------------------------------
# Disk state readers (shared by rail + panel)
# ---------------------------------------------------------------------------

def read_learning_state() -> list[dict]:
    try:
        from friday.learning.store import known_topics

        return known_topics()
    except Exception as exc:
        logger.debug("learning state read skipped: %s", exc)
        return []


def read_agent_state() -> list[dict]:
    try:
        from friday.agents.runtime import list_records

        return list_records()
    except Exception as exc:
        logger.debug("agent state read skipped: %s", exc)
        return []


def read_security_state() -> dict:
    try:
        from friday.learning.store import knowledge_root

        path = knowledge_root() / "_security" / "last_scan.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("security state read skipped: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Compact rail
# ---------------------------------------------------------------------------

class _Tile(QWidget):
    """Clickable compact tile: header + two data lines."""

    clicked = Signal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("OpsTile")
        self.setStyleSheet(
            "#OpsTile { background: rgba(0,180,210,14); "
            "border: 1px solid rgba(0,180,210,55); border-radius: 6px; }"
            "#OpsTile:hover { background: rgba(0,180,210,30); }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._dot = _hud("●", theme.TEXT_FAINT, size=10, spacing=0, bold=True)
        head.addWidget(self._dot)
        head.addWidget(_hud(title, theme.TEXT_FAINT, size=9, spacing=2.5, bold=True))
        head.addStretch(1)
        lay.addLayout(head)
        self._line1 = _hud("—", theme.TEXT_BRIGHT, size=10, spacing=1)
        self._line2 = _hud("", theme.TEXT_DIM, size=9.5, spacing=1)
        lay.addWidget(self._line1)
        lay.addWidget(self._line2)

    def set_data(self, dot_color, line1: str, line2: str = "") -> None:
        self._dot.setStyleSheet(
            f"color: {dot_color.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 10px; font-weight: 600; background: transparent;"
        )
        self._line1.setText(line1[:30])
        self._line2.setText(line2[:34])
        self._line2.setVisible(bool(line2))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
            ev.accept()
            return
        super().mousePressEvent(ev)


class OpsRail(QWidget):
    """Compact ops tile stack for the right column."""

    camera_requested = Signal()
    ops_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 6)
        lay.setSpacing(8)

        lay.addWidget(_hud("OPS · LIVE", theme.TEXT_FAINT, size=9, spacing=3, bold=True))

        self.camera_tile = _Tile("CAMERA")
        self.camera_tile.clicked.connect(self.camera_requested.emit)
        self.learning_tile = _Tile("LEARNING")
        self.learning_tile.clicked.connect(self.ops_requested.emit)
        self.agents_tile = _Tile("AGENTS")
        self.agents_tile.clicked.connect(self.ops_requested.emit)
        self.security_tile = _Tile("SECURITY")
        self.security_tile.clicked.connect(self.ops_requested.emit)
        for tile in (self.camera_tile, self.learning_tile, self.agents_tile, self.security_tile):
            lay.addWidget(tile)

        self.set_camera_state(False)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh)
        self._tick.start(4000)
        self.refresh()

    def set_camera_state(self, live: bool) -> None:
        if live:
            self.camera_tile.set_data(theme.HUD_RED, "FEED LIVE", "click to view")
        else:
            self.camera_tile.set_data(theme.TEXT_FAINT, "OFF", "click to open")

    def refresh(self) -> None:
        topics = read_learning_state()
        active = [t for t in topics if t.get("status") == "running"]
        if active:
            top = active[0]
            self.learning_tile.set_data(
                theme.HUD_CYAN,
                f"{len(active)} ACTIVE",
                f"{top.get('topic', '?')[:20]} · {top.get('coverage', '')}",
            )
        elif topics:
            self.learning_tile.set_data(
                theme.HUD_GREEN, f"{len(topics)} TOPICS", "all idle"
            )
        else:
            self.learning_tile.set_data(theme.TEXT_FAINT, "NO TOPICS", "say: learn about…")

        agents = read_agent_state()
        running = [a for a in agents if a.get("status") == "running"]
        if running:
            self.agents_tile.set_data(
                theme.HUD_CYAN,
                f"{len(running)} DEPLOYED",
                running[0].get("name", "?")[:22],
            )
        elif agents:
            done = sum(1 for a in agents if a.get("status") == "complete")
            self.agents_tile.set_data(theme.HUD_GREEN, f"{done}/{len(agents)} DONE", "reports ready")
        else:
            self.agents_tile.set_data(theme.TEXT_FAINT, "NONE", "delegate a task")

        scan = read_security_state()
        mac = scan.get("mac") or {}
        net = scan.get("network") or {}
        if mac or net:
            high = (mac.get("counts") or {}).get("high", 0)
            unknown = net.get("unknown_count", 0)
            if high or unknown:
                self.security_tile.set_data(
                    theme.HUD_RED,
                    f"{high} HIGH · {unknown} NEW DEV",
                    str(mac.get("verdict", ""))[:30],
                )
            else:
                self.security_tile.set_data(
                    theme.HUD_GREEN,
                    str(mac.get("verdict", "clean")).upper()[:24],
                    f"{net.get('device_count', '—')} devices on net",
                )
        else:
            self.security_tile.set_data(theme.TEXT_FAINT, "NO SCAN YET", "say: security scan")


# ---------------------------------------------------------------------------
# Full center panel
# ---------------------------------------------------------------------------

class _SectionHeader(QLabel):
    def __init__(self, text: str):
        super().__init__(text)
        self.setStyleSheet(
            f"color: {theme.HUD_ICE.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 10px; font-weight: 600; letter-spacing: 3px; "
            f"background: transparent; padding-top: 6px;"
        )


class OpsPanel(QWidget):
    """Full center view: learning topics, agents, last security scan."""

    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 12, 18, 12)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        title = QLabel("OPERATIONS · LEARNING / AGENTS / SECURITY")
        title.setStyleSheet(
            f"color: {theme.HUD_ICE.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 11px; font-weight: 600; letter-spacing: 3px; background: transparent;"
        )
        bar.addWidget(title)
        bar.addStretch(1)
        close_btn = QLabel("✕ CLOSE")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
            f"color: {theme.HUD_CYAN.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 10px; letter-spacing: 2px; background: rgba(0,180,210,28); "
            f"border: 1px solid rgba(0,180,210,90); border-radius: 4px; padding: 4px 12px;"
        )
        close_btn.mousePressEvent = lambda ev: self.close_requested.emit()
        bar.addWidget(close_btn)
        outer.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        body = QWidget()
        body.setAttribute(Qt.WA_TranslucentBackground, True)
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(4)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh)
        self._tick.start(4000)
        self.refresh()

    def _clear(self) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refresh(self) -> None:
        if not self.isVisible():
            return
        self._render()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._render()

    def _render(self) -> None:
        self._clear()
        add = self._body_layout.addWidget

        add(_SectionHeader("LEARNING TOPICS"))
        topics = read_learning_state()
        if not topics:
            add(_hud("no topics studied yet", theme.TEXT_DIM, size=10))
        for t in topics[:12]:
            status = t.get("status", "?")
            add(
                _hud(
                    f"● {t.get('topic', '?')[:42]:<44} {status:<9} "
                    f"coverage {t.get('coverage', '?'):<11} rounds {t.get('rounds_completed', 0)}",
                    _color(status),
                    size=10.5,
                    spacing=0.5,
                )
            )

        add(_SectionHeader("SUBAGENTS"))
        agents = read_agent_state()
        if not agents:
            add(_hud("no agents deployed yet", theme.TEXT_DIM, size=10))
        for a in agents[:12]:
            status = a.get("status", "?")
            add(
                _hud(
                    f"● {a.get('name', '?')[:24]:<26} {status:<9} steps {a.get('steps_taken', 0):<3} "
                    f"{a.get('task', '')[:48]}",
                    _color(status),
                    size=10.5,
                    spacing=0.5,
                )
            )

        add(_SectionHeader("SECURITY · LAST SCAN"))
        scan = read_security_state()
        mac = scan.get("mac") or {}
        net = scan.get("network") or {}
        if not mac and not net:
            add(_hud("no scan run yet — say: run a security scan", theme.TEXT_DIM, size=10))
        if mac:
            counts = mac.get("counts") or {}
            verdict_color = theme.HUD_RED if counts.get("high") else (
                theme.HUD_AMBER if counts.get("warn") else theme.HUD_GREEN
            )
            add(
                _hud(
                    f"MAC  {str(mac.get('verdict', '?')).upper():<32} "
                    f"high {counts.get('high', 0)} · warn {counts.get('warn', 0)} · {mac.get('at', '')[:16]}",
                    verdict_color,
                    size=10.5,
                    spacing=0.5,
                )
            )
            for f in (mac.get("top_findings") or [])[:5]:
                add(_hud(f"   {f[:96]}", theme.TEXT_DIM, size=10, spacing=0.5))
        if net:
            unknown = net.get("unknown_count", 0)
            add(
                _hud(
                    f"NET  {net.get('device_count', 0)} devices · {unknown} unknown · {net.get('at', '')[:16]}",
                    theme.HUD_RED if unknown else theme.HUD_GREEN,
                    size=10.5,
                    spacing=0.5,
                )
            )
        self._body_layout.addStretch(1)
