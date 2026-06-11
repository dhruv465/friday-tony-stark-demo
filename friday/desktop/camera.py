"""Camera panel — live FaceTime-camera feed inside the HUD.

The camera is OFF by default (no permission prompt, no green dot at
boot). It starts when the boss opens the camera view, clicks CAM ON, or
when FRIDAY needs a snapshot for vision (``describe_camera_view`` MCP
tool). Snapshot requests arrive from the tool process via the shared
desktop event log: ``{"type": "camera", "action": "snapshot", "path": …}``
— the panel captures a frame to that path and the tool picks the file up.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import theme

logger = logging.getLogger("friday.desktop.camera")

try:
    from PySide6.QtMultimedia import (
        QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices,
    )
    from PySide6.QtMultimediaWidgets import QVideoWidget

    MULTIMEDIA_OK = True
except Exception as exc:  # pragma: no cover - depends on install flavour
    MULTIMEDIA_OK = False
    logger.warning("QtMultimedia unavailable: %s", exc)


def _hud_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFixedHeight(26)
    btn.setStyleSheet(
        f"QPushButton {{ color: {theme.HUD_CYAN.name()}; background: rgba(0,180,210,28); "
        f"border: 1px solid rgba(0,180,210,90); border-radius: 4px; "
        f"font-family: {theme.FONT_HUD}; font-size: 10px; letter-spacing: 1.5px; "
        f"padding: 0 14px; }}"
        f"QPushButton:hover {{ background: rgba(0,180,210,60); }}"
    )
    return btn


class CameraPanel(QWidget):
    """Full center-stack camera view: live feed + CAM ON/OFF + SNAPSHOT."""

    state_changed = Signal(bool)  # camera live?
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._camera = None
        self._session = None
        self._capture = None
        self._live = False
        self._pending_snapshot: str | None = None
        self._stop_after_snapshot = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(10)

        # Chrome bar.
        bar = QHBoxLayout()
        title = QLabel("VISUAL FEED · CAMERA")
        title.setStyleSheet(
            f"color: {theme.HUD_ICE.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 11px; font-weight: 600; letter-spacing: 3px; background: transparent;"
        )
        bar.addWidget(title)
        bar.addStretch(1)
        self._toggle_btn = _hud_button("CAM ON")
        self._toggle_btn.clicked.connect(self.toggle)
        bar.addWidget(self._toggle_btn)
        self._snap_btn = _hud_button("SNAPSHOT")
        self._snap_btn.clicked.connect(self._manual_snapshot)
        bar.addWidget(self._snap_btn)
        close_btn = _hud_button("✕ CLOSE")
        close_btn.clicked.connect(self.close_requested.emit)
        bar.addWidget(close_btn)
        layout.addLayout(bar)

        # Feed area.
        if MULTIMEDIA_OK:
            self._video = QVideoWidget()
            self._video.setStyleSheet("background: black; border: 1px solid rgba(0,180,210,70);")
            layout.addWidget(self._video, 1)
        else:
            self._video = None
            missing = QLabel("CAMERA SUBSYSTEM OFFLINE — QtMultimedia not available")
            missing.setAlignment(Qt.AlignCenter)
            missing.setStyleSheet(
                f"color: {theme.HUD_RED.name()}; font-family: {theme.FONT_HUD}; font-size: 11px;"
            )
            layout.addWidget(missing, 1)

        self._status = QLabel("CAMERA OFF")
        self._status.setStyleSheet(
            f"color: {theme.TEXT_DIM.name()}; font-family: {theme.FONT_HUD}; "
            f"font-size: 10px; letter-spacing: 2px; background: transparent;"
        )
        layout.addWidget(self._status)

    # ------------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return self._live

    def start(self) -> bool:
        if not MULTIMEDIA_OK:
            return False
        if self._live:
            return True
        device = QMediaDevices.defaultVideoInput()
        if device.isNull():
            self._status.setText("NO CAMERA DEVICE FOUND")
            return False
        self._camera = QCamera(device)
        self._session = QMediaCaptureSession()
        self._session.setCamera(self._camera)
        self._session.setVideoOutput(self._video)
        self._capture = QImageCapture(self._camera)
        self._session.setImageCapture(self._capture)
        self._capture.imageSaved.connect(self._on_image_saved)
        self._capture.errorOccurred.connect(
            lambda _id, _err, msg: self._status.setText(f"SNAPSHOT ERROR — {msg}"[:80])
        )
        self._camera.start()
        self._live = True
        self._toggle_btn.setText("CAM OFF")
        self._status.setText(f"LIVE — {device.description()}")
        self.state_changed.emit(True)
        return True

    def stop(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
        self._camera = None
        self._session = None
        self._capture = None
        self._live = False
        self._toggle_btn.setText("CAM ON")
        self._status.setText("CAMERA OFF")
        self.state_changed.emit(False)

    def toggle(self) -> None:
        if self._live:
            self.stop()
        else:
            self.start()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def capture_to(self, path: str) -> None:
        """Capture a frame to ``path`` — used by the describe_camera_view
        tool via the event log. Starts the camera if needed and turns it
        back off afterwards so FRIDAY never leaves it running unasked."""
        if not MULTIMEDIA_OK:
            return
        self._pending_snapshot = path
        if self._live:
            self._stop_after_snapshot = False
            self._fire_capture()
        else:
            self._stop_after_snapshot = True
            if self.start():
                # Give the sensor a moment to expose before grabbing.
                QTimer.singleShot(1200, self._fire_capture)
            else:
                self._pending_snapshot = None

    def _fire_capture(self) -> None:
        if self._capture is None or self._pending_snapshot is None:
            return
        self._capture.captureToFile(self._pending_snapshot)

    def _on_image_saved(self, _id: int, path: str) -> None:
        self._status.setText(f"SNAPSHOT SAVED — {path}"[:90])
        self._pending_snapshot = None
        if self._stop_after_snapshot:
            self._stop_after_snapshot = False
            QTimer.singleShot(300, self.stop)

    def _manual_snapshot(self) -> None:
        import time as _time
        from pathlib import Path

        target = Path.home() / "Pictures" / "Friday" / f"cam-{int(_time.time() * 1000)}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        self.capture_to(str(target))
