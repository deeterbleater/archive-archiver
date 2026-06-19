import json
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import corpus
import db
import downloader
import processor
import text_validator


class PipelineStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _add_processed_text(self, title, text, category="philosophy", site="example.org"):
        root = Path(self.tempdir.name)
        text_path = root / f"{title.lower().replace(' ', '-')}.txt"
        normalized = corpus.normalize_text(text)
        text_path.write_text(normalized + "\n", encoding="utf-8")
        text_sha256 = corpus._sha256_text(normalized)

        work_id = db.add_work(title=title, author="Test Author", search_query="test query")
        db.add_file(
            work_id=work_id,
            site=site,
            format="Text",
            url=f"https://{site}/{title}",
            file_size=f"{len(text)} bytes",
            download_source="fixture",
            download_url=f"https://{site}/{title}.txt",
        )

        pending = db.get_pending_download_files(limit=10)
        file_id = pending[-1]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri=text_path.resolve().as_uri(),
            storage_key=text_path.name,
            sha256="raw-" + text_sha256[:16],
            byte_count=len(text.encode("utf-8")),
            content_type="text/plain",
            http_status=200,
            final_url=f"https://{site}/{title}.txt",
            etag='"fixture"',
            last_modified="Tue, 01 Jan 2030 00:00:00 GMT",
        )

        extraction_row = db.get_pending_extractions(limit=10)[-1]
        download_id = extraction_row["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id=download_id,
            extractor="plaintext.v2",
            text_uri=text_path.resolve().as_uri(),
            text_sha256=text_sha256,
            char_count=len(normalized),
            category=category,
            warnings="fixture",
        )
        return text_sha256

    def test_download_and_extraction_state_counts(self):
        self._add_processed_text("Alpha", "A compact philosophy fixture.")

        stats = db.get_stats()

        self.assertEqual(stats["downloads_by_status"], {"downloaded": 1})
        self.assertEqual(stats["extractions_by_status"], {"processed": 1})

    def test_gzip_text_extracts_plaintext_instead_of_bytes(self):
        path = Path(self.tempdir.name) / "fixture.txt.gz"
        path.write_bytes(gzip.compress(b"This is readable compressed text.\nSecond line."))

        text, mode = processor.extract_plaintext(path, format_hint="Text")

        self.assertEqual(mode, "text.gz")
        self.assertIn("readable compressed text", text)
        self.assertNotIn("\x8b", text)

    def test_local_validator_rejects_binary_garbage(self):
        result = text_validator.heuristic_quality("\x8b\b\b\x00\x00\x00garbage")

        self.assertEqual(result["status"], "unusable")
        self.assertFalse(result["needs_llm"])

    def test_validator_parses_json_object_with_trailing_text(self):
        payload = text_validator._parse_json_object('{"usable": true, "score": 0.8, "reason": "ok"}\nextra')

        self.assertTrue(payload["usable"])
        self.assertEqual(payload["score"], 0.8)

    def test_unusable_texts_are_excluded_from_corpus_candidates(self):
        self._add_processed_text("Bad Bytes", "Readable fixture used for db state.")
        extraction = db.get_processed_extractions(limit=1)[0]

        db.mark_text_quality(
            extraction["extraction_id"],
            "unusable",
            score=0.02,
            reason="binary garbage",
            model="local-heuristic",
        )

        self.assertEqual(db.get_processed_extractions(limit=10), [])

    def test_remove_unusable_marks_extraction_skipped_and_deletes_text(self):
        self._add_processed_text("Delete Bad Bytes", "Readable fixture used for cleanup.")
        extraction = db.get_processed_extractions(limit=1)[0]
        text_path = Path(extraction["text_uri"].replace("file://", ""))
        db.mark_text_quality(
            extraction["extraction_id"],
            "unusable",
            score=0.02,
            reason="binary garbage",
            model="local-heuristic",
        )

        result = text_validator.remove_unusable(verbose=False)

        self.assertEqual(result["removed"], 1)
        self.assertFalse(text_path.exists())
        conn = db.get_connection()
        row = conn.execute("SELECT status, text_uri FROM extractions WHERE id = ?", (extraction["extraction_id"],)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "skipped")
        self.assertIsNone(row["text_uri"])

    def test_text_validation_can_run_with_worker_pool(self):
        self._add_processed_text("Worker Alpha", "Readable fixture used for worker pool.")
        self._add_processed_text("Worker Beta", "Another readable fixture used for worker pool.")

        def fake_validate(row, model=None, use_llm=True):
            return {
                "status": "usable",
                "score": 0.9,
                "reason": f"ok {row['title']}",
                "model": model,
            }

        with mock.patch("text_validator.validate_text", side_effect=fake_validate):
            results = text_validator.validate_pending(limit=2, workers=2, model="fixture/model")

        self.assertEqual(results["checked"], 2)
        self.assertEqual(results["usable"], 2)
        rows = db.get_processed_extractions(limit=2)
        self.assertTrue(all(row["quality_status"] == "usable" for row in rows))

    def test_failed_download_falls_off_pending_list(self):
        work_id = db.add_work(title="Broken Link", author="Test Author", search_query="failures")
        db.add_file(
            work_id=work_id,
            site="broken.example",
            format="PDF",
            url="https://broken.example/detail",
            download_source="fixture",
            download_url="https://broken.example/missing.pdf",
        )
        pending = db.get_pending_download_files(limit=10)
        file_id = pending[0]["id"]

        db.mark_download_started(file_id)
        db.mark_download_failed(file_id, "HTTP 404", http_status=404)

        self.assertEqual(db.get_pending_download_files(limit=10), [])
        self.assertEqual(db.get_stats()["downloads_by_status"], {"failed": 1})
        self.assertEqual(db.get_backlog_counts()["pending_downloads"], 0)
        self.assertEqual(db.get_backlog_counts()["failed_downloads"], 1)

    def test_pending_downloads_choose_one_preferred_file_per_work(self):
        work_id = db.add_work(title="Many Formats", author="Test Author", search_query="formats")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="PDF",
            url="https://example.org/detail",
            file_size="10 MB",
            download_source="fixture",
            download_url="https://example.org/work.pdf",
        )
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="Text",
            url="https://example.org/detail",
            file_size="500 KB",
            download_source="fixture",
            download_url="https://example.org/work.txt",
        )

        pending = db.get_pending_download_files(limit=10)

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["format"], "Text")

    def test_failed_extractions_fall_off_pending_backlog(self):
        work_id = db.add_work(title="Bad Extraction", author="Test Author", search_query="failures")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="PDF",
            url="https://example.org/bad",
            download_source="fixture",
            download_url="https://example.org/bad.pdf",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri=(Path(self.tempdir.name) / "bad.pdf").resolve().as_uri(),
            storage_key="bad.pdf",
            sha256="raw-sha",
            byte_count=3,
            content_type="application/pdf",
            http_status=200,
            final_url="https://example.org/bad.pdf",
        )
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_failed(download_id, "plaintext.v2", "broken pdf")

        self.assertEqual(db.get_pending_extractions(limit=10, extractor="plaintext.v2"), [])
        self.assertEqual(db.get_backlog_counts("plaintext.v2")["pending_extractions"], 0)
        self.assertEqual(db.get_stats()["extractions_by_status"], {"failed": 1})

    def test_dynamic_category_is_created_when_defaults_do_not_match(self):
        row = {
            "title": "Thelema Ritual Notes",
            "author": "Test Author",
            "site": "fixture.example",
        }
        text = "thelema ritual thelema ceremonial magick aeon will star"

        category = processor.categorize_text(row, text)
        categories = {item["name"]: item for item in db.get_categories()}

        self.assertEqual(category, "thelema")
        self.assertTrue(categories["thelema"]["dynamic"])
        self.assertIn("ritual", categories["thelema"]["keywords"])

    def test_dynamic_category_rejects_pdf_artifacts_and_ocr_fragments(self):
        row = {
            "title": "",
            "author": "Test Author",
            "site": "fixture.example",
            "search_query": "",
        }
        text = "endobj stream flatedecode a9dj a9dj a9dj aewn aewn aewn b12c b12c"

        category = processor.categorize_text(row, text)

        self.assertEqual(category, "uncategorized")
        self.assertFalse(db.is_valid_dynamic_category_name("endobj"))
        self.assertFalse(db.is_valid_dynamic_category_name("a9dj"))
        self.assertFalse(db.is_valid_dynamic_category_name("abusch"))
        self.assertTrue(db.is_valid_dynamic_category_name("thelema"))

    def test_default_categories_are_seeded(self):
        categories = {item["name"] for item in db.get_categories()}

        self.assertIn("philosophy", categories)
        self.assertIn("anarchism", categories)

    def test_existing_archived_work_does_not_reenter_pending_downloads(self):
        self._add_processed_text("Archived Once", "Already in plaintext.")
        work_id = db.add_work(title="Archived Once", author="Test Author", search_query="duplicate")

        self.assertTrue(db.work_has_archive_activity(work_id))

        db.add_file(
            work_id=work_id,
            site="mirror.example",
            format="Text",
            url="https://mirror.example/archived-once",
            file_size="20 bytes",
            download_source="fixture",
            download_url="https://mirror.example/archived-once.txt",
        )

        pending = db.get_pending_download_files(limit=10)

        self.assertFalse(any(row["work_id"] == work_id for row in pending))

    def test_corpus_build_is_deterministic_and_records_substitutions(self):
        self._add_processed_text("Beta", "The self owns the text.")
        self._add_processed_text("Alpha", "The text owns the order.")

        substitutions_path = Path(self.tempdir.name) / "subs.json"
        substitutions_path.write_text(json.dumps({"text": "corpus"}), encoding="utf-8")
        output_dir = Path(self.tempdir.name) / "corpora"

        first = corpus.build_corpus(
            name="fixture",
            query="test",
            ordering_strategy="title",
            substitutions_path=str(substitutions_path),
            output_dir=str(output_dir),
        )
        second = corpus.build_corpus(
            name="fixture",
            query="test",
            ordering_strategy="title",
            substitutions_path=str(substitutions_path),
            output_dir=str(output_dir),
        )

        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(first["item_count"], 2)
        manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual([item["title"] for item in manifest["items"]], ["Alpha", "Beta"])
        corpus_text = Path(first["corpus_path"]).read_text(encoding="utf-8")
        self.assertIn("The corpus owns the order.", corpus_text)
        self.assertIn("The self owns the corpus.", corpus_text)

    def test_download_domain_prefers_download_url_host(self):
        row = {
            "site": "source.example",
            "url": "https://source.example/detail",
            "download_url": "https://cdn.example/files/book.txt",
        }

        self.assertEqual(downloader.download_domain(row), "cdn.example")

    def test_downloader_rejects_annas_archive_html_stub(self):
        class FakeResponse:
            status_code = 200
            url = "https://annas-archive.gl/md5/abc123abc123abc123abc123abc123ab"
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, chunk_size=1):
                yield b"<html>Anna's Archive page</html>"

        row = {
            "id": 1,
            "work_id": 1,
            "site": "annas-archive.org",
            "format": "PDF",
            "download_url": "https://annas-archive.gl/md5/abc123abc123abc123abc123abc123ab",
        }

        with mock.patch("downloader.requests.get", return_value=FakeResponse()):
            with self.assertRaisesRegex(ValueError, "Anna's Archive page URL"):
                downloader.download_file(
                    row,
                    bucket_dir=str(Path(self.tempdir.name) / "raw"),
                    quarantine_dir=str(Path(self.tempdir.name) / "quarantine"),
                )

    def test_domain_workers_process_one_queue_per_domain(self):
        alpha_work_id = db.add_work(title="Alpha Domain Fixture", author="Test Author", search_query="domains")
        db.add_file(
            work_id=alpha_work_id,
            site="alpha.example",
            format="Text",
            url="https://alpha.example/detail",
            download_source="fixture",
            download_url="https://alpha.example/a.txt",
        )
        beta_work_id = db.add_work(title="Beta Domain Fixture", author="Test Author", search_query="domains")
        db.add_file(
            work_id=beta_work_id,
            site="beta.example",
            format="Text",
            url="https://beta.example/detail",
            download_source="fixture",
            download_url="https://beta.example/b.txt",
        )

        seen_domains = []
        original_download_file = downloader.download_file

        def fake_download_file(row, bucket_dir, limiter, max_bytes, quarantine_dir=None):
            seen_domains.append(downloader.download_domain(row))
            return {
                "bucket_uri": "file:///tmp/fake.txt",
                "storage_key": f"{row['id']}.txt",
                "sha256": f"sha-{row['id']}",
                "byte_count": 12,
                "content_type": "text/plain",
                "http_status": 200,
                "final_url": row["download_url"],
                "etag": None,
                "last_modified": None,
                "scan_status": "clean",
                "scan_engine": "fixture",
                "scan_signature": None,
                "quarantine_uri": "file:///tmp/quarantine.txt",
            }

        try:
            downloader.download_file = fake_download_file
            results = downloader.download_pending_by_domain(limit=10, requests_per_second=1000)
        finally:
            downloader.download_file = original_download_file

        self.assertEqual(results, {"downloaded": 2, "failed": 0, "skipped": 0})
        self.assertEqual(sorted(seen_domains), ["alpha.example", "beta.example"])
        self.assertEqual(db.get_stats()["downloads_by_status"], {"downloaded": 2})


if __name__ == "__main__":
    unittest.main()
