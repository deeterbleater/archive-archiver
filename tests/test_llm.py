import unittest
from types import SimpleNamespace
from unittest import mock

import llm
import terminal_theme


class LlmTests(unittest.TestCase):
    def test_openrouter_app_headers_use_alge_crawler_label(self):
        self.assertEqual(llm.OPENROUTER_APP_HEADERS["X-Title"], "ALGE Crawler")
        self.assertEqual(
            llm.OPENROUTER_APP_HEADERS["HTTP-Referer"],
            "https://github.com/deeterbleater/archive-archiver",
        )

    def test_parse_page_with_llm_prints_analysis_bubbles(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"title":"Fixture","author":"Tester","files":[]}'
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=mock.Mock(return_value=response)
                )
            )
        )

        with mock.patch("llm.get_openrouter_client", return_value=client):
            with terminal_theme.console.capture() as capture:
                parsed = llm.parse_page_with_llm(
                    "Fixture page text with one download link.",
                    "https://example.org/fixture",
                    model="fixture/model",
                )

        output = capture.get()
        self.assertEqual(parsed["title"], "Fixture")
        self.assertIn("system -> analyzer", output)
        self.assertIn("crawler -> analyzer", output)
        self.assertIn("analyzer -> crawler", output)
        self.assertIn("Parsed analyzer JSON successfully", output)
        client.chat.completions.create.assert_called_once()
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["extra_headers"],
            llm.OPENROUTER_APP_HEADERS,
        )


if __name__ == "__main__":
    unittest.main()
