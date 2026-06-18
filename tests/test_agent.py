import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import agent
import agent_tools
import archive_plugins
import cli
import db
import goals
import llm
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
        goal_path = Path(self.tempdir.name) / "goals.json"
        self.shell.goal_store = goals.GoalStore(path=goal_path)
        self.old_archive_registry = archive_plugins.DEFAULT_REGISTRY_PATH
        archive_plugins.DEFAULT_REGISTRY_PATH = str(Path(self.tempdir.name) / "archive_plugins.json")

    def tearDown(self):
        archive_plugins.DEFAULT_REGISTRY_PATH = self.old_archive_registry
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

    @unittest.skipUnless(os.getenv("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY is required for live chat")
    def test_plain_text_enters_chat_path(self):
        result, output = self._run("remember this preference")

        self.assertIsNone(result)
        self.assertNotIn("Unknown command", output)
        rows = self.shell.memory.entries()
        self.assertTrue(
            any(row["kind"] == "user" and row["content"] == "remember this preference" for row in rows)
        )

    def test_model_command_sets_exact_model(self):
        result, output = self._run("/model qwen/qwen3.7-plus")

        self.assertIsNone(result)
        self.assertEqual(self.shell.config["model"], "qwen/qwen3.7-plus")
        self.assertIn("model updated", output)

    def test_memory_commands_save_and_read_context(self):
        self._run("/remember prioritize archive.org texts")
        result, output = self._run("/memory --search archive.org")

        self.assertIsNone(result)
        self.assertIn("prioritize archive.org texts", output)

    def test_backlog_tool_completes_when_empty(self):
        runner = agent_tools.AppToolRunner(self.shell)
        result = runner.run_backlog_until_done(max_cycles=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["backlog"]["pending_downloads"], 0)
        self.assertEqual(result["backlog"]["pending_extractions"], 0)

    def test_goal_command_creates_durable_goal(self):
        result, output = self._run("/goal Find everything about Thelema")

        self.assertIsNone(result)
        self.assertIn("GOAL", output)
        stored = self.shell.goal_store.list()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "active")
        self.assertEqual(stored[0]["objective"], "Find everything about Thelema")

    def test_goal_timer_tool_updates_active_goal(self):
        goal = self.shell.goal_store.create("Archive Thelema materials")
        self.shell.current_goal = goal
        result = self.shell.tools.set_goal_timer("2h", "initial estimate")

        self.assertTrue(result["ok"])
        updated = self.shell.goal_store.get(goal["id"])
        self.assertIsNotNone(updated["estimated_completion_at"])
        self.assertEqual(result["duration_seconds"], 7200)

    def test_add_archive_tool_registers_plugin_source(self):
        result = self.shell.tools.add_archive(
            name="Fixture Archive",
            base_url="https://fixture.example",
            search_url_template="https://fixture.example/search?q={query}",
            result_selector=".result",
            link_selector="a",
            title_selector=".title",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["archive"]["slug"], "fixture_archive")
        self.assertIn("archive_plugins", self.shell.config["sources"])
        plugins = archive_plugins.load_plugins()
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["base_url"], "https://fixture.example")

    def test_goal_tool_loop_honors_stop_checker_before_model_call(self):
        original_chat_completion = llm.chat_completion

        def fail_chat_completion(*_args, **_kwargs):
            raise AssertionError("chat_completion should not be called after goal stop")

        try:
            llm.chat_completion = fail_chat_completion
            result = self.shell._run_llm_tool_loop(
                [{"role": "user", "content": "keep going"}],
                stop_checker=lambda: True,
            )
        finally:
            llm.chat_completion = original_chat_completion

        self.assertIn("halted by operator", result)

    @unittest.skipUnless(os.getenv("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY is required for live compaction")
    def test_forced_compaction_creates_summary(self):
        self._run("/remember first note")
        self._run("/remember second note")
        result, compact_output = self._run("/compact --force")
        _, memory_output = self._run("/memory --limit 5")

        self.assertIsNone(result)
        self.assertIn("Memory compacted", compact_output)
        self.assertIn("[summary]", memory_output)


if __name__ == "__main__":
    unittest.main()
