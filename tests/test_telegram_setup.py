import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.setup_telegram import SetupError, call, groups_from_updates, save_settings


class TelegramSetupTests(unittest.TestCase):
    def test_group_discovery_excludes_private_messages_and_deduplicates(self):
        updates = [
            {"message": {"chat": {"id": 1, "type": "private"}}},
            {"message": {"chat": {"id": -1001, "type": "supergroup", "title": "Movie group"}}},
            {
                "my_chat_member": {
                    "chat": {"id": -1001, "type": "supergroup", "title": "Movie group"}
                }
            },
        ]
        self.assertEqual(groups_from_updates(updates), {"-1001": "Movie group"})

    def test_saving_replaces_both_settings_and_preserves_unrelated_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OTHER=value\nTELEGRAM_BOT_TOKEN=old\nTELEGRAM_CHAT_IDS=-2\n")
            save_settings(path, "123:private", "-1001")
            content = path.read_text()
            self.assertEqual(content.count("TELEGRAM_BOT_TOKEN="), 1)
            self.assertIn("OTHER=value", content)
            self.assertIn("TELEGRAM_CHAT_IDS=-1001", content)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_transport_error_never_displays_token_url(self):
        with patch(
            "scripts.setup_telegram.urllib.request.urlopen",
            side_effect=OSError("https://api.telegram.org/bot123:secret/getMe"),
        ):
            with self.assertRaises(SetupError) as exc:
                call("123:secret", "getMe")
            self.assertNotIn("secret", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
