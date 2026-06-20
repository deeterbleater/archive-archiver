import tempfile
import unittest
from pathlib import Path

import archive_plugins
import db
import text_munger


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()
        self.old_archive_registry = archive_plugins.DEFAULT_REGISTRY_PATH
        archive_plugins.DEFAULT_REGISTRY_PATH = str(Path(self.tempdir.name) / "archive_plugins.json")

        import api
        self.api = api

    def tearDown(self):
        archive_plugins.DEFAULT_REGISTRY_PATH = self.old_archive_registry
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def _add_fixture(self):
        text_path = Path(self.tempdir.name) / "work-text.txt"
        text_path.write_text("First line of fixture text.\nSecond line for review.\n", encoding="utf-8")
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
            text_uri=text_path.resolve().as_uri(),
            text_sha256="text-sha",
            char_count=text_path.stat().st_size,
            category="philosophy",
        )
        return work_id

    def _add_text_fixture(self, title, text, category="philosophy", site="example.org"):
        text_path = Path(self.tempdir.name) / f"{title.lower().replace(' ', '-')}.txt"
        text_path.write_text(text, encoding="utf-8")
        work_id = db.add_work(title, "Test Author", "viewer search")
        db.add_file(
            work_id=work_id,
            site=site,
            format="Text",
            url=f"https://{site}/{title}",
            file_size=f"{len(text)} bytes",
            download_source="fixture",
            download_url=f"https://{site}/{title}.txt",
        )
        file_id = db.get_pending_download_files(limit=10)[-1]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri=text_path.resolve().as_uri(),
            storage_key=text_path.name,
            sha256=f"raw-{title}",
            byte_count=len(text.encode("utf-8")),
            content_type="text/plain",
            http_status=200,
        )
        download_id = db.get_pending_extractions(limit=10, extractor="plaintext.v2")[-1]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id=download_id,
            extractor="plaintext.v2",
            text_uri=text_path.resolve().as_uri(),
            text_sha256=f"text-{title}",
            char_count=len(text),
            category=category,
        )
        return db.get_extraction(download_id, "plaintext.v2")["id"]

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
        self.assertEqual(summary["extracted_chars"], 52)
        self.assertEqual(summary["clean_scans"], 1)
        self.assertEqual(summary["quarantined_files"], 0)
        self.assertEqual(summary["archived_raw_files"], 0)
        self.assertEqual(summary["deleted_local_raw_files"], 0)
        self.assertEqual(sites[0]["site"], "example.org")
        self.assertEqual(categories[0]["category"], "philosophy")
        self.assertIn("description", categories[0])
        self.assertIn("dynamic", categories[0])
        self.assertEqual(scans[0]["status"], "clean")
        self.assertEqual(trust[0]["trust_level"], "trusted")
        self.assertEqual(raw_archives[0]["status"], "local")

    def test_site_breakdown_includes_known_and_configured_zero_count_archives(self):
        archive_plugins.add_plugin(
            name="No Files Yet",
            base_url="https://new-archive.example",
            path=archive_plugins.DEFAULT_REGISTRY_PATH,
        )

        sites = {row["site"]: row for row in self.api.site_breakdown()}

        self.assertIn("annas-archive.org", sites)
        self.assertIn("libgen.bz", sites)
        self.assertIn("new-archive.example", sites)
        self.assertEqual(sites["new-archive.example"]["files"], 0)
        self.assertEqual(sites["new-archive.example"]["works"], 0)

    def test_category_breakdown_rolls_invalid_dynamic_names_into_uncategorized(self):
        work_id = db.add_work("Junk Category Fixture", "Test Author", "test query")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="Text",
            url="https://example.org/junk",
            file_size="12 bytes",
            download_source="fixture",
            download_url="https://example.org/junk.txt",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri="file:///tmp/junk.txt",
            storage_key="junk.txt",
            sha256="raw-sha",
            byte_count=12,
            content_type="text/plain",
            http_status=200,
            final_url="https://example.org/junk.txt",
        )
        db.ensure_category("a9dj", keywords=["a9dj", "b12c"], dynamic=True)
        download_id = db.get_pending_extractions(limit=1, extractor="plaintext.v2")[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id=download_id,
            extractor="plaintext.v2",
            text_uri="file:///tmp/junk-text.txt",
            text_sha256="text-sha",
            char_count=123,
            category="a9dj",
        )

        categories = self.api.category_breakdown()
        dimensions = self.api.dimensions()

        self.assertFalse(any(item["category"] == "a9dj" for item in categories))
        self.assertFalse(any(item["category"] == "a9dj" for item in dimensions["categories"]))
        uncategorized = next(item for item in categories if item["category"] == "uncategorized")
        self.assertEqual(uncategorized["chars"], 123)

    def test_summary_separates_pending_and_failed_downloads(self):
        pending_work_id = db.add_work("Pending State", "Test Author", "states")
        db.add_file(
            work_id=pending_work_id,
            site="example.org",
            format="Text",
            url="https://example.org/pending",
            download_source="fixture",
            download_url="https://example.org/pending.txt",
        )
        failed_work_id = db.add_work("Failed State", "Test Author", "states")
        db.add_file(
            work_id=failed_work_id,
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

    def test_text_review_endpoints_return_metadata_and_preview(self):
        self._add_fixture()

        rows = self.api.list_texts(q="fixture", category="philosophy", limit=50, offset=0)
        payload = self.api.get_text(rows[0]["extraction_id"], max_chars=12)

        self.assertEqual(rows[0]["title"], "API Fixture")
        self.assertEqual(rows[0]["category"], "philosophy")
        self.assertEqual(rows[0]["quality_status"], "unvalidated")
        self.assertEqual(payload["text"], "First line o")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["preview_chars"], 12)

    def test_text_review_endpoint_can_return_munged_variant(self):
        self._add_fixture()
        row = self.api.list_texts(q="fixture", category="philosophy", limit=50, offset=0)[0]
        source_path = Path(row["text_uri"].replace("file://", ""))
        munged_path = Path(self.tempdir.name) / "munged.txt"
        munged_path.write_text("Munged training text.\n", encoding="utf-8")
        db.mark_text_munge_succeeded(
            extraction_id=row["extraction_id"],
            munger_version=text_munger.MUNGER_VERSION,
            source_text_sha256=row["text_sha256"],
            munged_text_uri=munged_path.resolve().as_uri(),
            munged_text_sha256="munged-sha",
            char_count=len("Munged training text."),
            rules_json="[]",
            stats_json='{"cleaned_chars":21}',
        )

        rows = self.api.list_texts(q="fixture", category="philosophy", limit=50, offset=0)
        payload = self.api.get_text(row["extraction_id"], max_chars=200, variant="munged")

        self.assertTrue(rows[0]["has_munged"])
        self.assertEqual(rows[0]["munged_char_count"], len("Munged training text."))
        self.assertEqual(payload["text_variant"], "munged")
        self.assertEqual(payload["text"], "Munged training text.\n")
        self.assertNotEqual(source_path.read_text(encoding="utf-8"), payload["text"])

    def test_text_search_endpoint_uses_full_text_index_and_snippets(self):
        self._add_text_fixture(
            "Occult Search Fixture",
            "This archive body discusses Thelema, ritual practice, and ceremonial magick.",
            category="occult",
        )
        self._add_text_fixture(
            "Political Search Fixture",
            "This archive body discusses unions, labor, and mutual aid.",
            category="anarchism",
        )

        payload = self.api.search_texts(q="Thelema ritual", mode="all", limit=10, offset=0)

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["title"], "Occult Search Fixture")
        self.assertEqual(payload["results"][0]["category"], "occult")
        self.assertIn("<mark>", payload["results"][0]["body_snippet"])
        self.assertIn("rank", payload["results"][0])

    def test_new_processed_text_is_indexed_immediately(self):
        extraction_id = self._add_text_fixture(
            "Fresh Index Fixture",
            "A brand new work about lodestar indexing should be searchable immediately.",
        )

        with db.get_connection() as conn:
            meta = conn.execute(
                "SELECT extraction_id, body_chars FROM text_search_meta WHERE extraction_id = ?",
                (extraction_id,),
            ).fetchone()
            match = conn.execute(
                "SELECT rowid FROM text_search_index WHERE text_search_index MATCH ?",
                ("lodestar",),
            ).fetchone()

        self.assertIsNotNone(meta)
        self.assertEqual(meta["extraction_id"], extraction_id)
        self.assertGreater(meta["body_chars"], 0)
        self.assertEqual(match["rowid"], extraction_id)

    def test_text_search_endpoint_supports_phrase_mode(self):
        self._add_text_fixture("Phrase Fixture", "A precise phrase about golden dawn ritual survives here.")

        payload = self.api.search_texts(q="golden dawn", mode="phrase", limit=10, offset=0)

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["title"], "Phrase Fixture")

    def test_text_search_excludes_unusable_texts(self):
        extraction_id = self._add_text_fixture("Rejected Fixture", "Unreadable marker should disappear from search.")
        db.mark_text_quality(extraction_id, "unusable", score=0.1, reason="bad", model="fixture")

        payload = self.api.search_texts(q="Unreadable marker", mode="all", limit=10, offset=0)

        self.assertEqual(payload["total"], 0)

    def test_summary_counts_rejected_text_validation(self):
        self._add_fixture()
        row = self.api.list_texts(q="fixture", category="philosophy", limit=50, offset=0)[0]
        db.mark_text_quality(row["extraction_id"], "unusable", score=0.01, reason="garbage", model="fixture")

        summary = self.api.summary()

        self.assertEqual(summary["processed_texts"], 0)
        self.assertEqual(summary["rejected_texts"], 1)

    def test_dimensions_include_dynamic_category_metadata(self):
        db.ensure_category(
            "thelema",
            description="Auto-created during extraction.",
            keywords=["thelema", "ritual"],
            dynamic=True,
        )

        payload = self.api.dimensions()
        category = next(item for item in payload["categories"] if item["category"] == "thelema")

        self.assertEqual(category["description"], "Auto-created during extraction.")
        self.assertEqual(category["dynamic"], 1)
        self.assertIn("thelema", category["keywords_json"])

    def test_agent_status_endpoints_return_latest_and_recent_rows(self):
        first = db.add_agent_status(
            "Starting goal loop 1 with minimax/minimax-m3.",
            session_id="test-session",
            loop_kind="goal",
            phase="start",
            model="minimax/minimax-m3",
            goal_id="goal-1",
        )
        second = db.add_agent_status(
            "Finished goal loop 1 after 2 tool calls.",
            session_id="test-session",
            loop_kind="goal",
            phase="end",
            model="minimax/minimax-m3",
            goal_id="goal-1",
        )

        latest = self.api.latest_agent_status()
        recent = self.api.recent_agent_status(limit=2)

        self.assertEqual(latest["id"], second["id"])
        self.assertEqual(latest["message"], second["message"])
        self.assertEqual(recent[0]["id"], second["id"])
        self.assertEqual(recent[1]["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
