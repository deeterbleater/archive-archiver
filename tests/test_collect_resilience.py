from types import SimpleNamespace
import unittest
from unittest import mock

import cli


def collect_args(**overrides):
    values = {
        "queries_file": None,
        "query": ["alpha", "beta"],
        "sources": ["archive_org"],
        "model": None,
        "max_results": 1,
        "download_limit": 5,
        "process_limit": 5,
        "archive_raw_limit": 5,
        "raw_bucket_dir": "bucket/raw",
        "quarantine_dir": "bucket/quarantine",
        "text_bucket_dir": "bucket/text",
        "rps": 1000,
        "max_mb": 10,
        "max_domains": 1,
        "per_domain_limit": 1,
        "extractor": "plaintext.test",
        "once": True,
        "sleep_seconds": 0,
        "error_sleep_seconds": 1,
        "max_error_sleep_seconds": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CollectResilienceTests(unittest.TestCase):
    def test_collect_continues_after_query_failure(self):
        calls = []

        def fake_crawl(query, *_args, **_kwargs):
            calls.append(query)
            if query == "alpha":
                raise RuntimeError("search failed")

        args = collect_args()
        with mock.patch("cli.perform_crawl", side_effect=fake_crawl):
            with mock.patch("cli.downloader.download_pending_by_domain", return_value={"downloaded": 1, "failed": 0, "skipped": 0}) as download:
                with mock.patch("cli.processor.process_pending", return_value={"processed": 1, "failed": 0, "skipped": 0}) as process:
                    with mock.patch("cli.processor.archive_processed_raws", return_value={"archived": 1, "failed": 0, "skipped": 0}) as archive:
                        errors = cli._run_collect_cycle(args, ["alpha", "beta"], cycle=1)

        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(len(errors), 1)
        download.assert_called_once()
        process.assert_called_once()
        archive.assert_called_once()

    def test_collect_summarizes_phase_failure_without_raising(self):
        args = collect_args()
        with mock.patch("cli.perform_crawl", return_value=None):
            with mock.patch("cli.downloader.download_pending_by_domain", side_effect=RuntimeError("downloader exploded")):
                with mock.patch("cli.processor.process_pending", return_value={"processed": 0, "failed": 0, "skipped": 0}) as process:
                    errors = cli._run_collect_cycle(args, ["alpha"], cycle=1)

        self.assertEqual(len(errors), 1)
        process.assert_called_once()

    def test_auto_queries_prioritize_sparse_categories(self):
        categories = [
            {"name": "literature", "count": 50, "chars": 1000},
            {"name": "philosophy", "count": 0, "chars": 0},
            {"name": "history", "count": 2, "chars": 200},
        ]

        with mock.patch("cli.db.get_categories", return_value=categories):
            queries = cli.build_auto_queries(limit=5, cycle=1, extra_queries=["Asimov public domain"])

        self.assertIn("ethics public domain philosophy", queries)
        self.assertIn("metaphysics public domain", queries)
        self.assertIn("historical accounts public domain", queries)

    def test_auto_runs_one_dynamic_cycle(self):
        args = collect_args(query=None)
        args.query_limit = 3

        with mock.patch("cli.build_auto_queries", return_value=["alpha", "beta"]) as planner:
            with mock.patch("cli._run_collect_cycle", return_value=[]) as run_cycle:
                cli.handle_auto(args)

        planner.assert_called_once()
        run_cycle.assert_called_once_with(args, ["alpha", "beta"], 1)

    def test_auto_loop_honors_stop_callback_before_cycle(self):
        args = collect_args(query=None, once=False, should_stop=lambda: True)
        args.query_limit = 3

        with mock.patch("cli._run_collect_cycle") as run_cycle:
            cli.handle_auto(args)

        run_cycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
