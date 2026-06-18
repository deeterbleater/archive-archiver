import tempfile
import unittest
from pathlib import Path

import db


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

        import api
        self.api = api

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _add_fixture(self):
        work_id = db.add_work("API Fixture", "Test Author", "test query")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="Text",
            url="https://example.org/work",
            file_size="12 bytes",
            download_source="fixture",
            download_url="https://example.org/work.txt",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri="file:///tmp/work.txt",
            storage_key="work.txt",
            sha256="raw-sha",
            byte_count=12,
            content_type="text/plain",
            http_status=200,
            final_url="https://example.org/work.txt",
            scan_status="clean",
            scan_engine="fixture",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id=download_id,
            extractor="plaintext.v2",
            text_uri="file:///tmp/work-text.txt",
            text_sha256="text-sha",
            char_count=42,
            category="philosophy",
        )
        return work_id

    def test_summary_and_breakdowns(self):
        self._add_fixture()

        summary = self.api.summary()
        sites = self.api.site_breakdown()
        categories = self.api.category_breakdown()
        scans = self.api.scan_status()
        trust = self.api.trust_breakdown()
        raw_archives = self.api.raw_archive_status()

        self.assertEqual(summary["total_works"], 1)
        self.assertEqual(summary["downloaded_bytes"], 12)
        self.assertEqual(summary["extracted_chars"], 42)
        self.assertEqual(summary["clean_scans"], 1)
        self.assertEqual(summary["quarantined_files"], 0)
        self.assertEqual(summary["archived_raw_files"], 0)
        self.assertEqual(summary["deleted_local_raw_files"], 0)
        self.assertEqual(sites[0]["site"], "example.org")
        self.assertEqual(categories[0]["category"], "philosophy")
        self.assertEqual(scans[0]["status"], "clean")
        self.assertEqual(trust[0]["trust_level"], "trusted")
        self.assertEqual(raw_archives[0]["status"], "local")

    def test_summary_separates_pending_and_failed_downloads(self):
        work_id = db.add_work("Download States", "Test Author", "states")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="Text",
            url="https://example.org/pending",
            download_source="fixture",
            download_url="https://example.org/pending.txt",
        )
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="PDF",
            url="https://example.org/failed",
            download_source="fixture",
            download_url="https://example.org/failed.pdf",
        )
        failed_file_id = db.get_pending_download_files(limit=10)[1]["id"]
        db.mark_download_started(failed_file_id)
        db.mark_download_failed(failed_file_id, "HTTP 404", http_status=404)

        summary = self.api.summary()

        self.assertEqual(summary["pending_download_files"], 1)
        self.assertEqual(summary["failed_download_files"], 1)
        self.assertEqual(summary["downloads_by_status"], {"failed": 1})

    def test_work_drilldown(self):
        work_id = self._add_fixture()

        payload = self.api.get_work(work_id)

        self.assertEqual(payload["title"], "API Fixture")
        self.assertEqual(payload["files"][0]["download_status"], "downloaded")
        self.assertEqual(payload["files"][0]["extraction_status"], "processed")
        self.assertEqual(payload["files"][0]["scan_status"], "clean")
        self.assertEqual(payload["files"][0]["trust_level"], "trusted")


if __name__ == "__main__":
    unittest.main()
