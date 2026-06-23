from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db
import download_unsticker


class DownloadUnstickerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _failed_file(self, title="Broken Download", url="https://archive.example/broken.txt", error="HTTP 504", attempts=1):
        work_id = db.add_work(title, author="Test Author", search_query="broken")
        db.add_file(
            work_id=work_id,
            site="archive.example",
            format="Text",
            url=url,
            download_source="fixture",
            download_url=url,
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        for _index in range(attempts):
            db.mark_download_started(file_id)
        db.mark_download_failed(file_id, error, http_status=504 if "504" in error else None)
        return file_id

    def test_retry_action_requeues_failed_download(self):
        file_id = self._failed_file()
        rows = db.get_stuck_downloads(limit=10)

        actions, requeued = download_unsticker.apply_plan(
            rows,
            {"actions": [{"file_id": file_id, "action": "retry", "reason": "transient"}]},
        )

        self.assertEqual(actions["retry"], 1)
        self.assertEqual(requeued, [file_id])
        pending = db.get_pending_download_files_for_file_ids([file_id], limit=1)
        self.assertEqual(pending[0]["id"], file_id)
        self.assertEqual(db.get_backlog_counts()["failed_downloads"], 0)

    def test_disable_action_removes_terminal_download_from_stuck_set(self):
        file_id = self._failed_file(error="refusing bulk archive torrent as single-work download")
        rows = db.get_stuck_downloads(limit=10)

        actions, requeued = download_unsticker.apply_plan(
            rows,
            {"actions": [{"file_id": file_id, "action": "disable", "reason": "bulk torrent"}]},
        )

        self.assertEqual(actions["disable"], 1)
        self.assertEqual(requeued, [])
        self.assertEqual(db.get_stuck_downloads(limit=10), [])
        self.assertEqual(db.get_pending_download_files_for_file_ids([file_id], limit=1), [])

    def test_run_once_downloads_only_requeued_files(self):
        file_id = self._failed_file()
        other_work = db.add_work("Other Pending", author="Test Author", search_query="other")
        db.add_file(
            work_id=other_work,
            site="archive.example",
            format="Text",
            url="https://archive.example/other.txt",
            download_source="fixture",
            download_url="https://archive.example/other.txt",
        )

        plan = {"actions": [{"file_id": file_id, "action": "retry", "reason": "transient"}]}
        with mock.patch.object(download_unsticker, "glm_plan", return_value=plan):
            with mock.patch.object(download_unsticker.downloader, "download_rows_by_domain", return_value={"downloaded": 1, "failed": 0, "skipped": 0}) as download:
                result = download_unsticker.run_once(download_limit=10)

        self.assertEqual(result["plan_source"], "glm")
        self.assertEqual(result["requeued"], 1)
        downloaded_rows = download.call_args.args[0]
        self.assertEqual([row["id"] for row in downloaded_rows], [file_id])

    def test_replace_url_requires_public_http_url(self):
        file_id = self._failed_file()
        rows = db.get_stuck_downloads(limit=10)

        actions, requeued = download_unsticker.apply_plan(
            rows,
            {"actions": [{"file_id": file_id, "action": "replace_url", "download_url": "file:///tmp/book.txt", "reason": "bad"}]},
        )

        self.assertEqual(actions["invalid"], 1)
        self.assertEqual(requeued, [])
        self.assertEqual(db.get_backlog_counts()["failed_downloads"], 1)


if __name__ == "__main__":
    unittest.main()
