import tempfile
import unittest
from pathlib import Path

import dashboard
import db
import terminal_theme


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def test_dashboard_renders_pipeline_status(self):
        renderable = dashboard.render_dashboard(["ALGE", "line2", "line3", "line4", "line5"])

        self.assertIsNotNone(renderable)

    def test_dashboard_uses_vertical_divider(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(dashboard.render_dashboard(["ALGE", "line2", "line3", "line4", "line5"]))

        self.assertIn("│", capture.get())


if __name__ == "__main__":
    unittest.main()
