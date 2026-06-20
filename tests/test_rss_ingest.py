from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db
import rss_ingest


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Fixture Feed</title>
    <item>
      <title>Readable Essay</title>
      <link>https://example.org/essay.html</link>
      <guid>essay-1</guid>
      <dc:creator>Ada Writer</dc:creator>
    </item>
  </channel>
</rss>
"""


ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom Essay</title>
    <id>tag:example.org,2026:atom-essay</id>
    <author><name>Atom Author</name></author>
    <link rel="alternate" href="https://example.org/atom-essay"/>
  </entry>
</feed>
"""


class RssIngestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def test_parse_rss_items(self):
        rows = rss_ingest.parse_feed(RSS_FIXTURE, feed_url="https://example.org/feed", feed_name="Fixture")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Readable Essay")
        self.assertEqual(rows[0]["author"], "Ada Writer")
        self.assertEqual(rows[0]["url"], "https://example.org/essay.html")
        self.assertEqual(rows[0]["format"], "HTML")

    def test_parse_atom_items(self):
        rows = rss_ingest.parse_feed(ATOM_FIXTURE, feed_url="https://example.org/atom", feed_name="Fixture")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Atom Essay")
        self.assertEqual(rows[0]["author"], "Atom Author")

    def test_archive_feed_items_dedupes_by_feed_item(self):
        feed = {"url": "https://example.org/feed", "name": "Fixture Feed", "trust_level": "trusted"}
        items = rss_ingest.parse_feed(RSS_FIXTURE, feed_url=feed["url"], feed_name=feed["name"])

        first = rss_ingest.archive_feed_items(feed, items)
        second = rss_ingest.archive_feed_items(feed, items)

        self.assertEqual(first["archived"], 1)
        self.assertEqual(second["seen"], 1)
        self.assertEqual(len(db.get_pending_download_files(limit=10)), 1)

    def test_ingest_feeds_fetches_configured_feeds(self):
        feed_path = Path(self.tempdir.name) / "feeds.json"
        feed_path.write_text(
            '{"feeds":[{"name":"Fixture Feed","url":"https://example.org/feed","trust_level":"trusted"}]}',
            encoding="utf-8",
        )

        with mock.patch("rss_ingest.fetch_feed", return_value=RSS_FIXTURE):
            summary = rss_ingest.ingest_feeds(path=feed_path, limit_per_feed=10)

        self.assertEqual(summary["feeds"], 1)
        self.assertEqual(summary["archived"], 1)


if __name__ == "__main__":
    unittest.main()
