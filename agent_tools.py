import contextlib
import io
import json
from types import SimpleNamespace

import corpus
import db
import downloader
import processor


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "status",
            "description": "Show database and pipeline status.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "backlog",
            "description": "Return pending download, plaintext extraction, and raw archive counts.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Discover archive records for a query and add matching works/files to the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_url",
            "description": "Analyze one archive detail URL and add the work/files to the database.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": "Generate focused search queries for a topic, crawl them, and synthesize a Markdown research report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download",
            "description": "Download pending file records into the raw bucket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "domain_workers": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process",
            "description": "Extract plaintext from downloaded raw files.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_raw",
            "description": "Upload processed raw originals to configured object storage and remove local copies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "keep_local": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_backlog_until_done",
            "description": "Continuously download pending works, process downloaded files, and optionally archive raw originals until no actionable backlog remains or progress stalls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "download_limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "process_limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "archive_raw": {"type": "boolean"},
                    "max_cycles": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_corpus",
            "description": "Build a deterministic corpus bundle from processed plaintext.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "site": {"type": "string"},
                    "query": {"type": "string"},
                    "ordering": {"type": "string", "enum": ["title", "hash", "created", "random"]},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
]


class AppToolRunner:
    def __init__(self, shell):
        self.shell = shell

    def execute(self, name, arguments):
        handlers = {
            "status": self.status,
            "backlog": self.backlog,
            "search": self.search,
            "ingest_url": self.ingest_url,
            "research": self.research,
            "download": self.download,
            "process": self.process,
            "archive_raw": self.archive_raw,
            "run_backlog_until_done": self.run_backlog_until_done,
            "build_corpus": self.build_corpus,
        }
        if name not in handlers:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            return handlers[name](**(arguments or {}))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "tool": name}

    def _capture(self, func, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            func(args)
        return output.getvalue()

    def _namespace(self, **values):
        return self.shell._namespace(**values)

    def status(self):
        stats = db.get_stats()
        return {"ok": True, "stats": stats, "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def backlog(self):
        return {"ok": True, "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def search(self, query, max_results=None, sources=None):
        args = self._namespace(
            query=query,
            max_results=max_results or self.shell.config["max_results"],
            sources=sources or self.shell.config["sources"],
        )
        output = self._capture(self.shell.cli.handle_search, args)
        return {"ok": True, "output": output[-8000:], "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def ingest_url(self, url):
        args = self._namespace(url=url)
        output = self._capture(self.shell.cli.handle_url, args)
        return {"ok": True, "output": output[-8000:], "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def research(self, topic, max_results=None, sources=None):
        args = self._namespace(
            topic=topic,
            max_results=max_results or self.shell.config["max_results"],
            sources=sources or self.shell.config["sources"],
        )
        output = self._capture(self.shell.cli.handle_research, args)
        return {"ok": True, "output": output[-12000:], "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def download(self, limit=None, domain_workers=True):
        limit = limit or self.shell.config["download_limit"]
        max_bytes = self.shell.config["max_mb"] * 1024 * 1024 if self.shell.config["max_mb"] else None
        if domain_workers:
            results = downloader.download_pending_by_domain(
                limit=limit,
                bucket_dir=downloader.DEFAULT_RAW_BUCKET_DIR,
                requests_per_second=self.shell.config["rps"],
                max_bytes=max_bytes,
                max_domains=self.shell.config["max_domains"],
                per_domain_limit=self.shell.config["per_domain_limit"],
                quarantine_dir=downloader.DEFAULT_QUARANTINE_BUCKET_DIR,
            )
        else:
            results = downloader.download_pending(
                limit=limit,
                bucket_dir=downloader.DEFAULT_RAW_BUCKET_DIR,
                requests_per_second=self.shell.config["rps"],
                max_bytes=max_bytes,
                quarantine_dir=downloader.DEFAULT_QUARANTINE_BUCKET_DIR,
            )
        return {"ok": True, "results": results, "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def process(self, limit=None):
        results = processor.process_pending(
            limit=limit or self.shell.config["process_limit"],
            bucket_dir=processor.DEFAULT_TEXT_BUCKET_DIR,
            extractor=processor.EXTRACTOR_VERSION,
        )
        return {"ok": True, "results": results, "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def archive_raw(self, limit=None, keep_local=False):
        results = processor.archive_processed_raws(
            limit=limit or self.shell.config["process_limit"],
            delete_local=not keep_local,
        )
        return {"ok": True, "results": results, "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def run_backlog_until_done(
        self,
        download_limit=None,
        process_limit=None,
        archive_raw=True,
        max_cycles=100,
    ):
        download_limit = download_limit or self.shell.config["download_limit"]
        process_limit = process_limit or self.shell.config["process_limit"]
        history = []
        reason = "complete"

        for cycle in range(1, max_cycles + 1):
            before = db.get_backlog_counts(processor.EXTRACTOR_VERSION)
            print(f"[agent] backlog cycle {cycle}: {before}")
            if (
                before["pending_downloads"] == 0
                and before["pending_extractions"] == 0
                and (not archive_raw or before["pending_raw_archives"] == 0)
            ):
                reason = "complete"
                break

            step = {"cycle": cycle, "before": before}
            if before["pending_downloads"] > 0:
                step["download"] = self.download(limit=download_limit)["results"]
            if before["pending_extractions"] > 0:
                step["process"] = self.process(limit=process_limit)["results"]
            if archive_raw and before["pending_raw_archives"] > 0:
                step["archive_raw"] = self.archive_raw(limit=process_limit)["results"]

            after = db.get_backlog_counts(processor.EXTRACTOR_VERSION)
            step["after"] = after
            history.append(step)

            if after == before:
                reason = "stalled"
                break
        else:
            reason = "max_cycles"

        return {
            "ok": reason == "complete",
            "reason": reason,
            "cycles": len(history),
            "history": history,
            "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION),
        }

    def build_corpus(self, name, category=None, site=None, query=None, ordering="title", limit=None):
        result = corpus.build_corpus(
            name=name,
            category=category,
            site=site,
            query=query,
            ordering_strategy=ordering,
            seed=0,
            limit=limit,
            substitutions_path=None,
            output_dir=corpus.DEFAULT_CORPUS_BUCKET_DIR,
        )
        return {"ok": True, "result": result}


def dumps_tool_result(result):
    return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
