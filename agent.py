import argparse
import cmd
import json
import os
import select
import shlex
import subprocess
import sys
import termios
import threading
import time
import tty
from types import SimpleNamespace

import corpus
import agent_tools
import downloader
import goals
import llm
import memory
import processor
import db
import terminal_theme
import text_munger
import text_validator


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
    "/validate-texts": "validate_texts",
    "/munge-texts": "munge_texts",
    "/archive-raw": "archive_raw",
    "/rss-ingest": "rss_ingest",
    "/cycle": "cycle",
    "/auto": "auto",
    "/corpus": "corpus",
    "/memory": "memory",
    "/remember": "remember",
    "/compact": "compact",
    "/context": "context",
    "/goal": "goal",
    "/exit": "exit",
    "/quit": "quit",
}

MEMORY_COMMANDS = {"memory", "remember", "compact", "context", "help", "slash", "config", "model", "goal"}
CHAT_KINDS = {"summary", "note", "user", "assistant"}
MODEL_PAGE_SIZE = 20


SYSTEM_PROMPT = """
You are ALGE, a terminal-native archive operations assistant inside the archive-archiver project.
You can talk normally and you also have tools that operate the archive app: status, backlog, web_search, search, ingest_url, add_archive, research, download, process, archive_raw, rss_ingest, run_backlog_until_done, build_corpus, set_goal_timer, and finish_goal.
Use tools when the user asks you to perform app work. For example, "download all backlogged works and process them" should call run_backlog_until_done. When the user asks you to find or collect a topic, author, or kind of work, call run_backlog_until_done with query set to that request so the download/process loop stays focused on newly discovered matches instead of the global backlog.
The archive search tool is asynchronous: it starts one background batch per archive source and returns immediately. Use status or backlog to inspect background_batches before deciding that a search has finished.
When the user gives a broad archival goal, use web_search to discover terminology, related people, source domains, and query expansions, then use archive search tools and the full download/process/archive workflow.
When the user asks to add a new archive, call add_archive with its base URL and any known CSS selectors, then search with source archive_plugins. If the generic plugin cannot discover usable result links, explain that this archive needs a custom scraper plugin.
You may use Rich terminal markup tags in normal assistant replies: [highlight]important[/highlight], [success]done[/success], [warning]watch this[/warning], [danger]problem[/danger], [muted]quiet detail[/muted], [tool]tool name[/tool]. The default highlight color is pond-scum green via [highlight]...[/highlight]. Use tags sparingly and never wrap JSON tool arguments in markup.
For long tasks, keep working through tool calls until the requested task is complete, stalled, or blocked by a clear error. Report concrete counts and stopping reason.
For explicit /goal work, set an estimated completion timer and call finish_goal only when the objective is complete or clearly blocked.
Slash commands such as /status, /search, /download, /process, /validate-texts, /munge-texts, /archive-raw, /rss-ingest, /cycle, /auto, /corpus, /memory, /context, /goal, and /model are still direct operator controls.
""".strip()
MAX_TOOL_ITERATIONS = 12
MAX_GOAL_TOOL_ITERATIONS = 20
IDLE_WARNING_SECONDS = int(os.getenv("ALGE_AGENT_IDLE_WARNING_SECONDS", "120"))
IDLE_WATCHDOG_INTERVAL_SECONDS = float(os.getenv("ALGE_AGENT_IDLE_WATCHDOG_INTERVAL_SECONDS", "5"))


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


class AgentLoopError(RuntimeError):
    pass


