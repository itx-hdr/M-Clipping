"""The rename must not strand an existing installation."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.compat import database_path, env, session_token


class BrandingCompatibility(unittest.TestCase):
    def test_existing_settings_and_explicit_new_values(self):
        with patch.dict(os.environ, {"PEAKCUT_PORT": "8768"}, clear=True):
            self.assertEqual(env("MCLIPPER_PORT"), "8768")
            os.environ["MCLIPPER_PORT"] = "8769"
            self.assertEqual(env("MCLIPPER_PORT"), "8769")
            os.environ["MCLIPPER_PORT"] = ""
            self.assertEqual(env("MCLIPPER_PORT"), "")
            self.assertEqual(env("MCLIPPER_MISSING", "default"), "default")

    def test_database_and_journal_stay_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(database_path(root), root / "mclipper.db")
            previous = root / "peakcut.db"
            previous.write_bytes(b"existing data")
            journal = root / "peakcut.db-wal"
            journal.write_bytes(b"journal")
            self.assertEqual(database_path(root), previous)
            self.assertEqual(journal.read_bytes(), b"journal")
            current = root / "mclipper.db"
            current.touch()
            self.assertEqual(database_path(root), current)
            self.assertEqual(previous.read_bytes(), b"existing data")

    def test_existing_session_and_new_session_precedence(self):
        self.assertEqual(session_token({"peakcut_session": "old"}), "old")
        self.assertEqual(session_token({"peakcut_session": "old", "mclipper_session": "new"}), "new")
        self.assertEqual(session_token({}), "")


if __name__ == "__main__":
    unittest.main()
