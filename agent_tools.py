import contextlib
import io
import json
import os
import re
import signal
import threading
from types import SimpleNamespace
import urllib.parse

import archive_plugins
import corpus
import db
import downloader
import goals
import processor
import requests
from bs4 import BeautifulSoup


DEFAULT_TOOL_TIMEOUT_SECONDS = int(os.getenv("ALGE_TOOL_TIMEOUT_SECONDS", "180"))
TOOL_TIMEOUTS = {
    "web_search": int(os.getenv("ALGE_WEB_SEARCH_TIMEOUT_SECONDS", "45")),
    "search": int(os.getenv("ALGE_SEARCH_TIMEOUT_SECONDS", str(DEFAULT_TOOL_TIMEOUT_SECONDS))),
    "ingest_url": int(os.getenv("ALGE_INGEST_URL_TIMEOUT_SECONDS", str(DEFAULT_TOOL_TIMEOUT_SECONDS))),
    "research": int(os.getenv("ALGE_RESEARCH_TIMEOUT_SECONDS", "240")),
}


class ToolTimeoutError(TimeoutError):
    pass


@contextlib.contextmanager
def _tool_timeout(tool_name):
    seconds = TOOL_TIMEOUTS.get(tool_name)
    if not seconds or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(_signum, _frame):
        raise ToolTimeoutError(f"{tool_name} exceeded {seconds}s timeout")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


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
            "name": "web_search",
            "description": "Search the public web for context, leads, sources, and search terms that are not already known to the archive app.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
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
            "name": "add_archive",
            "description": "Register a searchable archive plugin. Use CSS selectors when known; otherwise the generic link extractor will be used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "base_url": {"type": "string"},
                    "search_url_template": {
                        "type": "string",
                        "description": "Search URL containing {query}, for example https://example.org/search?q={query}.",
                    },
                    "result_selector": {"type": "string"},
                    "link_selector": {"type": "string"},
                    "title_selector": {"type": "string"},
                    "trust_level": {"type": "string", "enum": ["trusted", "untrusted"]},
                },
                "required": ["name", "base_url"],
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
    {
        "type": "function",
        "function": {
            "name": "set_goal_timer",
            "description": "Set or update the active goal's estimated completion timer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "string", "description": "Relative duration such as 30m, 6h, or 2d."},
                    "reason": {"type": "string"},
                },
                "required": ["duration"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_goal",
            "description": "Mark the active goal complete or blocked with a concrete reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["complete", "blocked"]},
                    "reason": {"type": "string"},
                },
                "required": ["status", "reason"],
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
            "web_search": self.web_search,
            "search": self.search,
            "ingest_url": self.ingest_url,
            "add_archive": self.add_archive,
            "research": self.research,
            "download": self.download,
            "process": self.process,
            "archive_raw": self.archive_raw,
            "run_backlog_until_done": self.run_backlog_until_done,
            "build_corpus": self.build_corpus,
            "set_goal_timer": self.set_goal_timer,
            "finish_goal": self.finish_goal,
        }
        if name not in handlers:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            with _tool_timeout(name):
                return handlers[name](**(arguments or {}))
        except ToolTimeoutError as exc:
            timeout_seconds = TOOL_TIMEOUTS.get(name)
            message = f"{name} did not finish within {timeout_seconds}s; the operation was stopped so the agent can continue."
            self._report_tool_problem(name, message)
            return {
                "ok": False,
                "error": message,
                "error_type": "timeout",
                "tool": name,
                "timeout_seconds": timeout_seconds,
                "retryable": True,
            }
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._report_tool_problem(name, message)
            return {"ok": False, "error": message, "error_type": type(exc).__name__, "tool": name}

    def _report_tool_problem(self, name, message):
        print(f"[!] Tool {name} failed: {message}", flush=True)
        if hasattr(self.shell, "_log_agent_status"):
            self.shell._log_agent_status(
                f"Tool {name} failed: {message}",
                loop_kind="tool",
                phase="error",
            )

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

    def web_search(self, query, limit=5):
        if hasattr(self.shell, "_log_agent_status"):
            self.shell._log_agent_status(
                f"Searching the public web for '{query}'.",
                loop_kind="tool",
                phase="start",
            )
        brave = self._brave_search(query, limit=limit)
        if brave.get("ok") and brave.get("results"):
            return brave
        duck = self._duckduckgo_search(query, limit=limit)
        if duck.get("ok") and duck.get("results"):
            return duck
        return {
            "ok": False,
            "query": query,
            "error": "no web search results returned",
            "attempts": [brave, duck],
        }

    def _brave_search(self, query, limit=5):
        url = "https://search.brave.com/search"
        try:
            response = requests.get(
                url,
                params={"q": query, "source": "web"},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=20,
            )
            response.raise_for_status()
        except Exception as exc:
            return {"ok": False, "engine": "brave", "error": str(exc), "error_type": type(exc).__name__, "query": query}

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        seen = set()
        for link in soup.select('a[href^="http"]'):
            href = link.get("href")
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc.endswith("search.brave.com") or parsed.netloc.startswith("cdn."):
                continue
            text = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            if not text:
                continue
            classes = " ".join(link.get("class") or [])
            if "l1" not in classes and "desktop-heading" not in classes and len(results) >= 1:
                continue
            if href in seen:
                continue
            seen.add(href)
            parent_text = re.sub(r"\s+", " ", link.parent.get_text(" ", strip=True)).strip() if link.parent else ""
            results.append({
                "title": text[:240],
                "url": href,
                "snippet": parent_text[:500],
                "engine": "brave",
            })
            if len(results) >= limit:
                break
        return {"ok": True, "engine": "brave", "query": query, "results": results}

    def _duckduckgo_search(self, query, limit=5):
        url = "https://html.duckduckgo.com/html/"
        try:
            response = requests.get(
                url,
                params={"q": query},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=20,
            )
            response.raise_for_status()
        except Exception as exc:
            return {"ok": False, "engine": "duckduckgo", "error": str(exc), "error_type": type(exc).__name__, "query": query}

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for node in soup.select(".result"):
            link = node.select_one(".result__a")
            snippet = node.select_one(".result__snippet")
            if not link:
                continue
            href = link.get("href") or ""
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc.endswith("duckduckgo.com"):
                qs = urllib.parse.parse_qs(parsed.query)
                href = (qs.get("uddg") or [href])[0]
            title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            text = re.sub(r"\s+", " ", snippet.get_text(" ", strip=True)).strip() if snippet else ""
            if title and href:
                results.append({"title": title, "url": href, "snippet": text})
            if len(results) >= limit:
                break
        return {"ok": True, "engine": "duckduckgo", "query": query, "results": results}

    def search(self, query, max_results=None, sources=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.shell.cli.perform_crawl(
                query,
                self.shell.config["model"],
                max_results or self.shell.config["max_results"],
                sources=sources or self.shell.config["sources"],
                should_stop=getattr(self.shell, "_goal_should_stop", None),
            )
        output = output.getvalue()
        return {"ok": True, "output": output[-8000:], "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def ingest_url(self, url):
        args = self._namespace(url=url)
        output = self._capture(self.shell.cli.handle_url, args)
        return {"ok": True, "output": output[-8000:], "backlog": db.get_backlog_counts(processor.EXTRACTOR_VERSION)}

    def add_archive(
        self,
        name,
        base_url,
        search_url_template=None,
        result_selector=None,
        link_selector=None,
        title_selector=None,
        trust_level="untrusted",
    ):
        plugin = archive_plugins.add_plugin(
            name=name,
            base_url=base_url,
            search_url_template=search_url_template,
            result_selector=result_selector,
            link_selector=link_selector,
            title_selector=title_selector,
            trust_level=trust_level,
        )
        if "archive_plugins" not in self.shell.config["sources"]:
            self.shell.config["sources"].append("archive_plugins")
        plugins = archive_plugins.load_plugins()
        return {
            "ok": True,
            "archive": plugin,
            "archive_count": len(plugins),
            "usage": "Search this archive by including source 'archive_plugins' in the search tool.",
        }

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

    def set_goal_timer(self, duration, reason=None):
        goal = getattr(self.shell, "current_goal", None)
        if not goal:
            return {"ok": False, "error": "no active goal"}
        seconds = goals.parse_duration(duration)
        estimated_at = goals.timestamp_after(seconds)
        updated = self.shell.goal_store.update(goal["id"], estimated_completion_at=estimated_at)
        self.shell.current_goal = updated
        self.shell.goal_store.append_event(
            goal["id"],
            "timer",
            f"estimated completion set to {estimated_at}",
            {"duration": duration, "reason": reason},
        )
        return {"ok": True, "estimated_completion_at": estimated_at, "duration_seconds": seconds}

    def finish_goal(self, status, reason):
        goal = getattr(self.shell, "current_goal", None)
        if not goal:
            return {"ok": False, "error": "no active goal"}
        updated = self.shell.goal_store.update(goal["id"], status=status)
        self.shell.current_goal = updated
        self.shell.goal_store.append_event(goal["id"], status, reason)
        return {"ok": True, "status": status, "reason": reason}


def dumps_tool_result(result):
    return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
