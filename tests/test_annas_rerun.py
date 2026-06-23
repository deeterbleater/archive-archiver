import importlib.util
from unittest import mock
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

    def _file_id_for_work(self, work_id):
        conn = db.get_connection()
        row = conn.execute("SELECT id FROM files WHERE work_id = ? ORDER BY id DESC LIMIT 1", (work_id,)).fetchone()
        conn.close()
        return row["id"]

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
        file_id = self._file_id_for_work(work_id)
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

    def test_affected_works_can_resume_after_work_id(self):
        work_id = self._add_stubbed_work()

        rows = rerun_annas_affected.affected_works(
            db_file=db.DB_FILE,
            start_after_work_id=work_id,
        )

        self.assertEqual(rows, [])

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
        file_id = self._file_id_for_work(replacement_id)
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

    def test_affected_works_skips_stub_resolved_by_title_variant(self):
        work_id = db.add_work(
            "Kingdom of Fear: Loathsome Secrets of a Star-Crossed Child in the Final Days of the American Century",
            "Hunter S. Thompson",
            "anna repair",
        )
        db.add_file(
            work_id=work_id,
            site="annas-archive.org",
            format="PDF",
            url="https://annas-archive.gl/md5/def123def123def123def123def123de",
            download_url="https://annas-archive.gl/md5/def123def123def123def123def123de",
            trust_level="untrusted",
        )
        file_id = self._file_id_for_work(work_id)
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri="file:///tmp/kingdom-stub.html",
            storage_key="kingdom-stub.html",
            sha256="kingdom-stub-sha",
            byte_count=123,
            content_type="text/html; charset=utf-8",
            http_status=200,
            final_url="https://annas-archive.gl/md5/def123def123def123def123def123de",
        )

        replacement_id = db.add_work("Kingdom of Fear", "Hunter S. Thompson", "anna repair")
        db.add_file(
            work_id=replacement_id,
            site="archive.org",
            format="Text",
            url="https://archive.org/details/kingdomoffear",
            download_url="https://archive.org/download/kingdomoffear/kingdomoffear.txt",
        )
        replacement_file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(replacement_file_id)
        db.mark_download_succeeded(
            file_id=replacement_file_id,
            bucket_uri="file:///tmp/kingdomoffear.txt",
            storage_key="kingdomoffear.txt",
            sha256="kingdom-replacement-sha",
            byte_count=1024,
            content_type="text/plain",
            http_status=200,
            final_url="https://archive.org/download/kingdomoffear/kingdomoffear.txt",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id,
            "plaintext.v2",
            "file:///tmp/kingdomoffear-plain.txt",
            "kingdom-text-sha",
            1024,
            "literature",
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

    def test_alge_command_can_disable_global_download_queue(self):
        class Args:
            sources = ["annas_archive", "archive_org"]
            max_results = 4
            download_limit = 12
            process_limit = 12
            rps = 0.05
            max_mb = 250
            max_domains = 3
            per_domain_limit = 2

        command = rerun_annas_affected.alge_command(
            Args,
            "/tmp/queries.txt",
            download_limit=0,
            process_limit=0,
        )

        self.assertIn("--download-limit 0", command)
        self.assertIn("--process-limit 0", command)

    def test_targeted_workflow_uses_query_work_ids(self):
        work_id = self._add_stubbed_work()
        discovered_id = db.add_work("Discovered Anna Work", "Ada Author", "Stubbed Anna Work Ada Author")

        class Args:
            download_limit = 7
            process_limit = 5
            rps = 0.05
            max_mb = 250
            max_domains = 3
            per_domain_limit = 2

        batch = [{"work_id": work_id, "title": "Stubbed Anna Work", "author": "Ada Author"}]

        with mock.patch.object(rerun_annas_affected.downloader, "download_work_ids_by_domain", return_value={"downloaded": 1, "failed": 0, "skipped": 0}) as download:
            with mock.patch.object(rerun_annas_affected.processor, "process_pending_for_work_ids", return_value={"processed": 1, "failed": 0, "skipped": 0}) as process:
                code = rerun_annas_affected.run_targeted_workflow(Args, batch)

        self.assertEqual(code, 0)
        self.assertEqual(download.call_args.args[0], [work_id, discovered_id])
        self.assertEqual(process.call_args.args[0], [work_id, discovered_id])


if __name__ == "__main__":
    unittest.main()
