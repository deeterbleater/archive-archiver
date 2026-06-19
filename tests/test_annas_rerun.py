import importlib.util
from pathlib import Path
import tempfile
import unittest

import db


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rerun_annas_affected.py"
SPEC = importlib.util.spec_from_file_location("rerun_annas_affected", SCRIPT_PATH)
rerun_annas_affected = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rerun_annas_affected)


class AnnasRerunScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _add_stubbed_work(self):
        work_id = db.add_work("Stubbed Anna Work", "Ada Author", "anna repair")
        db.add_file(
            work_id=work_id,
            site="annas-archive.org",
            format="PDF",
            url="https://annas-archive.gl/md5/abc123abc123abc123abc123abc123ab",
            download_url="https://annas-archive.gl/md5/abc123abc123abc123abc123abc123ab",
            trust_level="untrusted",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri="file:///tmp/stub.html",
            storage_key="stub.html",
            sha256="stub-sha",
            byte_count=123,
            content_type="text/html; charset=utf-8",
            http_status=200,
            final_url="https://annas-archive.gl/md5/abc123abc123abc123abc123abc123ab",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_skipped(download_id, "plaintext.v2", "stub")
        return work_id

    def test_affected_works_selects_annas_html_page_rows(self):
        work_id = self._add_stubbed_work()

        rows = rerun_annas_affected.affected_works(db_file=db.DB_FILE)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["work_id"], work_id)
        self.assertEqual(rerun_annas_affected.query_for_work(rows[0]), "Stubbed Anna Work Ada Author")

    def test_affected_works_skips_stub_resolved_by_processed_duplicate(self):
        self._add_stubbed_work()
        replacement_id = db.add_work("Stubbed Anna Work", "Ada Author", "anna repair")
        db.add_file(
            work_id=replacement_id,
            site="archive.org",
            format="Text",
            url="https://archive.org/details/stubbedannawork",
            download_url="https://archive.org/download/stubbedannawork/stubbedannawork.txt",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri="file:///tmp/stubbedannawork.txt",
            storage_key="stubbedannawork.txt",
            sha256="replacement-sha",
            byte_count=1024,
            content_type="text/plain",
            http_status=200,
            final_url="https://archive.org/download/stubbedannawork/stubbedannawork.txt",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id,
            "plaintext.v2",
            "file:///tmp/stubbedannawork-plain.txt",
            "text-sha",
            1024,
            "uncategorized",
        )

        rows = rerun_annas_affected.affected_works(db_file=db.DB_FILE)

        self.assertEqual(rows, [])

    def test_alge_command_uses_cycle_with_targeted_sources(self):
        class Args:
            sources = ["annas_archive", "libgen"]
            max_results = 4
            download_limit = 12
            process_limit = 12
            rps = 0.05
            max_mb = 250
            max_domains = 3
            per_domain_limit = 2

        command = rerun_annas_affected.alge_command(Args, "/tmp/queries.txt")

        self.assertIn("/cycle", command)
        self.assertIn("--queries-file /tmp/queries.txt", command)
        self.assertIn("--sources annas_archive libgen", command)
        self.assertIn("--max-results 4", command)
        self.assertIn("--per-domain-limit 2", command)


if __name__ == "__main__":
    unittest.main()
