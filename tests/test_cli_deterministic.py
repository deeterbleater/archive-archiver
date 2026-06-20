from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cli
import db


class CliDeterministicParsingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = str(Path(self.tempdir.name) / "archive_works.db")
        db.init_db()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        self.tempdir.cleanup()

    def test_anarchist_library_crawl_uses_deterministic_parser_before_llm(self):
        url = "https://theanarchistlibrary.org/library/petr-kropotkin-the-conquest-of-bread"
        html = """
        <html>
          <head>
            <title>The Conquest of Bread | The Anarchist Library</title>
            <meta name="author" content="Peter Kropotkin">
          </head>
          <body>
            <h1>The Conquest of Bread</h1>
            <a href="/library/petr-kropotkin-the-conquest-of-bread.muse">Muse</a>
          </body>
        </html>
        """

        with mock.patch("scrapers.search_anarchist_library", return_value=[{"title": "The Conquest of Bread", "url": url}]):
            with mock.patch("scrapers.fetch_url", return_value=html):
                with mock.patch("llm.parse_page_with_llm", side_effect=AssertionError("LLM should not run")):
                    cli.perform_crawl(
                        "conquest bread",
                        model=None,
                        max_results=1,
                        sources=["anarchist_library"],
                    )

        pending = db.get_pending_download_files(limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["format"], "Muse")
        self.assertEqual(pending[0]["download_url"], "https://theanarchistlibrary.org/library/petr-kropotkin-the-conquest-of-bread.muse")


if __name__ == "__main__":
    unittest.main()
