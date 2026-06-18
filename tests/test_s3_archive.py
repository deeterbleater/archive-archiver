import tempfile
import unittest
from pathlib import Path

import db
import processor
import s3_storage


class RawArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def test_object_key_uses_prefix(self):
        self.assertEqual(
            s3_storage.object_key("archive.org/1/2/raw.pdf", prefix="raw-originals"),
            "raw-originals/archive.org/1/2/raw.pdf",
        )

    def test_archive_raw_after_extraction_uploads_and_deletes_local(self):
        raw_path = Path(self.tempdir.name) / "raw.txt"
        raw_path.write_text("raw fixture", encoding="utf-8")
        work_id = db.add_work("Raw Fixture", "Author", "query")
        db.add_file(
            work_id=work_id,
            site="example.org",
            format="Text",
            url="https://example.org/raw",
            download_url="https://example.org/raw.txt",
        )
        file_id = db.get_pending_download_files(limit=1)[0]["id"]
        db.mark_download_started(file_id)
        db.mark_download_succeeded(
            file_id=file_id,
            bucket_uri=raw_path.resolve().as_uri(),
            storage_key="example.org/1/1/raw.txt",
            sha256="raw-sha",
            byte_count=11,
        )
        row = db.get_raw_archive_candidates(limit=1)[0] if db.get_raw_archive_candidates(limit=1) else None
        self.assertIsNone(row)

        download_id = db.get_pending_extractions(limit=1)[0]["id"]
        db.mark_extraction_started(download_id, "plaintext.v2")
        db.mark_extraction_succeeded(
            download_id=download_id,
            extractor="plaintext.v2",
            text_uri=(Path(self.tempdir.name) / "text.txt").resolve().as_uri(),
            text_sha256="text-sha",
            char_count=10,
            category="fixture",
        )
        row = db.get_raw_archive_candidates(limit=1)[0]

        class FakeS3Client:
            def put_file(self, path, key, content_type=None, metadata=None):
                self.path = path
                self.key = key
                return {
                    "uri": f"s3://fixture/{key}",
                    "sha256": "raw-sha",
                    "bytes": 11,
                    "etag": '"fixture"',
                }

        original_client = s3_storage.S3Client
        try:
            s3_storage.S3Client = FakeS3Client
            result = processor.archive_raw_after_extraction(row, delete_local=True)
        finally:
            s3_storage.S3Client = original_client

        self.assertEqual(result["uri"], "s3://fixture/raw-originals/example.org/1/1/raw.txt")
        self.assertFalse(raw_path.exists())
        self.assertEqual(db.get_stats()["raw_archives_by_status"], {"archived": 1})


if __name__ == "__main__":
    unittest.main()
