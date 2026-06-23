from io import StringIO
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import db
import terminal_theme
import tui


def render_text(renderable, width=80):
    console = Console(file=StringIO(), width=width, theme=terminal_theme.THEME, highlight=False, record=True)
    console.print(renderable)
    return console.export_text()


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def test_tui_renders_core_sections(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE", "line2", "line3", "line4", "line5"], height=40))

        output = capture.get()
        self.assertIn("Pipeline", output)
        self.assertIn("Work Queue", output)
        self.assertIn("workers", output)
        self.assertIn("Work Queue", output)
        self.assertIn("Activity", output)
        self.assertIn("Next Actions", output)
        self.assertIn("Operator Cue", output)
        self.assertIn("o overview", output)
        self.assertNotIn("/help", output)

    def test_compact_tui_keeps_essential_sections(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE", "line2", "line3", "line4", "line5"], height=20))

        output = capture.get()
        self.assertIn("Pipeline", output)
        self.assertIn("Backlog", output)
        self.assertIn("Operator Cue", output)
        self.assertNotIn("Controls", output)
        self.assertNotIn("tmux persistent", output)

    def test_compact_summary_panel_contains_key_status(self):
        stats = {
            "downloads_by_status": {"downloaded": 3},
            "extractions_by_status": {"processed": 2},
            "raw_archives_by_status": {"archived": 1},
        }
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 1,
        }
        workers = {"total": 1, "running": 0, "idle": 1, "failed": 0}
        scans = {}

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._compact_summary_panel(stats, backlog, workers, scans, freshness={"age": "1m", "age_seconds": 60, "stale": False}))

        output = capture.get()
        self.assertIn("Pipeline / Backlog", output)
        self.assertIn("queue", output)
        self.assertIn("seen", output)

    def test_queue_view_focuses_queue_without_controls(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="queue"))

        output = capture.get()
        self.assertIn("Queue", output)
        self.assertNotIn("Controls", output)

    def test_overview_queue_panel_combines_counts_and_preview(self):
        work_id = db.add_work("Queued Work", author="Example Author", search_query="queue")
        db.add_file(
            work_id,
            site="archive.example",
            format="PDF",
            url="https://archive.example/work.pdf",
            download_url="https://archive.example/work.pdf",
        )
        backlog = db.get_backlog_counts()

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._overview_queue_panel(backlog))

        output = capture.get()
        self.assertIn("Work Queue", output)
        self.assertIn("download", output)
        self.assertIn("Queued Work", output)

    def test_controls_view_renders_in_compact_height(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=24, view="controls"))

        output = capture.get()
        self.assertIn("Command Reference", output)
        self.assertIn("Next Actions", output)
        self.assertIn("/download", output)
        self.assertIn("/cycle", output)

    def test_tight_compact_queue_prioritizes_content_and_cue(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=20, view="queue", interactive=True))

        output = capture.get()
        self.assertIn("Queue", output)
        self.assertIn("Operator Cue", output)
        self.assertNotIn("Next Actions", output)

    def test_narrow_queue_uses_short_empty_stage_labels(self):
        output = render_text(tui.render_tui(["ALGE"], height=20, view="queue", interactive=True, width=50), width=50)

        self.assertIn("No files need text.", output)
        self.assertIn("No raw files ready.", output)
        self.assertNotIn("No downloaded files waiting", output)

    def test_tight_compact_selected_views_fit_twenty_lines(self):
        for width in (50, 80):
            for view in ("overview", "queue", "failures", "activity", "controls"):
                with self.subTest(view=view, width=width):
                    output = render_text(tui.render_tui(["ALGE"], height=20, view=view, interactive=True, width=width), width=width)

                    self.assertLessEqual(len(output.splitlines()), 20)

    def test_full_controls_view_keeps_reference_grid(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="controls"))

        output = capture.get()
        self.assertIn("Controls", output)
        self.assertIn("command", output)
        self.assertIn("role", output)
        self.assertIn("use", output)
        self.assertIn("discover", output)
        self.assertIn("archive", output)
        self.assertIn("find and queue new public-domain", output)
        self.assertIn("works", output)
        self.assertIn("extract readable text", output)

    def test_compact_command_reference_uses_two_column_reference(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._compact_command_reference())

        output = capture.get()
        self.assertIn("Command Reference", output)
        self.assertIn("/search", output)
        self.assertIn("/download", output)
        self.assertIn("discover / find", output)
        self.assertNotIn("find and queue new public-domain works", output)

    def test_view_aliases_are_normalized(self):
        self.assertEqual(tui._normalize_view("triage"), "failures")
        self.assertEqual(tui._normalize_view("logs"), "activity")
        self.assertEqual(tui._normalize_view("unknown"), "overview")

    def test_view_detail_uses_normalized_view(self):
        label, description = tui._view_detail("triage")

        self.assertEqual(label, "failures")
        self.assertIn("triage", description)

    def test_view_navigation_cycles_forward_and_backward(self):
        self.assertEqual(tui._view_for_key("\t", "overview"), "queue")
        self.assertEqual(tui._view_for_key("n", "controls"), "overview")
        self.assertEqual(tui._view_for_key("p", "overview"), "controls")
        self.assertEqual(tui._view_for_key("h", "activity"), "failures")
        self.assertEqual(tui._view_for_key("f", "overview"), "failures")
        self.assertEqual(tui._view_for_key("x", "queue"), "queue")

    def test_view_bar_marks_active_view(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._view_bar("failures", interactive=True))

        output = capture.get()
        self.assertIn("f fail", output)
        self.assertIn("tab/n next", output)
        self.assertIn("p/h prev", output)
        self.assertIn("q quit", output)

    def test_view_bar_explains_noninteractive_view_selection(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._view_bar("queue"))

        output = capture.get()
        self.assertIn("w queue", output)
        self.assertIn("use --view", output)
        self.assertNotIn("q quit", output)

    def test_narrow_interactive_header_keeps_brand_and_nav_visible(self):
        console = Console(width=60, theme=terminal_theme.THEME, highlight=False)

        with console.capture() as capture:
            console.print(tui.render_tui(["ALGE"], height=20, view="overview", interactive=True, width=60))

        output = capture.get()
        self.assertIn("ALGE", output)
        self.assertIn("f fail", output)
        self.assertIn("c controls", output)

    def test_very_narrow_header_uses_compact_nav(self):
        output = render_text(tui.render_tui(["ALGE"], height=20, view="overview", interactive=True, width=50), width=50)

        self.assertIn("ALGE  state", output)
        self.assertIn(" o ", output)
        self.assertIn(" c ", output)
        self.assertIn("tab/n   p/h   q", output)
        self.assertNotIn("ALGEstate", output)
        self.assertNotIn("c controls", output)

    def test_tui_renders_recent_agent_activity(self):
        db.add_agent_status("working through backlog", phase="update")

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE", "line2", "line3", "line4", "line5"], height=40))

        self.assertIn("working through backlog", capture.get())

    def test_activity_summary_counts_recent_labels(self):
        db.add_agent_status("Batch search-123 complete: archive_org / Example", phase="complete")
        db.add_agent_status("Batch search-456 failed: archive_org / Bad", phase="failed")
        db.add_agent_status("waiting for work", phase="idle")

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._activity_summary_panel(limit=5))

        output = capture.get()
        self.assertIn("Activity Summary", output)
        self.assertIn("done", output)
        self.assertIn("fail", output)
        self.assertIn("wait", output)

    def test_activity_count_styles_match_activity_meaning(self):
        self.assertEqual(tui._activity_count_style("done", 1), "success")
        self.assertEqual(tui._activity_count_style("run", 1), "tool")
        self.assertEqual(tui._activity_count_style("wait", 1), "warning")
        self.assertEqual(tui._activity_count_style("fail", 1), "danger")
        self.assertEqual(tui._activity_count_style("run", 0), "muted")

    def test_activity_summary_explains_recent_failures(self):
        db.add_agent_status("Batch search-456 failed: archive_org / Bad", phase="failed")

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._activity_summary_panel(limit=5))

        self.assertIn("recent failures need review", capture.get())

    def test_activity_decision_prioritizes_failures_then_running_work(self):
        failure = tui._activity_decision({"done": 2, "run": 1, "wait": 0, "fail": 1, "note": 0})
        running = tui._activity_decision({"done": 0, "run": 2, "wait": 0, "fail": 0, "note": 0})

        self.assertIn("failures need review", failure.plain)
        self.assertIn("watch for done/fail", running.plain)

    def test_activity_decision_guides_idle_and_complete_states(self):
        waiting = tui._activity_decision({"done": 0, "run": 0, "wait": 1, "fail": 0, "note": 0})
        done = tui._activity_decision({"done": 3, "run": 0, "wait": 0, "fail": 0, "note": 0})

        self.assertIn("choose the next command", waiting.plain)
        self.assertIn("recent completions", done.plain)

    def test_full_activity_view_includes_activity_summary(self):
        db.add_agent_status("working through backlog", phase="update")

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="activity"))

        self.assertIn("Activity Summary", capture.get())

    def test_activity_time_label_prefers_elapsed_age(self):
        now = datetime(2026, 6, 22, 7, 0, 0, tzinfo=timezone.utc)

        label = tui._activity_time_label("2026-06-22 06:45:00", now=now)

        self.assertEqual(label, "15m")

    def test_activity_time_label_falls_back_to_time_suffix(self):
        label = tui._activity_time_label("not-a-timestamp 12:34:56")

        self.assertEqual(label, "12:34:56")

    def test_tui_header_describes_active_view(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="controls"))

        output = capture.get()
        self.assertIn("controls", output)
        self.assertIn("slash commands for direct operation", output)

    def test_queue_header_focus_calls_out_bottleneck(self):
        work_id = db.add_work("Needs Download", author="Example Author", search_query="queue")
        db.add_file(
            work_id,
            site="archive.example",
            format="PDF",
            url="https://archive.example/work.pdf",
            download_url="https://archive.example/work.pdf",
        )

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="queue"))

        self.assertIn("downloads ready", capture.get())

    def test_failures_header_focus_counts_triage_issues(self):
        stats = {"extractions_by_status": {"failed": 2}, "raw_archives_by_status": {}}
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 1,
        }
        workers = {"total": 1, "running": 0, "idle": 0, "failed": 1}
        scans = {"infected": 1}

        focus = tui._view_focus_text("failures", backlog, workers, scans, stats)

        self.assertEqual(focus.plain, "5 issues in triage")

    def test_controls_header_focus_shows_primary_command(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="controls"))

        self.assertIn("primary /search", capture.get())

    def test_operator_cue_prioritizes_failures(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 2,
            "pending_raw_archives": 0,
            "failed_downloads": 1,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        cue = tui._operation_hint(backlog, workers)

        self.assertIn("pipeline failures", cue.plain)

    def test_operator_cue_recommends_processing_before_downloads(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 2,
            "pending_raw_archives": 0,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        cue = tui._operation_hint(backlog, workers)

        self.assertIn("/process --limit 25", cue.plain)

    def test_queue_view_operator_cue_recommends_download_when_queue_has_downloads(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        cue = tui._operation_hint(backlog, workers, view="queue")

        self.assertIn("/download --limit 25 --domain-workers", cue.plain)

    def test_queue_command_examples_prioritize_current_bottleneck(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 2,
            "pending_raw_archives": 1,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._command_examples("queue", backlog=backlog, workers=workers))

        lines = [line.strip() for line in capture.get().splitlines()]
        process_line = next(line for line in lines if "/process --limit 25" in line)

        self.assertIn("process", process_line)
        self.assertIn("now", process_line)

    def test_next_actions_do_not_look_like_number_key_shortcuts(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._command_examples("overview", limit=3))

        output = capture.get()
        self.assertIn("now", output)
        self.assertIn("next", output)
        self.assertIn("later", output)
        self.assertNotIn("1.", output)

    def test_tmux_interactive_view_bar_shows_enter_paste_hint(self):
        with patch.dict(tui.os.environ, {"TMUX": "/tmp/tmux"}):
            with terminal_theme.console.capture() as capture:
                terminal_theme.console.print(tui._view_bar("queue", interactive=True))

        self.assertIn("enter paste", capture.get())

    def test_primary_action_matches_prioritized_queue_command(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 2,
            "pending_raw_archives": 1,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        label, command, hint = tui._primary_action("queue", backlog=backlog, workers=workers)

        self.assertEqual(label, "process")
        self.assertEqual(command, "/process --limit 25")
        self.assertIn("extract", hint)

    def test_full_tui_shows_primary_command_tray(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="queue"))

        output = capture.get()
        self.assertIn("Primary Command", output)
        self.assertIn("/download --limit 25 --domain-workers", output)

    def test_compact_interactive_footer_explains_enter_action_in_tmux(self):
        with patch.dict(tui.os.environ, {"TMUX": "/tmp/tmux"}):
            output = render_text(tui.render_tui(["ALGE"], height=20, view="queue", interactive=True), width=80)

        self.assertIn("enter paste", output)
        self.assertIn("/download --limit 25 --domain-workers", output)

    def test_notice_line_accepts_plain_text_for_compatibility(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._notice_line("pasted /status"))

        self.assertIn("pasted /status", capture.get())

    def test_footer_renders_success_and_error_notices(self):
        stats = {
            "downloads_by_status": {},
            "extractions_by_status": {},
            "raw_archives_by_status": {},
        }
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}
        scans = {}

        with terminal_theme.console.capture() as success_capture:
            terminal_theme.console.print(tui._footer(backlog, workers, scans, stats, "success", notice=("success", "pasted /status; review and press Enter")))
        with terminal_theme.console.capture() as error_capture:
            terminal_theme.console.print(tui._footer(backlog, workers, scans, stats, "danger", notice=("danger", "tmux: pane not found")))

        self.assertIn("pasted /status", success_capture.get())
        self.assertIn("review and press Enter", success_capture.get())
        self.assertIn("tmux: pane not found", error_capture.get())

    def test_send_command_to_agent_requires_tmux(self):
        with patch.dict(tui.os.environ, {}, clear=True):
            ok, message = tui._send_command_to_agent("/status")

        self.assertFalse(ok)
        self.assertIn("not running inside tmux", message)

    def test_send_command_to_agent_pastes_without_executing(self):
        class Result:
            returncode = 0
            stderr = ""

        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return Result()

        with patch.dict(tui.os.environ, {"TMUX": "/tmp/tmux", "ALGE_TUI_AGENT_TARGET": "{down-of}"}):
            with patch("tui.subprocess.run", side_effect=fake_run):
                ok, message = tui._send_command_to_agent("/status")

        self.assertTrue(ok)
        self.assertIn("pasted /status", message)
        self.assertIn("review and press Enter", message)
        self.assertEqual(calls[0], ["tmux", "send-keys", "-t", "{down-of}", "-l", "/status"])
        self.assertEqual(calls[1], ["tmux", "select-pane", "-t", "{down-of}"])

    def test_failures_view_operator_cue_mentions_triage(self):
        backlog = {
            "pending_downloads": 4,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 1,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        cue = tui._operation_hint(backlog, workers, view="failures")

        self.assertIn("Triage", cue.plain)

    def test_failures_primary_action_reviews_before_retrying_raw_archive(self):
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}
        stats = {"raw_archives_by_status": {"failed": 3}}

        label, command, hint = tui._primary_action("failures", backlog=backlog, workers=workers, stats=stats)

        self.assertEqual(label, "status")
        self.assertEqual(command, "/status")
        self.assertIn("failed", hint)

    def test_failure_summary_panel_breaks_down_failed_stages(self):
        stats = {
            "extractions_by_status": {"failed": 2},
            "raw_archives_by_status": {"failed": 3},
        }
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 4,
        }
        workers = {"total": 1, "running": 0, "idle": 0, "failed": 1}
        scans = {"infected": 5}

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._failure_summary_panel(stats, backlog, workers, scans))

        output = capture.get()
        self.assertIn("Failure Summary", output)
        self.assertIn("downloads", output)
        self.assertIn("text", output)
        self.assertIn("raw", output)
        self.assertIn("quarantine", output)

    def test_full_failures_view_includes_failure_summary(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="failures"))

        self.assertIn("Failure Summary", capture.get())

    def test_controls_operator_cue_is_short_and_direct(self):
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 0,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        cue = tui._operation_hint(backlog, workers, view="controls")

        self.assertEqual(cue.plain, "Type a slash command in the agent pane.")

    def test_failures_command_examples_include_status(self):
        backlog = {
            "pending_downloads": 0,
            "pending_extractions": 0,
            "pending_raw_archives": 0,
            "failed_downloads": 1,
        }
        workers = {"total": 0, "running": 0, "idle": 0, "failed": 0}

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._command_examples("failures", backlog=backlog, workers=workers))

        output = capture.get()
        self.assertIn("Next Actions", output)
        self.assertIn("/status", output)

    def test_bar_shows_completed_and_pending_segments(self):
        bar = tui._bar(1, 4, width=4)

        self.assertIn("█", bar.plain)
        self.assertIn("░", bar.plain)

    def test_percent_clamps_to_complete(self):
        self.assertEqual(tui._percent(12, 10), "100%")

    def test_freshness_formats_recent_activity_age(self):
        latest = {
            "created_at": "2026-06-22 06:59:00",
            "message": "Batch search-abcd complete: archive_org / Example",
        }
        now = datetime(2026, 6, 22, 7, 0, 30, tzinfo=timezone.utc)

        freshness = tui._freshness(latest_status=latest, now=now)

        self.assertEqual(freshness["age"], "1m")
        self.assertFalse(freshness["stale"])
        self.assertEqual(freshness["message"], "archive_org / Example")

    def test_freshness_marks_stale_activity(self):
        latest = {
            "created_at": "2026-06-22 06:00:00",
            "message": "quiet",
        }
        now = datetime(2026, 6, 22, 7, 0, 0, tzinfo=timezone.utc)

        freshness = tui._freshness(latest_status=latest, now=now)

        self.assertEqual(freshness["age"], "1h")
        self.assertTrue(freshness["stale"])

    def test_age_from_timestamp_formats_elapsed_time(self):
        now = datetime(2026, 6, 22, 7, 0, 0, tzinfo=timezone.utc)

        age = tui._age_from_timestamp("2026-06-22 06:55:00", now=now)

        self.assertEqual(age, "5m")

    def test_stage_badge_keeps_status_visible(self):
        badge = tui._stage_badge("download", status=504)

        self.assertEqual(badge.plain, "download:504")

    def test_stage_badge_allows_blank_continuation_rows(self):
        badge = tui._stage_badge("")

        self.assertEqual(badge.plain, "")

    def test_meta_label_combines_age_and_source(self):
        row = {
            "updated_at": "2026-06-22 06:55:00",
            "site": "archive.example",
        }

        label = tui._meta_label(row, limit=40)

        self.assertIn("archive.example", label)

    def test_activity_message_removes_batch_id_noise(self):
        message = tui._activity_message("Batch search-242c2d6d complete: archive_org / Robert Heinlein")

        self.assertEqual(message, "archive_org / Robert Heinlein")

    def test_activity_label_marks_failures(self):
        label, style = tui._activity_label("end", "failed archive_org / Example")

        self.assertEqual(label, "fail")
        self.assertEqual(style, "danger")

    def test_recent_failed_downloads_feed_triage(self):
        work_id = db.add_work("Broken Download", author="Example Author", search_query="broken")
        db.add_file(
            work_id,
            site="archive.example",
            format="PDF",
            url="https://archive.example/broken.pdf",
            download_url="https://archive.example/broken.pdf",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_failed(file_id, "timeout while downloading", http_status=504)

        rows = db.get_recent_failed_downloads(limit=1)

        self.assertEqual(rows[0]["title"], "Broken Download")
        self.assertEqual(rows[0]["http_status"], 504)

    def test_recent_pipeline_failures_include_text_failures(self):
        work_id = db.add_work("Broken Text", author="Example Author", search_query="broken")
        db.add_file(
            work_id,
            site="archive.example",
            format="PDF",
            url="https://archive.example/text.pdf",
            download_url="https://archive.example/text.pdf",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id,
            bucket_uri="file:///tmp/text.pdf",
            storage_key="raw/text.pdf",
            sha256="abc",
            byte_count=12,
            content_type="application/pdf",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_failed(download_id, "plaintext.v2", "pdf parser failed")

        rows = db.get_recent_pipeline_failures(limit=3)

        self.assertTrue(any(row["stage"] == "text" and row["title"] == "Broken Text" for row in rows))

    def test_triage_panel_renders_failed_downloads(self):
        work_id = db.add_work("Broken Download", author="Example Author", search_query="broken")
        db.add_file(
            work_id,
            site="archive.example",
            format="PDF",
            url="https://archive.example/broken.pdf",
            download_url="https://archive.example/broken.pdf",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_failed(file_id, "timeout while downloading", http_status=504)

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui._triage_panel())

        output = capture.get()
        self.assertIn("Broken Download", output)
        self.assertIn("download:504", output)

    def test_attention_panel_shows_six_triage_rows_when_roomy(self):
        with patch.object(tui, "_triage_panel", return_value="triage") as triage:
            panel = tui._attention_panel({}, compact=False)

        self.assertEqual(panel, "triage")
        triage.assert_called_once_with(limit=6)


if __name__ == "__main__":
    unittest.main()
