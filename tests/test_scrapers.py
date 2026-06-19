import unittest

import cli
import scrapers


class ScraperSourceTests(unittest.TestCase):
    def test_arxiv_feed_parser_returns_pdf_rows(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2401.00001v1</id>
            <title> Mutual Aid in Distributed Systems </title>
            <summary> A fixture abstract. </summary>
            <author><name>Ada Example</name></author>
            <author><name>Max Example</name></author>
            <link href="http://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
            <link title="pdf" href="http://arxiv.org/pdf/2401.00001v1" rel="related" type="application/pdf"/>
          </entry>
        </feed>
        """

        rows = scrapers.parse_arxiv_feed(xml)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "arxiv.org")
        self.assertEqual(rows[0]["format"], "PDF")
        self.assertEqual(rows[0]["author"], "Ada Example, Max Example")
        self.assertEqual(rows[0]["download_url"], "http://arxiv.org/pdf/2401.00001v1")

    def test_substack_parser_returns_post_html_rows(self):
        html = """
        <html><body>
          <a href="https://example.substack.com/p/mutual-aid-notes">Mutual Aid Notes</a>
          <a href="https://example.substack.com/about">About</a>
          <a href="https://substack.com/@writer/p/public-archives">Public Archives</a>
        </body></html>
        """

        rows = scrapers.parse_substack_search(html, query="mutual aid")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "example.substack.com")
        self.assertEqual(rows[0]["format"], "HTML")
        self.assertEqual(rows[0]["download_url"], "https://example.substack.com/p/mutual-aid-notes")

    def test_substack_feed_parser_returns_html_rows(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel>
            <title>Fixture Stack</title>
            <item>
              <title>Archive Notes</title>
              <link>https://fixture.substack.com/p/archive-notes</link>
              <dc:creator>Fixture Writer</dc:creator>
            </item>
          </channel>
        </rss>
        """

        rows = scrapers.parse_substack_feed(xml, "https://fixture.substack.com")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "fixture.substack.com")
        self.assertEqual(rows[0]["author"], "Fixture Writer")
        self.assertEqual(rows[0]["download_source"], "Substack RSS HTML")

    def test_public_sources_include_arxiv_substack_annas_archive_and_libgen(self):
        self.assertIn("arxiv", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("substack", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("annas_archive", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("libgen", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("arxiv", cli.ALL_SOURCES)
        self.assertIn("substack", cli.ALL_SOURCES)
        self.assertIn("annas_archive", cli.ALL_SOURCES)
        self.assertIn("libgen", cli.ALL_SOURCES)

    def test_libgen_search_uses_only_libgen_mirrors(self):
        captured = {}

        def fake_slum_search(query, mirrors=None):
            captured["query"] = query
            captured["mirrors"] = mirrors
            return []

        original = scrapers.search_slum_archives
        try:
            scrapers.search_slum_archives = fake_slum_search
            rows = scrapers.search_libgen("religion")
        finally:
            scrapers.search_slum_archives = original

        self.assertEqual(rows, [])
        self.assertEqual(captured["query"], "religion")
        self.assertTrue(captured["mirrors"])
        self.assertTrue(all(mirror["group"] == "libgen_plus" for mirror in captured["mirrors"]))

    def test_libgen_link_extractor_skips_navigation_links(self):
        html = """
        <html><body>
          <a href="/index.php?req=religion&curtab=f">Files 197004</a>
          <a href="/setlang.php?req=religion&lang=ru">RU</a>
          <a href="edition.php?id=138387302">The re-emergence of emergence: science to religion</a>
          <a href="/file.php?id=93590192">2 MB</a>
        </body></html>
        """
        mirror = {"name": "LibGen Fixture", "group": "libgen_plus", "url": "https://libgen.example/"}

        rows = scrapers._extract_detail_links(
            html,
            "https://libgen.example/index.php?req=religion",
            "religion",
            mirror,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "The re-emergence of emergence: science to religion")
        self.assertEqual(rows[0]["url"], "https://libgen.example/edition.php?id=138387302")

    def test_annas_archive_search_tries_multiple_mirrors(self):
        calls = []

        def fake_fetch(url, *_args, **_kwargs):
            calls.append(url)
            if "mirror-one" in url:
                return ""
            return '<html><body><a href="/md5/abc123"><h3>Mirror Work</h3></a></body></html>'

        original_fetch = scrapers.fetch_url
        try:
            scrapers.fetch_url = fake_fetch
            rows = scrapers.search_annas_archive(
                "mirror work",
                mirrors=["https://mirror-one.example", "https://mirror-two.example"],
            )
        finally:
            scrapers.fetch_url = original_fetch

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Mirror Work")
        self.assertEqual(rows[0]["url"], "https://mirror-two.example/md5/abc123")
        self.assertEqual(len(calls), 2)

    def test_select_best_file_prefers_plaintext_over_heavier_formats(self):
        rows = [
            {
                "format": "PDF",
                "file_size": "12 MB",
                "url": "https://example.org/detail",
                "download_url": "https://example.org/work.pdf",
            },
            {
                "format": "EPUB",
                "file_size": "2 MB",
                "url": "https://example.org/detail",
                "download_url": "https://example.org/work.epub",
            },
            {
                "format": "Text",
                "file_size": "900 KB",
                "url": "https://example.org/detail",
                "download_url": "https://example.org/work.txt",
            },
        ]

        best = scrapers.select_best_file(rows)

        self.assertEqual(best["format"], "Text")


if __name__ == "__main__":
    unittest.main()