class ArchiveAgentShell(cmd.Cmd):
    prompt = terminal_theme.prompt()

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
        self.goal_store = goals.GoalStore()
        self.current_goal = None
        self.session_id = f"alge-{os.getpid()}-{int(time.time())}"
        self._agent_last_activity = time.monotonic()
        self._agent_current_operation = "idle"
        self._goal_stop_requested = False
        self._auto_thread = None
        self._auto_stop_event = threading.Event()
        self._auto_focus = None
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

    def _active_goal_id(self):
        if self.current_goal:
            return self.current_goal.get("id")
        return None

    def _system_prompt(self):
        if not self._auto_focus:
            return SYSTEM_PROMPT
        return (
            f"{SYSTEM_PROMPT}\n\n"
            "Current autonomous collection focus: "
            f"{self._auto_focus}. Bias discovery toward this fiction/non-fiction "
            "topic while still maintaining archival quality and dedupe discipline."
        )

    def _log_agent_status(self, message, loop_kind="chat", phase="update", goal_id=None):
        try:
            return db.add_agent_status(
                message,
                session_id=self.session_id,
                loop_kind=loop_kind,
                phase=phase,
                model=self._active_model(),
                goal_id=goal_id or self._active_goal_id(),
            )
        except Exception as exc:
            terminal_theme.print_markup(f"[muted]status log failed: {exc}[/muted]")
            return None

    def _touch_agent_activity(self, operation):
        self._agent_last_activity = time.monotonic()
        self._agent_current_operation = operation

    def _chat_messages(self, user_text, include_memory=True):
        context_length = self.memory.model_context_length(model=self._active_model())
        budget = max(2000, int(context_length * 0.65))
        system_prompt = self._system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        used = memory.estimate_tokens(system_prompt) + memory.estimate_tokens(user_text)

        if include_memory:
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
            terminal_theme.print_markup(
                "[muted]memory[/muted] compacted "
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
            response = self._run_llm_tool_loop(messages, loop_kind="chat")
        except Exception as exc:
            self._log_agent_status(
                f"Chat loop failed before completion: {exc}",
                loop_kind="chat",
                phase="error",
            )
            terminal_theme.print_markup(f"[danger][!] OpenRouter chat error:[/danger] {exc}")
            return

        if response:
            terminal_theme.print_markup(response)
            self.memory.append("assistant", response, {"model": self._active_model()})
        self._auto_compact()

    def _run_llm_tool_loop(
        self,
        messages,
        max_iterations=MAX_TOOL_ITERATIONS,
        stop_checker=None,
        loop_kind="chat",
        goal_id=None,
    ):
        final_text = None
        for _iteration in range(max_iterations):
            iteration = _iteration + 1
            self._touch_agent_activity(f"{loop_kind} loop {iteration}")
            self._log_agent_status(
                f"Starting {loop_kind} loop {iteration} with {self._active_model()}.",
                loop_kind=loop_kind,
                phase="start",
                goal_id=goal_id,
            )
            if stop_checker and stop_checker():
                self._log_agent_status(
                    f"{loop_kind.title()} loop {iteration} halted by operator before the model call.",
                    loop_kind=loop_kind,
                    phase="halted",
                    goal_id=goal_id,
                )
                return "[goal] halted by operator."
            self._touch_agent_activity(f"{loop_kind} loop {iteration} waiting for model")
            try:
                completion = llm.chat_completion(
                    messages,
                    model=self._active_model(),
                    tools=agent_tools.TOOL_SCHEMAS,
                )
            except Exception as exc:
                message = f"{loop_kind.title()} loop {iteration} model call failed: {type(exc).__name__}: {exc}"
                self._touch_agent_activity(f"{loop_kind} loop {iteration} model call failed")
                self._log_agent_status(
                    message,
                    loop_kind=loop_kind,
                    phase="error",
                    goal_id=goal_id,
                )
                terminal_theme.print_markup(f"[danger][!][/danger] {message}")
                if loop_kind == "goal":
                    raise AgentLoopError(message) from exc
                return message
            self._touch_agent_activity(f"{loop_kind} loop {iteration} received model response")
            if stop_checker and stop_checker():
                self._log_agent_status(
                    f"{loop_kind.title()} loop {iteration} halted by operator after the model call.",
                    loop_kind=loop_kind,
                    phase="halted",
                    goal_id=goal_id,
                )
                return "[goal] halted by operator."
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            content = (message.content or "").strip() if message.content else ""

            if not tool_calls:
                self._log_agent_status(
                    f"Finished {loop_kind} loop {iteration} with a direct assistant response.",
                    loop_kind=loop_kind,
                    phase="end",
                    goal_id=goal_id,
                )
                return content

            assistant_message = {"role": "assistant", "content": message.content or "", "tool_calls": []}
            tool_messages = []
            if content:
                terminal_theme.print_markup(content)

            tool_names = []
            for call in tool_calls:
                if stop_checker and stop_checker():
                    self._log_agent_status(
                        f"{loop_kind.title()} loop {iteration} halted while preparing a tool call.",
                        loop_kind=loop_kind,
                        phase="halted",
                        goal_id=goal_id,
                    )
                    return "[goal] halted by operator."
                function = call.function
                tool_names.append(function.name)
                try:
                    arguments = json.loads(function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    arguments = {}
                    result = {"ok": False, "error": f"invalid tool arguments: {exc}"}
                else:
                    terminal_theme.print_tool_call(function.name, arguments)
                    self._touch_agent_activity(f"tool {function.name} running")
                    result = self.tools.execute(function.name, arguments)
                    self._touch_agent_activity(f"tool {function.name} returned")

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
                if stop_checker and stop_checker():
                    self._log_agent_status(
                        f"{loop_kind.title()} loop {iteration} halted after running {function.name}.",
                        loop_kind=loop_kind,
                        phase="halted",
                        goal_id=goal_id,
                    )
                    return "[goal] halted by operator."

            messages.append(assistant_message)
            messages.extend(tool_messages)
            tool_summary = ", ".join(tool_names[:4])
            if len(tool_names) > 4:
                tool_summary += f", +{len(tool_names) - 4} more"
            self._log_agent_status(
                f"Finished {loop_kind} loop {iteration} after {len(tool_names)} tool call(s): {tool_summary}.",
                loop_kind=loop_kind,
                phase="end",
                goal_id=goal_id,
            )

        self._log_agent_status(
            f"Stopped {loop_kind} loop after reaching the {max_iterations} iteration limit.",
            loop_kind=loop_kind,
            phase="limit",
            goal_id=goal_id,
        )
        return (
            "I stopped after the tool-iteration limit. Last tool result:\n"
            f"{final_text or 'no tool result'}"
        )

    def do_exit(self, _line):
        """Leave the agent harness."""
        if self._auto_thread and self._auto_thread.is_alive():
            self._auto_stop_event.set()
        terminal_theme.print_markup("[muted]bye[/muted]")
        if os.getenv("ALGE_TMUX_MANAGED") == "1" and os.getenv("TMUX"):
            session = os.getenv("ALGE_TMUX_SESSION", "alge")
            subprocess.Popen(
                ["tmux", "kill-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
        table = terminal_theme.make_table("Setting", "Value", title="Agent Config")
        for key, value in self.config.items():
            if isinstance(value, list):
                value = ", ".join(value)
            table.add_row(str(key), str(value))
        terminal_theme.console.print(table)

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
        terminal_theme.print_rule("OpenRouter Models")
        terminal_theme.print_markup(f"[label]active:[/label] [highlight]{self._active_model()}[/highlight]")
        terminal_theme.print_markup(f"[label]page:[/label] {page + 1}/{page_count}  [label]models:[/label] {len(rows)}")
        for offset, row in enumerate(visible, start=1):
            marker = "*" if row["id"] == self._active_model() else " "
            style = "highlight" if marker == "*" else "muted"
            terminal_theme.print_markup(f"[{style}]{marker} {offset:>2}. {row['id']}[/]  ctx={row['context_length']}")
        terminal_theme.print_markup("[muted]Use left/right or up/down arrows to page, number + Enter to select, q to quit.[/muted]")
        terminal_theme.print_rule(style="muted")
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
            terminal_theme.print_markup(f"[success][+][/success] model updated: [highlight]{self.config['model']}[/highlight]")
            return

        try:
            rows = self._model_rows(refresh=args.refresh)
        except Exception as exc:
            terminal_theme.print_markup(f"[danger][!][/danger] Could not load OpenRouter models: {exc}")
            return
        if not rows:
            terminal_theme.print_markup("[warning][!][/warning] OpenRouter returned no models.")
            return

        selected = self._read_model_selection(rows)
        if selected:
            self.config["model"] = selected
            context_length = self.memory.model_context_length(model=selected)
            terminal_theme.print_markup(f"[success][+][/success] model updated: [highlight]{selected}[/highlight] [muted](context {context_length})[/muted]")

    def do_set(self, line):
        """Set a session default: /set model MODEL | /set max-results N | /set sources archive_org anarchist_library arxiv substack annas_archive libgen."""
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
  /validate-texts [--limit N] [--workers N]
  /munge-texts [--limit N] [--use-llm]
  /archive-raw [--limit N]
  /rss-ingest [--limit-per-feed N]
  /cycle [--query QUERY]
  /auto [--query-limit N] [--sleep-seconds N]
  /corpus NAME [--query TEXT]
  /memory [--limit N] [--search TEXT]
  /remember TEXT
  /compact [--force]
  /context [--refresh]
  /goal [--run] [--resume ID] [--status] [--stop ID] OBJECTIVE
  /exit
""".strip())

    def _goal_messages(self, goal, cycle):
        events = goal.get("events", [])[-20:]
        event_text = "\n".join(
            f"- {event.get('ts')} [{event.get('kind')}] {event.get('content')} {event.get('metadata', {})}"
            for event in events
        )
        objective = goal["objective"]
        content = f"""
Goal ID: {goal['id']}
Goal objective: {objective}
Goal status: {goal.get('status')}
Cycle: {cycle}
Estimated completion: {goal.get('estimated_completion_at') or 'not set'}

Recent goal events:
{event_text or '- none'}

This goal context is isolated. Work only on this Goal ID and objective; ignore prior goal objectives unless this objective explicitly references them.
Continue this goal. Use web_search for outside knowledge and discovery leads, use archive search tools to add works, then use download/process/archive_raw or run_backlog_until_done to push found files through the archival workflow. Set or update the goal timer when you can estimate remaining work. Keep going unless the goal is complete or blocked.
""".strip()
        return self._chat_messages(content, include_memory=False)

    def _print_goal(self, goal):
        lines = [
            f"[label]id:[/label] [highlight]{goal['id']}[/highlight]",
            f"[label]status:[/label] {goal.get('status')}",
            f"[label]objective:[/label] {goal.get('objective')}",
            f"[label]cycles:[/label] {goal.get('cycles', 0)}",
            f"[label]estimated_completion_at:[/label] {goal.get('estimated_completion_at')}",
            f"[label]updated_at:[/label] {goal.get('updated_at')}",
        ]
        events = goal.get("events", [])[-5:]
        if events:
            lines.append("[label]recent events:[/label]")
            for event in events:
                lines.append(f"  - [muted]{event.get('ts')}[/muted] [tool]{event.get('kind')}[/tool] {event.get('content')}")
        terminal_theme.print_panel("\n".join(lines), title="Goal", border_style="pond")

    def _request_goal_stop(self):
        self._goal_stop_requested = True

    def _goal_should_stop(self):
        return self._goal_stop_requested

    def _start_goal_key_watcher(self):
        stream = self.stdin or sys.stdin
        if not hasattr(stream, "isatty") or not stream.isatty():
            return None

        fd = stream.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except termios.error:
            return None

        stop_watcher = threading.Event()

        def watch_keys():
            while not stop_watcher.is_set() and not self._goal_stop_requested:
                readable, _writable, _errored = select.select([stream], [], [], 0.1)
                if not readable:
                    continue
                key = stream.read(1)
                if key in ("q", "Q"):
                    self._request_goal_stop()
                    terminal_theme.print_markup("\n[warning]goal[/warning] halt requested; finishing current operation...")
                    return

        tty.setcbreak(fd)
        thread = threading.Thread(target=watch_keys, daemon=True)
        thread.start()

        def cleanup():
            stop_watcher.set()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
            thread.join(timeout=0.2)

        return cleanup

    def _start_goal_idle_watchdog(self, goal_id):
        if IDLE_WARNING_SECONDS <= 0:
            return None

        stop_watcher = threading.Event()
        last_warning_bucket = None

        def watch_idle():
            nonlocal last_warning_bucket
            while not stop_watcher.wait(IDLE_WATCHDOG_INTERVAL_SECONDS):
                if self._goal_should_stop():
                    return
                idle_seconds = time.monotonic() - self._agent_last_activity
                if idle_seconds < IDLE_WARNING_SECONDS:
                    continue
                warning_bucket = int(idle_seconds // IDLE_WARNING_SECONDS)
                if warning_bucket == last_warning_bucket:
                    continue
                last_warning_bucket = warning_bucket
                message = (
                    f"Goal has been idle for {int(idle_seconds)}s while {self._agent_current_operation}. "
                    "The watchdog is still alive; press q in the agent pane to request a halt."
                )
                self._log_agent_status(message, loop_kind="goal", phase="idle", goal_id=goal_id)
                terminal_theme.print_markup(f"\n[warning]watchdog[/warning] {message}")

        thread = threading.Thread(target=watch_idle, daemon=True)
        thread.start()

        def cleanup():
            stop_watcher.set()
            thread.join(timeout=0.2)

        return cleanup

    def _run_goal(self, goal, max_cycles=1, sleep_seconds=0):
        self._goal_stop_requested = False
        self.current_goal = self.goal_store.update(goal["id"], status="running")
        self._touch_agent_activity("goal starting")
        cleanup_key_watcher = self._start_goal_key_watcher()
        cleanup_idle_watchdog = self._start_goal_idle_watchdog(goal["id"])
        if cleanup_key_watcher:
            terminal_theme.print_markup("[highlight]goal[/highlight] running; press [warning]q[/warning] to halt this goal and return to chat.")
        try:
            for _ in range(max_cycles):
                goal = self.goal_store.get(self.current_goal["id"])
                if self._goal_should_stop():
                    goal = self.goal_store.update(goal["id"], status="stopped")
                    self.goal_store.append_event(goal["id"], "stopped", "halted by q key")
                    break
                if goal.get("status") in ("complete", "blocked", "stopped", "superseded"):
                    break
                cycle = int(goal.get("cycles", 0)) + 1
                self.goal_store.append_event(goal["id"], "cycle", f"starting goal cycle {cycle}")
                terminal_theme.print_markup(f"[highlight]goal[/highlight] cycle {cycle} for [muted]{goal['id']}[/muted]")
                response = self._run_llm_tool_loop(
                    self._goal_messages(goal, cycle),
                    max_iterations=MAX_GOAL_TOOL_ITERATIONS,
                    stop_checker=self._goal_should_stop,
                    loop_kind="goal",
                    goal_id=goal["id"],
                )
                if response:
                    terminal_theme.print_markup(response)
                    self.goal_store.append_event(goal["id"], "assistant", response)
                goal = self.goal_store.update(goal["id"], cycles=cycle)
                self.current_goal = goal
                if self._goal_should_stop():
                    goal = self.goal_store.update(goal["id"], status="stopped")
                    self.goal_store.append_event(goal["id"], "stopped", "halted by q key")
                    self.current_goal = goal
                    break
                if goal.get("status") in ("complete", "blocked", "stopped", "superseded"):
                    break
                if sleep_seconds:
                    terminal_theme.print_markup(f"[muted]goal sleeping {sleep_seconds}s before next cycle[/muted]")
                    deadline = time.time() + sleep_seconds
                    while time.time() < deadline and not self._goal_should_stop():
                        time.sleep(min(0.25, deadline - time.time()))
                    if self._goal_should_stop():
                        goal = self.goal_store.update(goal["id"], status="stopped")
                        self.goal_store.append_event(goal["id"], "stopped", "halted by q key")
                        self.current_goal = goal
                        break
        except KeyboardInterrupt:
            goal = self.goal_store.update(self.current_goal["id"], status="active")
            self.goal_store.append_event(goal["id"], "interrupted", "goal run interrupted by operator")
            self.current_goal = goal
            terminal_theme.print_markup("\n[warning]goal[/warning] interrupted; goal remains active and can be resumed")
        except AgentLoopError as exc:
            goal = self.goal_store.update(self.current_goal["id"], status="active")
            self.goal_store.append_event(goal["id"], "error", str(exc))
            self._log_agent_status(
                f"Goal loop paused after error: {exc}",
                loop_kind="goal",
                phase="error",
                goal_id=goal["id"],
            )
            self.current_goal = goal
            terminal_theme.print_markup(
                "\n[warning]goal[/warning] paused after a model/API error; "
                "the goal remains active and can be resumed."
            )
        except Exception as exc:
            message = f"Goal loop crashed: {type(exc).__name__}: {exc}"
            goal = self.goal_store.update(self.current_goal["id"], status="active")
            self.goal_store.append_event(goal["id"], "error", message)
            self._log_agent_status(
                message,
                loop_kind="goal",
                phase="error",
                goal_id=goal["id"],
            )
            self.current_goal = goal
            terminal_theme.print_markup(
                "\n[danger][!][/danger] goal paused after an unexpected error; "
                "the goal remains active and can be resumed."
            )
        finally:
            if cleanup_key_watcher:
                cleanup_key_watcher()
            if cleanup_idle_watchdog:
                cleanup_idle_watchdog()
        goal = self.goal_store.get(self.current_goal["id"])
        if goal.get("status") == "running":
            goal = self.goal_store.update(goal["id"], status="active")
        self.current_goal = goal
        self._print_goal(goal)

    def do_goal(self, line):
        """Create, inspect, or run a durable goal."""
        parser = _parser("goal")
        parser.add_argument("--run", action="store_true", help="Run goal cycles after creating or resuming.")
        parser.add_argument("--resume", help="Resume an existing goal id.")
        parser.add_argument("--status", action="store_true", help="Show active and recent goals.")
        parser.add_argument("--stop", help="Stop a goal id.")
        parser.add_argument("--forever", action="store_true", help="Run cycles until complete, blocked, stopped, or interrupted.")
        parser.add_argument("--max-cycles", type=int, default=1)
        parser.add_argument("--sleep-seconds", type=int, default=0)
        parser.add_argument("objective", nargs="*")
        args = self._run_parser(parser, line)
        if not args:
            return

        if args.status:
            active = self.goal_store.active()
            if active:
                self._print_goal(active)
            else:
                print("[goal] no active goal")
            for goal in self.goal_store.list()[-5:]:
                if not active or goal["id"] != active["id"]:
                    self._print_goal(goal)
            return

        if args.stop:
            goal = self.goal_store.update(args.stop, status="stopped")
            self.goal_store.append_event(goal["id"], "stopped", "stopped by operator")
            self._print_goal(goal)
            return

        if args.resume:
            goal = self.goal_store.get(args.resume)
            if not goal:
                print(f"[!] Unknown goal: {args.resume}")
                return
            if goal.get("status") in ("complete", "blocked", "stopped", "superseded"):
                goal = self.goal_store.update(goal["id"], status="active")
            superseded = self.goal_store.supersede_active(
                replacement_id=goal["id"],
                reason=f"superseded by resumed goal: {goal['id']}",
            )
            if superseded:
                terminal_theme.print_markup(f"[muted]superseded {len(superseded)} active goal(s)[/muted]")
        else:
            objective = " ".join(args.objective).strip()
            if not objective:
                active = self.goal_store.active()
                if active:
                    self._print_goal(active)
                else:
                    print("[!] Usage: /goal [--run] OBJECTIVE")
                return
            superseded = self.goal_store.supersede_active(reason=f"superseded by new goal: {objective}")
            if superseded:
                terminal_theme.print_markup(f"[muted]superseded {len(superseded)} active goal(s)[/muted]")
            goal = self.goal_store.create(objective, metadata={"model": self._active_model()})
            self.goal_store.append_event(goal["id"], "created", objective)

        self.current_goal = goal
        self._print_goal(goal)
        if args.run:
            max_cycles = 10**9 if args.forever else max(args.max_cycles, 1)
            self._run_goal(goal, max_cycles=max_cycles, sleep_seconds=max(args.sleep_seconds, 0))

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

    def do_validate_texts(self, line):
        """Validate extracted plaintext legibility: validate-texts [--limit N]."""
        parser = _parser("validate-texts")
        parser.add_argument("--limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--validator-model", default=text_validator.DEFAULT_VALIDATOR_MODEL)
        parser.add_argument("--workers", type=int, default=4)
        parser.add_argument("--recheck", action="store_true")
        parser.add_argument("--no-llm", action="store_true")
        parser.add_argument("--remove-unusable", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_validate_texts(self._namespace(**vars(args)))

    def do_munge_texts(self, line):
        """Clean processed plaintext into training-ready artifacts: munge-texts [--limit N]."""
        parser = _parser("munge-texts")
        parser.add_argument("--limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--bucket-dir", default=text_munger.DEFAULT_MUNGED_BUCKET_DIR)
        parser.add_argument("--use-llm", action="store_true")
        parser.add_argument("--munger-model", default=text_munger.DEFAULT_MUNGER_MODEL)
        parser.add_argument("--recheck", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_munge_texts(self._namespace(**vars(args)))

    def do_archive_raw(self, line):
        """Upload processed raw originals to object storage: /archive-raw [--limit N]."""
        parser = _parser("archive-raw")
        parser.add_argument("--limit", type=int, default=self.config["process_limit"])
        parser.add_argument("--keep-local", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_archive_raw(self._namespace(**vars(args)))

    def do_rss_ingest(self, line):
        """Archive configured RSS feed items: /rss-ingest [--limit-per-feed N]."""
        parser = _parser("rss-ingest")
        parser.add_argument("--feeds-file", default=str(self.cli.rss_ingest.DEFAULT_FEEDS_PATH))
        parser.add_argument("--limit-per-feed", type=int, default=self.cli.rss_ingest.DEFAULT_LIMIT_PER_FEED)
        parser.add_argument("--timeout", type=int, default=self.cli.rss_ingest.DEFAULT_TIMEOUT_SECONDS)
        parser.add_argument("--dry-run", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_rss_ingest(self._namespace(**vars(args)))

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

    def do_auto(self, line):
        """Continuously expand the data lake in the background: auto [--stop|--status]."""
        parser = _parser("auto")
        parser.add_argument("--stop", action="store_true")
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--query", action="append")
        parser.add_argument("--queries-file")
        parser.add_argument("--sources", nargs="+", choices=self.cli.ALL_SOURCES, default=self.config["sources"])
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--query-limit", type=int, default=12)
        parser.add_argument("--auto-focus")
        parser.add_argument("--sleep-seconds", type=int, default=1800)
        parser.add_argument("--error-sleep-seconds", type=int, default=300)
        parser.add_argument("--max-error-sleep-seconds", type=int, default=3600)
        parser.add_argument("--max-results", type=int, default=self.config["max_results"])
        parser.add_argument("--download-limit", type=int, default=max(self.config["download_limit"], 100))
        parser.add_argument("--process-limit", type=int, default=max(self.config["process_limit"], 100))
        parser.add_argument("--archive-raw-limit", type=int, default=50)
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
        if args.stop:
            if self._auto_thread and self._auto_thread.is_alive():
                self._auto_stop_event.set()
                print("[+] auto stop requested.")
            else:
                self._auto_focus = None
                print("[=] auto is not running.")
            return
        if args.status:
            status = "running" if self._auto_thread and self._auto_thread.is_alive() else "stopped"
            suffix = f" focus={self._auto_focus}" if self._auto_focus else ""
            print(f"auto: {status}{suffix}")
            return
        if self._auto_thread and self._auto_thread.is_alive():
            print("[!] auto is already running. Use /auto --stop first.")
            return

        values = vars(args)
        values.pop("stop", None)
        values.pop("status", None)
        values["auto_focus"] = values.get("auto_focus") or self.cli.random_auto_focus()
        self._auto_focus = values["auto_focus"]
        self._auto_stop_event.clear()
        auto_args = self._namespace(**values, should_stop=self._auto_stop_event.is_set)

        def run_auto():
            try:
                self.cli.handle_auto(auto_args)
            except Exception as exc:
                terminal_theme.print_markup(f"[danger]auto loop failed: {type(exc).__name__}: {exc}[/danger]")
            finally:
                self._auto_focus = None
                self._auto_stop_event.clear()

        self._auto_thread = threading.Thread(target=run_auto, name="alge-auto", daemon=True)
        self._auto_thread.start()
        print(f"[+] auto started in the background with focus: {self._auto_focus}. Use /auto --status or /auto --stop.")

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
        parser.add_argument("--munged", action="store_true")
        args = self._run_parser(parser, line)
        if not args:
            return
        self.cli.handle_corpus(self._namespace(**vars(args)))

    def do_help(self, arg):
        if arg:
            return super().do_help(arg)
        table = terminal_theme.make_table("Command", "Purpose", title="ALGE Commands")
        rows = [
            ("/status", "Show database counts and pipeline state."),
            ("/config", "Show session defaults used by agent commands."),
            ("/model [MODEL_ID]", "Choose the active OpenRouter model."),
            ("/set KEY VALUE", "Update session, memory, and compaction defaults."),
            ("/search QUERY", "Discover archive records for a query."),
            ("/url URL", "Ingest one archive detail page."),
            ("/research TOPIC", "Generate focused terms, crawl them, and write a report."),
            ("/download", "Download pending files into the raw bucket."),
            ("/process", "Extract plaintext from downloaded files."),
            ("/validate-texts", "Validate plaintext quality and reject unreadable text."),
            ("/munge-texts", "Clean processed plaintext into training-ready artifacts."),
            ("/archive-raw", "Upload processed raw originals to object storage."),
            ("/rss-ingest", "Archive configured RSS/Atom feed items into the backlog."),
            ("/cycle", "Run one discover-download-process cycle."),
            ("/auto", "Continuously expand the data lake from sparse categories and rotating seeds."),
            ("/corpus NAME", "Build a deterministic corpus manifest and text bundle."),
            ("/memory", "Read saved command and note context logs."),
            ("/remember TEXT", "Save an operator note into context memory."),
            ("/context", "Show context window and compaction threshold."),
            ("/compact", "Compact saved context logs."),
            ("/goal OBJECTIVE", "Create or resume a durable long-running archival goal."),
            ("/exit", "Leave the harness."),
        ]
        for command, purpose in rows:
            table.add_row(f"[highlight]{command}[/highlight]", purpose)
        terminal_theme.console.print(table)
        terminal_theme.print_panel(
            "Normal text without a slash is sent to the active model as chat.\n"
            "Assistant replies can include Rich tags like [highlight]pond-scum green[/highlight], "
            "[success]success[/success], [warning]warning[/warning], and [danger]danger[/danger].",
            title="Markup",
            border_style="pond",
        )


def run_agent(cli_module, command=None):
    shell = ArchiveAgentShell(cli_module)
    if command:
        shell.onecmd(command)
        return
    terminal_theme.print_panel(
        "ALGE archive harness\n"
        "[muted]Type[/muted] [highlight]/help[/highlight] [muted]for commands, "
        "[highlight]/config[/highlight] [muted]for defaults, or[/muted] "
        "[highlight]/exit[/highlight] [muted]to leave.[/muted]",
        title="Agentic Lexicon Generation Engine",
        border_style="pond",
    )
    shell.cmdloop()
