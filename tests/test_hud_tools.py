import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from friday.tools import hud, security


class HudToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "FRIDAY_KNOWLEDGE_DIR": self._tmp.name,
                "FRIDAY_DESKTOP_EVENT_LOG": str(Path(self._tmp.name) / "events.jsonl"),
                "FRIDAY_CAMERA_SNAPSHOT_DIR": str(Path(self._tmp.name) / "cam"),
                "FRIDAY_CAMERA_SNAPSHOT_TIMEOUT_S": "0.6",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _events(self) -> list[dict]:
        path = Path(self._tmp.name) / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_show_hud_panel_emits_event_and_validates(self):
        result = hud.show_hud_panel("camera")
        self.assertEqual(result["status"], "ok")
        events = self._events()
        self.assertEqual(events[-1]["type"], "hud_panel")
        self.assertEqual(events[-1]["panel"], "camera")
        with self.assertRaises(ValueError):
            hud.show_hud_panel("reactor")

    def test_snapshot_request_times_out_without_hud(self):
        self.assertIsNone(hud.request_camera_snapshot())
        events = self._events()
        self.assertEqual(events[-1]["type"], "camera")
        self.assertEqual(events[-1]["action"], "snapshot")

    def test_snapshot_request_returns_path_when_file_lands(self):
        import threading, time

        def _deliver():
            time.sleep(0.1)
            events = self._events()
            target = Path(events[-1]["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-jpeg-bytes")

        threading.Thread(target=_deliver, daemon=True).start()
        path = hud.request_camera_snapshot(timeout_s=3.0)
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).is_file())

    def test_describe_camera_errors_honestly_without_hud(self):
        result = hud.describe_camera()
        self.assertEqual(result["status"], "error")
        self.assertIn("desktop HUD", result["error"])

    def test_security_scans_persist_last_scan_for_hud(self):
        with patch.object(security, "audit_launch_items", return_value=[]), patch.object(
            security, "audit_crontab", return_value=[]
        ), patch.object(security, "audit_processes", return_value=[]), patch.object(
            security, "audit_connections", return_value=[]
        ), patch.object(security, "audit_hosts_file", return_value=[]):
            security.run_mac_audit()

        last = json.loads(
            (Path(self._tmp.name) / "_security" / "last_scan.json").read_text()
        )
        self.assertEqual(last["mac"]["verdict"], "clean")
        self.assertIn("at", last["mac"])

    def test_hud_tools_register_with_mcp(self):
        class FakeMcp:
            def __init__(self):
                self.names = []

            def tool(self):
                def decorate(fn):
                    self.names.append(fn.__name__)
                    return fn

                return decorate

        mcp = FakeMcp()
        hud.register(mcp)
        self.assertEqual(
            mcp.names, ["open_hud_panel", "close_hud_panel", "describe_camera_view"]
        )


if __name__ == "__main__":
    unittest.main()
