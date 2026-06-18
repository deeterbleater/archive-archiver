import tempfile
import unittest
from pathlib import Path

import archive_plugins
import scrapers


class ArchivePluginTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry = str(Path(self.tempdir.name) / "archive_plugins.json")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_add_plugin_persists_search_configuration(self):
        plugin = archive_plugins.add_plugin(
            name="Local Archive",
            base_url="https://archive.example",
            search_url_template="https://archive.example/find/{query}",
            result_selector=".work",
            link_selector="a.download",
            title_selector="h2",
            path=self.registry,
        )

        plugins = archive_plugins.load_plugins(self.registry)

        self.assertEqual(plugin["slug"], "local_archive")
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["search_url_template"], "https://archive.example/find/{query}")

    def test_search_plugin_extracts_configured_results(self):
        plugin = archive_plugins.add_plugin(
            name="Local Archive",
            base_url="https://archive.example",
            search_url_template="https://archive.example/find?q={query}",
            result_selector=".work",
            link_selector="a.download",
            title_selector="h2",
            path=self.registry,
        )
        html = """
        <html><body>
          <article class="work">
            <h2>Readable Work</h2>
            <a class="download" href="/works/readable">Read</a>
          </article>
        </body></html>
        """
        original_fetch_url = scrapers.fetch_url

        try:
            scrapers.fetch_url = lambda *_args, **_kwargs: html
            rows = archive_plugins.search_plugin(plugin, "readable")
        finally:
            scrapers.fetch_url = original_fetch_url

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Readable Work")
        self.assertEqual(rows[0]["url"], "https://archive.example/works/readable")
        self.assertEqual(rows[0]["trust_level"], "untrusted")


if __name__ == "__main__":
    unittest.main()
