import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from friday.tools import security


ARP_SAMPLE = """\
router.lan (192.168.1.1) at a4:2b:b0:11:22:33 on en0 ifscope [ethernet]
? (192.168.1.42) at 6e:77:88:99:aa:bb on en0 ifscope [ethernet]
? (192.168.1.99) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""


def _write_plist(folder: Path, name: str, program: str, label: str = "test") -> Path:
    path = folder / name
    with path.open("wb") as fh:
        plistlib.dump({"Label": label, "Program": program}, fh)
    return path


class SecurityAuditTests(unittest.TestCase):
    def test_launch_item_in_tmp_flagged_high(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_plist(folder, "com.evil.agent.plist", "/tmp/.hidden/payload")
            findings = security.audit_launch_items(dirs=(str(folder),))

        self.assertTrue(findings)
        self.assertEqual(findings[0].severity, "high")
        self.assertIn("suspicious location", findings[0].detail)
        self.assertIn("quarantine_launch_agent", findings[0].remediation)

    def test_recent_benign_launch_item_flagged_warn_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_plist(folder, "com.vendor.updater.plist", "/Applications/Vendor.app/updater")
            findings = security.audit_launch_items(dirs=(str(folder),))

        self.assertEqual([f.severity for f in findings], ["warn"])

    def test_old_benign_launch_item_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = _write_plist(folder, "com.vendor.updater.plist", "/usr/local/bin/updater")
            old = 30 * 86400
            os.utime(path, (path.stat().st_atime - old, path.stat().st_mtime - old))
            findings = security.audit_launch_items(dirs=(str(folder),))

        self.assertEqual(findings, [])

    def test_hosts_file_redirect_of_vendor_domain_is_high(self):
        with tempfile.NamedTemporaryFile("w", suffix="hosts", delete=False) as fh:
            fh.write("127.0.0.1 localhost\n10.0.0.5 apple.com\n192.168.1.7 mything.lan\n")
            hosts_path = fh.name
        try:
            findings = security.audit_hosts_file(path=hosts_path)
        finally:
            os.unlink(hosts_path)

        severities = sorted(f.severity for f in findings)
        self.assertEqual(severities, ["high", "warn"])

    def test_path_suspicion_tiers(self):
        home = str(Path.home())
        # Known dev tooling in dot-dirs is clean.
        self.assertIsNone(security._path_suspicion(f"{home}/.local/bin/uv"))
        self.assertIsNone(security._path_suspicion(f"{home}/.bun/bin/bun"))
        self.assertIsNone(security._path_suspicion("/Applications/Safari.app/Contents/MacOS/Safari"))
        # Unknown hidden dir is warn, not high.
        self.assertEqual(security._path_suspicion(f"{home}/.zz-weird/payload"), "warn")
        # Malware drop zones are high.
        self.assertEqual(security._path_suspicion("/tmp/payload"), "high")
        self.assertEqual(security._path_suspicion("/Users/Shared/payload"), "high")
        self.assertEqual(security._path_suspicion(f"{home}/Downloads/payload"), "high")

    def test_parse_arp_table_drops_noise(self):
        devices = security.parse_arp_table(ARP_SAMPLE)
        self.assertEqual(
            [d["ip"] for d in devices], ["192.168.1.1", "192.168.1.42"]
        )
        self.assertEqual(devices[0]["mac"], "a4:2b:b0:11:22:33")


class SecurityRemediationBrokerTests(unittest.TestCase):
    def setUp(self):
        security.clear_pending_security_actions()
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"FRIDAY_KNOWLEDGE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()

    def tearDown(self):
        security.clear_pending_security_actions()
        self._env.stop()
        self._tmp.cleanup()

    def test_kill_requires_confirmation_before_execution(self):
        with patch.object(security, "_do_kill_process") as kill:
            kill.return_value = {"status": "executed", "result": "done"}
            pending = security.propose_remediation("kill_process", "54321", reason="test")
            kill.assert_not_called()
            self.assertEqual(pending["status"], "pending_confirmation")

            result = security.confirm_remediation()

        kill.assert_called_once_with(54321)
        self.assertEqual(result["status"], "executed")

    def test_refuses_dangerous_or_invalid_targets(self):
        with self.assertRaises(PermissionError):
            security.propose_remediation("kill_process", "1")
        with self.assertRaises(PermissionError):
            security.propose_remediation("kill_process", str(os.getpid()))
        with self.assertRaises(ValueError):
            security.propose_remediation("kill_process", "not-a-pid")
        with self.assertRaises(ValueError):
            security.propose_remediation("reboot_phone", "x")
        with self.assertRaises(PermissionError):
            security.propose_remediation(
                "quarantine_launch_agent", "/etc/passwd"
            )

    def test_cancel_drops_pending(self):
        pending = security.propose_remediation("kill_process", "54321")
        result = security.cancel_remediation(pending["action_id"])
        self.assertEqual(result["status"], "cancelled")
        with self.assertRaises(KeyError):
            security.confirm_remediation()

    def test_accept_devices_then_scan_flags_newcomer(self):
        import asyncio

        first = [
            {"ip": "192.168.1.1", "mac": "aa:aa", "name": "router"},
        ]
        second = first + [{"ip": "192.168.1.66", "mac": "bb:bb", "name": "?"}]

        with patch.object(security, "_arp_devices", return_value=first):
            accepted = security.accept_devices()
        self.assertEqual(accepted["status"], "ok")

        with patch.object(security, "_arp_devices", return_value=second):
            report = asyncio.run(security.run_network_scan())

        self.assertTrue(report["baseline_accepted"])
        self.assertEqual(
            [d["mac"] for d in report["unknown_devices"]], ["bb:bb"]
        )

    def test_security_tools_register_with_mcp(self):
        class FakeMcp:
            def __init__(self):
                self.names = []

            def tool(self):
                def decorate(fn):
                    self.names.append(fn.__name__)
                    return fn

                return decorate

        mcp = FakeMcp()
        security.register(mcp)

        self.assertEqual(
            mcp.names,
            [
                "security_scan_mac",
                "security_scan_network",
                "security_accept_devices",
                "propose_security_remediation",
                "confirm_security_remediation",
                "cancel_security_remediation",
                "enable_trust_mode",
                "disable_trust_mode",
                "trust_mode_status",
            ],
        )


if __name__ == "__main__":
    unittest.main()
