import argparse
import cmd
import shlex
from types import SimpleNamespace

import corpus
import downloader
import llm
import memory
import processor


INTRO = """
ALGE archive harness
Type /help for commands, /config for current defaults, or /exit to leave.
"""

SLASH_COMMANDS = {
    "/help": "help",
    "/slash": "slash",
    "/status": "status",
    "/config": "config",
    "/set": "set",
    "/search": "search",
    "/url": "url",
    "/research": "research",
    "/download": "download",
    "/process": "process",
    "/cycle": "cycle",
    "/corpus": "corpus",
    "/memory": "memory",
    "/remember": "remember",
    "/compact": "compact",
    "/context": "context",
    "/exit": "exit",
    "/quit": "quit",
}

MEMORY_COMMANDS = {"memory", "remember", "compact", "context", "help", "slash", "config"}


def _parser(prog, description=None):
    return argparse.ArgumentParser(
        prog=prog,
        description=description,
        add_help=True,
        exit_on_error=False,
    )


def _split(line):
    try:
        return shlex.split(line)
    except ValueError as exc:
        print(f"[!] Could not parse command: {exc}")
        return None


class AgentCommandError(ValueError):
    pass


class ArchiveAgentShell(cmd.Cmd):
    prompt = "alge> "

    def __init__(self, cli_module, stdin=None, stdout=None):
        super().__init__(stdin=stdin, stdout=stdout)
        self.cli = cli_module
        self.config = {
            "model": None,
            "max_results": 2,
            "sources": list(cli_module.DEFAULT_PUBLIC_SOURCES),
            "download_limit": 10,
            "process_limit": 10,
            "rps": 0.2,
            "max_mb": 250,
            "max_domains": None,
            "per_domain_limit": None,
            "memory_path": memory.DEFAULT_MEMORY_PATH,
            "compaction_ratio": memory.DEFAULT_COMPACTION_RATIO,
        }
        self.memory = memory.MemoryStore(
            path=self.config["memory_path"],
            compaction_ratio=self.config["compaction_ratio"],
        )

    def _run_parser(self, parser, line):
        argv = _split(line)
        if argv is None:
            return None
        try:
            return parser.parse_args(argv)
        except argparse.ArgumentError as exc:
            print(f"[!] {exc}")
            return None
        except SystemExit:
            return None

    def _namespace(self, **overrides):
        values = {
            "model": self.config["model"],
            "max_results": self.config["max_results"],
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _normalize_command(self, line):
        stripped = line.strip()
        if not stripped.startswith("/"):
            return line

        command, separator, rest = stripped.partition(" ")
        mapped = SLASH_COMMANDS.get(command)
        if not mapped:
            return line
        if separator:
            return f"{mapped} {rest}"
        return mapped

    def onecmd(self, line):
        normalized = self._normalize_command(line)
        command_name = normalized.strip().split(" ", 1)[0] if normalized.strip() else ""
        result = super().onecmd(normalized)
        if command_name and command_name not in MEMORY_COMMANDS and command_name != "exit":
            self.memory.append("command", normalized, {"raw": line.strip()})
            self._auto_compact()
        return result

    def _auto_compact(self):
        result = self.memory.compact(model=self.config["model"], force=False)
        if result.get("compacted"):
            print(
                "[memory] compacted "
                f"{result['source_entries']} entries to {result['kept_entries']} "
                f"({result['tokens']} estimated tokens)."
            )

    def emptyline(self):
        return None

    def default(self, line):
        if line.strip().startswith("/"):
            print(f"[!] Unknown slash command: {line.strip()}")
            print("    Type /help to see available slash commands.")
            return
        print(f"[!] Unknown command: {line}")
        print("    Type /help to see available commands.")

    def do_exit(self, _line):
        """Leave the agent harness."""
        print("bye")
        return True

    def do_quit(self, line):
        """Leave the agent harness."""
        return self.do_exit(line)

    def do_EOF(self, line):
        """Leave the agent harness with Ctrl-D."""
        print()
        return self.do_exit(line)

    def do_config(self, _line):
        """Show current session defaults."""
        print("\n================ AGENT CONFIG ===================")
        for key, value in self.config.items():
            if isinstance(value, list):
                value = ", ".join(value)
            print(f"{key}: {value}")
        print("=================================================")

    def do_set(self, line):
        """Set a session default: /set model MODEL | /set max-results N | /set sources archive_org anarchist_library."""
        argv = _split(line)
        if argv is None:
            return
        if len(argv) < 2:
            print("[!] Usage: set <model|max-results|sources|rps|max-mb|max-domains|per-domain-limit> <value>")
            return

        key = argv[0].replace("_", "-")
        values = argv[1:]
        try:
            if key == "model":
                self.config["model"] = " ".join(values)
            elif key == "max-results":
                self.config["max_results"] = int(values[0])
            elif key == "sources":
                invalid = [source for source in values if source not in self.cli.ALL_SOURCES]
                if invalid:
                    raise AgentCommandError(f"unknown sources: {', '.join(invalid)}")
                self.config["sources"] = values
            elif key == "rps":
                self.config["rps"] = float(values[0])
            elif key == "max-mb":
                self.config["max_mb"] = int(values[0])
            elif key == "max-domains":
                self.config["max_domains"] = int(values[0])
            elif key == "per-domain-limit":
                self.config["per_domain_limit"] = int(values[0])
            elif key == "memory-path":
                self.config["memory_path"] = values[0]
                self.memory = memory.MemoryStore(
                    path=self.config["memory_path"],
                    compaction_ratio=self.config["compaction_ratio"],
                )
            elif key == "compaction-ratio":
                ratio = float(values[0])
                if ratio <= 0 or ratio >= 1:
                    raise AgentCommandError("compaction-ratio must be between 0 and 1")
                self.config["compaction_ratio"] = ratio
                self.memory.compaction_ratio = ratio
            else:
                raise AgentCommandError(f"unknown config key: {key}")
        except (IndexError, ValueError, AgentCommandError) as exc:
            print(f"[!] Could not set config: {exc}")
            return

        print(f"[+] {key} updated.")

    def do_slash(self, _line):
        """Show slash command aliases."""
        print("""
Slash commands:
  /help
  /status
  /config
  /set KEY VALUE
  /search QUERY
  /url URL
  /research TOPIC
  /download [--limit N] [--domain-workers]
  /process [--limit N]
  /cycle [--query QUERY]
  /corpus NAME [--query TEXT]
  /memory [--limit N] [--search TEXT]
  /remember TEXT
  /compact [--force]
  /context [--refresh]
  /exit
""".strip())

    def do_memory(self, line):
        """Show saved context logs: /memory [--limit N] [--search TEXT] [--clear]."""
        parser = _parser("memory")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--search")
        parser.add_argument("--clear", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        if args.clear:
            self.memory.clear()
            print("[+] Memory log cleared.")
            return

        rows = self.memory.recent(limit=args.limit, query=args.search)
        if not rows:
            print("[memory] no saved context entries")
            return

        print("\n================ SAVED CONTEXT ==================")
        for row in rows:
            print(f"{row.get('ts')} [{row.get('kind')}] {row.get('content')}")
            metadata = row.get("metadata") or {}
            if metadata:
                print(f"  metadata: {metadata}")
        print("=================================================")

    def do_remember(self, line):
        """Save a note into context memory: /remember TEXT."""
        note = line.strip()
        if not note:
            print("[!] Usage: /remember TEXT")
            return
        self.memory.append("note", note)
        self._auto_compact()
        print("[+] Remembered.")

    def do_context(self, line):
        """Show model context and memory budget: /context [--refresh]."""
        parser = _parser("context")
        parser.add_argument("--refresh", action="store_true")
        parser.add_argument("--model", default=self.config["model"])
        args = self._run_parser(parser, line)
        if not args:
            return
        context_length = self.memory.model_context_length(
            model=args.model,
            force_refresh=args.refresh,
        )
        threshold = int(context_length * self.memory.compaction_ratio)
        tokens = self.memory.token_count()
        print("\n================ CONTEXT WINDOW =================")
        print(f"model: {args.model or self.config['model'] or llm.DEFAULT_MODEL}")
        print(f"context_length: {context_length}")
        print(f"memory_tokens_estimate: {tokens}")
        print(f"compaction_threshold: {threshold}")
        print(f"memory_path: {self.memory.path}")
        print("=================================================")

    def do_compact(self, line):
        """Compact saved context memory: /compact [--force]."""
        parser = _parser("compact")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--model", default=self.config["model"])
        args = self._run_parser(parser, line)
        if not args:
            return
        result = self.memory.compact(model=args.model, force=args.force)
        if result.get("compacted"):
            print(
                "[+] Memory compacted: "
                f"{result['source_entries']} entries -> {result['kept_entries']} entries, "
                f"{result['tokens']} estimated tokens."
            )
        else:
            print(f"[-] Memory not compacted: {result.get('reason')}")

    def do_status(self, line):
        """Show database status."""
        if line.strip():
            print("[!] status does not accept arguments.")
            return
        self.cli.handle_status(self._namespace())

    def do_search(self, line):
        """Discover works: search [--max-results N] [--sources SRC ...] QUERY."""
        parser = _parser("search")
        parser.add_argument("--max-results", type=int, default=self.config["max_results"])
        parser.add_argument("--sources", nargs="+", choices=self.cli.ALL_SOURCES, default=self.config["sources"])
        parser.add_argument("query", nargs="+")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_search(self._namespace(
            query=" ".join(args.query),
            max_results=args.max_results,
            sources=args.sources,
        ))

    def do_url(self, line):
        """Ingest a direct archive detail URL: url URL."""
        parser = _parser("url")
        parser.add_argument("url")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_url(self._namespace(url=args.url))

    def do_research(self, line):
        """Generate search terms, crawl them, and synthesize a report: research TOPIC."""
        parser = _parser("research")
        parser.add_argument("--max-results", type=int, default=self.config["max_results"])
        parser.add_argument("--sources", nargs="+", choices=self.cli.ALL_SOURCES, default=self.config["sources"])
        parser.add_argument("topic", nargs="+")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_research(self._namespace(
            topic=" ".join(args.topic),
            max_results=args.max_results,
            sources=args.sources,
        ))

    def do_download(self, line):
        """Download pending files: download [--limit N] [--domain-workers] [--rps N]."""
        parser = _parser("download")
        parser.add_argument("--limit", type=int, default=self.config["download_limit"])
        parser.add_argument("--bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR)
        parser.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR)
        parser.add_argument("--rps", type=float, default=self.config["rps"])
        parser.add_argument("--max-mb", type=int, default=self.config["max_mb"])
        parser.add_argument("--domain-workers", action="store_true")
        parser.add_argument("--max-domains", type=int, default=self.config["max_domains"])
        parser.add_argument("--per-domain-limit", type=int, default=self.config["per_domain_limit"])
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_download(self._namespace(**vars(args)))

    def do_process(self, line):
        """Extract plaintext from downloaded files: process [--limit N]."""
        parser = _parser("process")
        parser.add_argument("--limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR)
        parser.add_argument("--extractor", default=processor.EXTRACTOR_VERSION)
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_process(self._namespace(**vars(args)))

    def do_cycle(self, line):
        """Run one discover-download-process cycle: cycle [--query QUERY] [--download-limit N]."""
        parser = _parser("cycle")
        parser.add_argument("--query", action="append")
        parser.add_argument("--queries-file")
        parser.add_argument("--sources", nargs="+", choices=self.cli.ALL_SOURCES, default=self.config["sources"])
        parser.add_argument("--max-results", type=int, default=self.config["max_results"])
        parser.add_argument("--download-limit", type=int, default=self.config["download_limit"])
        parser.add_argument("--process-limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--raw-bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR)
        parser.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR)
        parser.add_argument("--text-bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR)
        parser.add_argument("--rps", type=float, default=self.config["rps"])
        parser.add_argument("--max-mb", type=int, default=self.config["max_mb"])
        parser.add_argument("--max-domains", type=int, default=self.config["max_domains"])
        parser.add_argument("--per-domain-limit", type=int, default=self.config["per_domain_limit"])
        parser.add_argument("--extractor", default=processor.EXTRACTOR_VERSION)
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_collect(self._namespace(
            **vars(args),
            once=True,
            sleep_seconds=0,
        ))

    def do_corpus(self, line):
        """Build a corpus: corpus NAME [--query TEXT] [--ordering title|hash|created|random]."""
        parser = _parser("corpus")
        parser.add_argument("name")
        parser.add_argument("--category")
        parser.add_argument("--site")
        parser.add_argument("--query")
        parser.add_argument("--ordering", choices=["title", "hash", "created", "random"], default="title")
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--limit", type=int)
        parser.add_argument("--substitutions-file")
        parser.add_argument("--bucket-dir", default=corpus.DEFAULT_CORPUS_BUCKET_DIR)
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_corpus(self._namespace(**vars(args)))

    def do_help(self, arg):
        if arg:
            return super().do_help(arg)
        print("""
Commands:
  /status
      Show database counts and pipeline state.
  /config
      Show session defaults used by agent commands.
  /set model MODEL
  /set max-results N
  /set sources archive_org anarchist_library
  /set rps N
      Update session defaults.
  /set memory-path PATH
  /set compaction-ratio 0.55
      Update memory and compaction defaults.
  /search [--max-results N] [--sources SRC ...] QUERY
      Discover archive records for a query.
  /url URL
      Ingest one archive detail page.
  /research [--max-results N] TOPIC
      Generate focused search terms, crawl them, and write a report.
  /download [--limit N] [--domain-workers] [--rps N]
      Download pending files into the raw bucket.
  /process [--limit N]
      Extract plaintext from downloaded files.
  /cycle [--query QUERY] [--download-limit N] [--process-limit N]
      Run one discover-download-process cycle.
  /corpus NAME [--query TEXT] [--ordering title|hash|created|random]
      Build a deterministic corpus manifest and text bundle.
  /memory [--limit N] [--search TEXT]
      Read saved command and note context logs.
  /remember TEXT
      Save an operator note into context memory.
  /context [--refresh]
      Show model context window, memory estimate, and compaction threshold.
  /compact [--force]
      Compact saved context logs using OpenRouter when available.
  /exit
      Leave the harness.

Plain command names also work without the slash.
""".strip())


def run_agent(cli_module, command=None):
    shell = ArchiveAgentShell(cli_module)
    if command:
        shell.onecmd(command)
        return
    print(INTRO.strip())
    shell.cmdloop()
