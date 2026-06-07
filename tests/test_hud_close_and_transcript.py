import unittest


class HudCloseAndTranscriptTests(unittest.TestCase):
    def test_odysseus_close_commands_return_core(self):
        from friday.desktop.window import parse_odysseus_command

        for command in ("/ody close", "/odysseus close", "/close ody", "/core"):
            with self.subTest(command=command):
                self.assertEqual(parse_odysseus_command(command), "__core__")

    def test_odysseus_close_event_returns_core(self):
        from friday.desktop.window import apply_external_event

        class FakeWindow:
            def __init__(self):
                self.closed = False

            def show_core(self):
                self.closed = True

        window = FakeWindow()
        apply_external_event(window, {"type": "odysseus_panel", "panel": "close"})

        self.assertTrue(window.closed)

    def test_transcript_panel_has_compact_height_limit(self):
        from friday.desktop import chat

        self.assertLessEqual(chat.TRANSCRIPT_MAX_HEIGHT, 260)

    def test_odysseus_tool_catalog_has_close_panel_action(self):
        from friday.tools.odysseus import ACTIONS

        self.assertIn("close.panel", ACTIONS)

    def test_commandbar_suggests_odysseus_close(self):
        from friday.desktop.commandbar import SLASH_COMMANDS

        self.assertIn("/ody close", SLASH_COMMANDS)


if __name__ == "__main__":
    unittest.main()
