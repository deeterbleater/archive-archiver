import argparse
import cmd
import json
import shlex
import sys
import termios
import tty
from types import SimpleNamespace

import corpus
import agent_tools
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
    "/model": "model",
    "/set": "set",
    "/search": "search",
    "/url": "url",
    "/research": "research",
    "/download": "download",
    "/process": "process",
    "/archive-raw": "archive_raw",
    "/cycle": "cycle",
    "/corpus": "corpus",
    "/memory": "memory",
    "/remember": "remember",
    "/compact": "compact",
    "/context": "context",
    "/exit": "exit",
    "/quit": "quit",
}

MEMORY_COMMANDS = {"memory", "remember", "compact", "context", "help", "slash", "config", "model"}
CHAT_KINDS = {"summary", "note", "user", "assistant"}
MODEL_PAGE_SIZE = 20


SYSTEM_PROMPT = """
You are ALGE, a terminal-native archive operations assistant inside the archive-archiver project.
You can talk normally and you also have tools that operate the archive app: status, backlog, search, ingest_url, research, download, process, archive_raw, run_backlog_until_done, and build_corpus.
Use tools when the user asks you to perform app work. For example, "download all backlogged works and process them" should call run_backlog_until_done.
For long tasks, keep working through tool calls until the requested task is complete, stalled, or blocked by a clear error. Report concrete counts and stopping reason.
Slash commands such as /status, /search, /download, /process, /archive-raw, /cycle, /corpus, /memory, /context, and /model are still direct operator controls.
""".strip()
MAX_TOOL_ITERATIONS = 12


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
        self.tools = agent_tools.AppToolRunner(self)

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

    def _active_model(self):
        return self.config["model"] or llm.DEFAULT_MODEL

    def _chat_messages(self, user_text):
        context_length = self.memory.model_context_length(model=self._active_model())
        budget = max(2000, int(context_length * 0.65))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        used = memory.estimate_tokens(SYSTEM_PROMPT) + memory.estimate_tokens(user_text)

        rows = [
            row for row in self.memory.entries()
            if row.get("kind") in CHAT_KINDS
        ]
        selected = []
        for row in reversed(rows):
            content = row.get("content", "")
            tokens = memory.estimate_tokens(content)
            if used + tokens > budget:
                break
            selected.append(row)
            used += tokens

        for row in reversed(selected):
            kind = row.get("kind")
            content = row.get("content", "")
            if kind == "assistant":
                messages.append({"role": "assistant", "content": content})
            elif kind == "user":
                messages.append({"role": "user", "content": content})
            elif kind == "summary":
                messages.append({"role": "system", "content": f"Prior context summary:\n{content}"})
            elif kind == "note":
                messages.append({"role": "system", "content": f"Operator note:\n{content}"})

        messages.append({"role": "user", "content": user_text})
        return messages

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
        stripped = line.strip()
        if stripped and not stripped.startswith("/"):
            self._chat(stripped)
            return None

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
        self._chat(line.strip())

    def _chat(self, line):
        if not line:
            return
        messages = self._chat_messages(line)
        self.memory.append("user", line, {"model": self._active_model()})
        try:
            response = self._run_llm_tool_loop(messages)
        except Exception as exc:
            print(f"[!] OpenRouter chat error: {exc}")
            return

        if response:
            print(response)
            self.memory.append("assistant", response, {"model": self._active_model()})
        self._auto_compact()

    def _run_llm_tool_loop(self, messages):
        final_text = None
        for _iteration in range(MAX_TOOL_ITERATIONS):
            completion = llm.chat_completion(
                messages,
                model=self._active_model(),
                tools=agent_tools.TOOL_SCHEMAS,
            )
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            content = (message.content or "").strip() if message.content else ""

            if not tool_calls:
                return content

            assistant_message = {"role": "assistant", "content": message.content or "", "tool_calls": []}
            tool_messages = []
            if content:
                print(content)

            for call in tool_calls:
                function = call.function
                try:
                    arguments = json.loads(function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    arguments = {}
                    result = {"ok": False, "error": f"invalid tool arguments: {exc}"}
                else:
                    print(f"[agent] {function.name}({arguments})")
                    result = self.tools.execute(function.name, arguments)

                self.memory.append("tool", function.name, {"arguments": arguments, "result": result})
                assistant_message["tool_calls"].append({
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": function.name,
                        "arguments": function.arguments or "{}",
                    },
                })
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": agent_tools.dumps_tool_result(result),
                })
                final_text = agent_tools.dumps_tool_result(result)

            messages.append(assistant_message)
            messages.extend(tool_messages)

        return (
            "I stopped after the tool-iteration limit. Last tool result:\n"
            f"{final_text or 'no tool result'}"
        )

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

    def _model_rows(self, refresh=False):
        payload = self.memory.fetch_model_specs(force=refresh)
        rows = []
        for item in payload.get("data", []):
            model_id = item.get("id") or item.get("canonical_slug")
            if not model_id:
                continue
            provider = item.get("top_provider") or {}
            context_length = provider.get("context_length") or item.get("context_length") or "?"
            rows.append({
                "id": model_id,
                "name": item.get("name") or model_id,
                "context_length": context_length,
            })
        rows.sort(key=lambda row: row["id"])
        return rows

    def _print_model_page(self, rows, page):
        page_count = max(1, (len(rows) + MODEL_PAGE_SIZE - 1) // MODEL_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        start = page * MODEL_PAGE_SIZE
        visible = rows[start:start + MODEL_PAGE_SIZE]
        print("\n================ OPENROUTER MODELS ==============")
        print(f"active: {self._active_model()}")
        print(f"page: {page + 1}/{page_count}  models: {len(rows)}")
        for offset, row in enumerate(visible, start=1):
            marker = "*" if row["id"] == self._active_model() else " "
            print(f"{marker} {offset:>2}. {row['id']}  ctx={row['context_length']}")
        print("Use left/right or up/down arrows to page, number + Enter to select, q to quit.")
        print("=================================================")
        return page

    def _read_model_selection(self, rows):
        stream = self.stdin or sys.stdin
        if not hasattr(stream, "isatty") or not stream.isatty():
            self._print_model_page(rows, 0)
            print("[!] Interactive model selection requires a TTY. Use /model MODEL_ID instead.")
            return None

        page = 0
        buffer = ""
        fd = stream.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                page = self._print_model_page(rows, page)
                if buffer:
                    print(f"selection: {buffer}")
                key = stream.read(1)
                if key == "\x03":
                    print()
                    return None
                if key in ("\r", "\n"):
                    if not buffer:
                        continue
                    selected = int(buffer)
                    index = page * MODEL_PAGE_SIZE + selected - 1
                    if selected < 1 or selected > MODEL_PAGE_SIZE or index >= len(rows):
                        print(f"\n[!] Selection out of range: {buffer}")
                        buffer = ""
                        continue
                    print()
                    return rows[index]["id"]
                if key in ("q", "Q", "\x1b"):
                    if key == "\x1b":
                        rest = stream.read(2)
                        if rest in ("[C", "[B"):
                            page += 1
                            buffer = ""
                            continue
                        if rest in ("[D", "[A"):
                            page -= 1
                            buffer = ""
                            continue
                    print()
                    return None
                if key in ("n", "N"):
                    page += 1
                    buffer = ""
                    continue
                if key in ("p", "P"):
                    page -= 1
                    buffer = ""
                    continue
                if key in ("\x7f", "\b"):
                    buffer = buffer[:-1]
                    continue
                if key.isdigit():
                    buffer = (buffer + key)[:2]
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    def do_model(self, line):
        """Choose the active OpenRouter model: /model [--refresh] [MODEL_ID]."""
        parser = _parser("model")
        parser.add_argument("--refresh", action="store_true")
        parser.add_argument("model", nargs="?")
        args = self._run_parser(parser, line)
        if not args:
            return

        if args.model:
            self.config["model"] = args.model
            print(f"[+] model updated: {self.config['model']}")
            return

        try:
            rows = self._model_rows(refresh=args.refresh)
        except Exception as exc:
            print(f"[!] Could not load OpenRouter models: {exc}")
            return
        if not rows:
            print("[!] OpenRouter returned no models.")
            return

        selected = self._read_model_selection(rows)
        if selected:
            self.config["model"] = selected
            context_length = self.memory.model_context_length(model=selected)
            print(f"[+] model updated: {selected} (context {context_length})")

    def do_set(self, line):
        """Set a session default: /set model MODEL | /set max-results N | /set sources archive_org anarchist_library arxiv substack."""
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
  /model
  /set KEY VALUE
  /search QUERY
  /url URL
  /research TOPIC
  /download [--limit N] [--domain-workers]
  /process [--limit N]
  /archive-raw [--limit N]
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

    def do_archive_raw(self, line):
        """Upload processed raw originals to object storage: /archive-raw [--limit N]."""
        parser = _parser("archive-raw")
        parser.add_argument("--limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--keep-local", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_archive_raw(self._namespace(**vars(args)))

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
  /model [MODEL_ID]
      Fetch OpenRouter models and choose the active chat/model-extraction model.
  /set model MODEL
  /set max-results N
  /set sources archive_org anarchist_library arxiv substack
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
  /archive-raw [--limit N] [--keep-local]
      Upload processed raw originals to object storage and remove local copies.
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

Normal text without a slash is sent to the active model as chat.
""".strip())


def run_agent(cli_module, command=None):
    shell = ArchiveAgentShell(cli_module)
    if command:
        shell.onecmd(command)
        return
    print(INTRO.strip())
    shell.cmdloop()
