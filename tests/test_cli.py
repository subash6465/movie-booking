"""CLI diagnostics must remain read-only and fail clearly without credentials."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from movie_monitor.cli import main


class CliTests(unittest.TestCase):
    def test_validation_does_not_start_engine_or_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unused.sqlite3"
            with (
                patch("movie_monitor.engine.run", side_effect=AssertionError("No polling")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(["--config", "watches.example.json", "--db", str(path), "--validate"]), 0
                )
            self.assertFalse(path.exists())

    def test_malformed_status_is_a_cli_error_without_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            for content in ("{broken", "[]", '{"providers":{}}', '{"providers":[null]}'):
                (path.parent / "latest.json").write_text(content, encoding="utf-8")
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main(["--db", str(path), "--status"])
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(path.exists())

    def test_status_does_not_load_credentials_or_lock_live_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            (path.parent / "latest.json").write_text(
                json.dumps({"providers": [], "running": True}), encoding="utf-8"
            )
            with (
                patch("movie_monitor.cli.load_env", side_effect=AssertionError("No credentials")),
                patch("movie_monitor.lock.SingleInstance", side_effect=AssertionError("No lock")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["--db", str(path), "--notify", "--status"]), 0)
