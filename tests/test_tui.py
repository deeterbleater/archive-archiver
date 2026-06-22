import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import db
import terminal_theme
import tui


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
        self.assertIn("Backlog", output)
        self.assertIn("workers", output)
        self.assertIn("Queue", output)
        self.assertIn("Activity", output)
        self.assertIn("Actions", output)
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

    def test_queue_view_focuses_queue_without_controls(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="queue"))

        output = capture.get()
        self.assertIn("Queue", output)
        self.assertNotIn("Controls", output)

    def test_controls_view_renders_in_compact_height(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=20, view="controls"))

        output = capture.get()
        self.assertIn("Controls", output)
        self.assertIn("Actions", output)
        self.assertIn("/download", output)
        self.assertIn("/cycle", output)

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
        self.assertIn("f failures", output)
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

    def test_tui_renders_recent_agent_activity(self):
        db.add_agent_status("working through backlog", phase="update")

        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE", "line2", "line3", "line4", "line5"], height=40))

        self.assertIn("working through backlog", capture.get())

    def test_tui_header_describes_active_view(self):
        with terminal_theme.console.capture() as capture:
            terminal_theme.console.print(tui.render_tui(["ALGE"], height=40, view="controls"))

        output = capture.get()
        self.assertIn("controls", output)
        self.assertIn("slash commands for direct operation", output)

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
        self.assertIn("Actions", output)
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


if __name__ == "__main__":
    unittest.main()
