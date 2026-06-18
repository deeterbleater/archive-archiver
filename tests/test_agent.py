import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import agent
import cli
import db
import memory


class AgentHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()
        self.shell = agent.ArchiveAgentShell(cli)
        memory_path = Path(self.tempdir.name) / "memory.jsonl"
        self.shell.config["memory_path"] = str(memory_path)
        self.shell.memory = memory.MemoryStore(path=memory_path)

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _run(self, command):
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.shell.onecmd(command)
        return result, output.getvalue()

    def test_status_command_uses_database_state(self):
        result, output = self._run("/status")

        self.assertIsNone(result)
        self.assertIn("DATABASE STATUS", output)
        self.assertIn("Total Unique Works Logged: 0", output)

    def test_set_updates_session_defaults(self):
        self._run("/set max-results 7")
        self._run("/set sources archive_org")
        result, output = self._run("/config")

        self.assertIsNone(result)
        self.assertEqual(self.shell.config["max_results"], 7)
        self.assertEqual(self.shell.config["sources"], ["archive_org"])
        self.assertIn("max_results: 7", output)
        self.assertIn("sources: archive_org", output)

    def test_unknown_command_does_not_exit_shell(self):
        result, output = self._run("/launch")

        self.assertIsNone(result)
        self.assertIn("Unknown slash command", output)

    def test_plain_commands_still_work(self):
        result, output = self._run("config")

        self.assertIsNone(result)
        self.assertIn("AGENT CONFIG", output)

    def test_memory_commands_save_and_read_context(self):
        self._run("/remember prioritize archive.org texts")
        result, output = self._run("/memory --search archive.org")

        self.assertIsNone(result)
        self.assertIn("prioritize archive.org texts", output)

    def test_forced_compaction_creates_summary(self):
        self._run("/remember first note")
        self._run("/remember second note")
        self.shell.memory._summarize = lambda rows, model=None: "test summary"
        result, compact_output = self._run("/compact --force")
        _, memory_output = self._run("/memory --limit 5")

        self.assertIsNone(result)
        self.assertIn("Memory compacted", compact_output)
        self.assertIn("[summary]", memory_output)


if __name__ == "__main__":
    unittest.main()
