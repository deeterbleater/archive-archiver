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

    def test_public_sources_include_arxiv_and_substack(self):
        self.assertIn("arxiv", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("substack", cli.DEFAULT_PUBLIC_SOURCES)
        self.assertIn("arxiv", cli.ALL_SOURCES)
        self.assertIn("substack", cli.ALL_SOURCES)


if __name__ == "__main__":
    unittest.main()
