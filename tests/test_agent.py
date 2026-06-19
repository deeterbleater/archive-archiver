import io
import os
import time
from unittest import mock
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

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
        self.assertIn("max_results", output)
        self.assertIn("7", output)
        self.assertIn("sources", output)
        self.assertIn("archive_org", output)

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
        result, output = self._run("/model minimax/minimax-m3")

        self.assertIsNone(result)
        self.assertEqual(self.shell.config["model"], "minimax/minimax-m3")
        self.assertIn("model updated", output)

    def test_exit_kills_managed_tmux_session(self):
        with mock.patch.dict(os.environ, {"ALGE_TMUX_MANAGED": "1", "ALGE_TMUX_SESSION": "alge-test", "TMUX": "/tmp/tmux"}):
            with mock.patch("agent.subprocess.Popen") as popen:
                result, output = self._run("/exit")

        self.assertTrue(result)
        self.assertIn("bye", output)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["tmux", "kill-session", "-t", "alge-test"])

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
        self.assertIn("Goal", output)
        stored = self.shell.goal_store.list()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["status"], "active")
        self.assertEqual(stored[0]["objective"], "Find everything about Thelema")

    def test_goal_command_supersedes_previous_active_goal(self):
        self._run("/goal First objective")
        result, output = self._run("/goal Second objective")

        stored = self.shell.goal_store.list()
        active = self.shell.goal_store.active()
        self.assertIsNone(result)
        self.assertIn("superseded 1 active goal", output)
        self.assertEqual(stored[0]["status"], "superseded")
        self.assertEqual(stored[1]["status"], "active")
        self.assertEqual(active["objective"], "Second objective")

    def test_goal_messages_do_not_include_chat_memory(self):
        self.shell.memory.append("user", "OLD GOAL: archive Thelema", {})
        self.shell.memory.append("assistant", "still working on the old objective", {})
        goal = self.shell.goal_store.create("New objective only")

        messages = self.shell._goal_messages(goal, cycle=1)
        joined = "\n".join(message["content"] for message in messages)

        self.assertIn("New objective only", joined)
        self.assertIn("goal context is isolated", joined)
        self.assertNotIn("OLD GOAL", joined)
        self.assertNotIn("old objective", joined)

    def test_goal_resume_supersedes_other_active_goals(self):
        first = self.shell.goal_store.create("First objective")
        second = self.shell.goal_store.create("Second objective")

        result, output = self._run(f"/goal --resume {first['id']}")

        first_updated = self.shell.goal_store.get(first["id"])
        second_updated = self.shell.goal_store.get(second["id"])
        self.assertIsNone(result)
        self.assertIn("superseded 1 active goal", output)
        self.assertEqual(first_updated["status"], "active")
        self.assertEqual(second_updated["status"], "superseded")

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

    def test_agent_initializes_idle_worker_slots_for_configured_sources(self):
        counts = db.get_agent_worker_counts()

        self.assertEqual(counts["total"], len(self.shell.config["sources"]))
        self.assertEqual(counts["idle"], len(self.shell.config["sources"]))
        self.assertEqual(counts["running"], 0)
        self.assertEqual(counts["failed"], 0)

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

    def test_chat_loop_logs_agent_status_rows(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ready", tool_calls=[])
                )
            ]
        )
        with mock.patch("llm.chat_completion", return_value=completion):
            result = self.shell._run_llm_tool_loop(
                [{"role": "user", "content": "status please"}],
                loop_kind="chat",
            )

        rows = db.get_recent_agent_statuses(limit=2)
        self.assertEqual(result, "ready")
        self.assertEqual(rows[0]["phase"], "end")
        self.assertEqual(rows[1]["phase"], "start")
        self.assertEqual(rows[0]["session_id"], self.shell.session_id)

    def test_tool_timeout_returns_error_and_logs_status(self):
        original_timeout = agent_tools.TOOL_TIMEOUTS["search"]
        agent_tools.TOOL_TIMEOUTS["search"] = 1

        def slow_search(**_kwargs):
            time.sleep(2)
            return {"ok": True}

        try:
            with mock.patch.object(self.shell.tools, "search", side_effect=slow_search):
                result = self.shell.tools.execute("search", {"query": "stuck"})
        finally:
            agent_tools.TOOL_TIMEOUTS["search"] = original_timeout

        latest = db.get_latest_agent_status()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "timeout")
        self.assertEqual(result["timeout_seconds"], 1)
        self.assertEqual(latest["phase"], "error")
        self.assertIn("Tool search failed", latest["message"])

    def test_search_tool_starts_one_background_batch_per_source(self):
        completed = []

        def fake_crawl(query, model, max_results, sources, should_stop=None):
            completed.append((query, tuple(sources)))

        with mock.patch.object(self.shell.cli, "perform_crawl", side_effect=fake_crawl):
            result = self.shell.tools.search("egoism", max_results=1, sources=["archive_org", "arxiv"])
            deadline = time.time() + 2
            while time.time() < deadline:
                snapshots = self.shell.tools._batch_snapshot()
                if len(snapshots) == 2 and all(batch["status"] == "complete" for batch in snapshots):
                    break
                time.sleep(0.01)

        self.assertTrue(result["ok"])
        self.assertTrue(result["async"])
        self.assertEqual(len(result["batches"]), 2)
        self.assertIn(("egoism", ("archive_org",)), completed)
        self.assertIn(("egoism", ("arxiv",)), completed)
        statuses = {batch["label"]: batch["status"] for batch in self.shell.tools._batch_snapshot()}
        self.assertEqual(statuses["archive_org / egoism"], "complete")
        self.assertEqual(statuses["arxiv / egoism"], "complete")
        worker_counts = db.get_agent_worker_counts()
        self.assertEqual(worker_counts["total"], len(self.shell.config["sources"]))
        self.assertEqual(worker_counts["idle"], len(self.shell.config["sources"]))
        self.assertEqual(worker_counts["running"], 0)

        with mock.patch.object(self.shell.cli, "perform_crawl", side_effect=fake_crawl):
            self.shell.tools.search("stirner", max_results=1, sources=["archive_org", "arxiv"])
            deadline = time.time() + 2
            while time.time() < deadline:
                counts = db.get_agent_worker_counts()
                if (
                    counts["total"] == len(self.shell.config["sources"])
                    and counts["idle"] == len(self.shell.config["sources"])
                    and counts["running"] == 0
                ):
                    break
                time.sleep(0.01)

        self.assertEqual(db.get_agent_worker_counts()["total"], len(self.shell.config["sources"]))

    def test_background_batch_failure_is_recorded(self):
        def fail_crawl(*_args, **_kwargs):
            raise RuntimeError("archive offline")

        with mock.patch.object(self.shell.cli, "perform_crawl", side_effect=fail_crawl):
            result = self.shell.tools.search("egoism", max_results=1, sources=["archive_org"])
            deadline = time.time() + 2
            while self.shell.tools._batch_snapshot()[0]["status"] == "running" and time.time() < deadline:
                time.sleep(0.01)

        self.assertTrue(result["async"])
        latest = self.shell.tools._batch_snapshot()[0]
        self.assertEqual(latest["status"], "failed")
        self.assertIn("archive offline", latest["error"])
        worker_counts = db.get_agent_worker_counts()
        self.assertEqual(worker_counts["failed"], 1)
        self.assertEqual(worker_counts["idle"], len(self.shell.config["sources"]) - 1)

    def test_cli_backed_tool_capture_streams_visible_output(self):
        visible_output = io.StringIO()
        self.shell.stdout = visible_output

        def fake_handler(_args):
            print("direct url analysis started")
            print("direct url analysis finished")

        output = self.shell.tools._capture(fake_handler, self.shell._namespace())

        self.assertIn("direct url analysis started", output)
        self.assertIn("direct url analysis finished", output)
        self.assertIn("direct url analysis started", visible_output.getvalue())
        self.assertIn("direct url analysis finished", visible_output.getvalue())

    def test_tool_execute_prints_completion_line(self):
        with agent_tools.terminal_theme.console.capture() as capture:
            result = self.shell.tools.execute("backlog", {})

        self.assertTrue(result["ok"])
        self.assertIn("tool backlog complete", capture.get())

    def test_goal_idle_watchdog_logs_idle_status(self):
        original_warning_seconds = agent.IDLE_WARNING_SECONDS
        original_interval = agent.IDLE_WATCHDOG_INTERVAL_SECONDS
        agent.IDLE_WARNING_SECONDS = 1
        agent.IDLE_WATCHDOG_INTERVAL_SECONDS = 0.05
        self.shell._agent_last_activity = time.monotonic() - 2
        self.shell._agent_current_operation = "test operation"

        try:
            cleanup = self.shell._start_goal_idle_watchdog("goal-id")
            time.sleep(0.2)
            cleanup()
        finally:
            agent.IDLE_WARNING_SECONDS = original_warning_seconds
            agent.IDLE_WATCHDOG_INTERVAL_SECONDS = original_interval

        latest = db.get_latest_agent_status()
        self.assertEqual(latest["phase"], "idle")
        self.assertEqual(latest["goal_id"], "goal-id")
        self.assertIn("test operation", latest["message"])

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
