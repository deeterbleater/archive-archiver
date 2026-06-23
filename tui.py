import contextlib
from datetime import datetime, timezone
import os
import re
import select
import subprocess
import sys
import termios
import time
import tty

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import db
import goals
import processor
import terminal_theme


COMMANDS = [
    ("discover", "/search", "find"),
    ("fetch", "/download", "raw"),
    ("extract", "/process", "text"),
    ("archive", "/archive-raw", "s3"),
    ("loop", "/cycle", "pass"),
    ("auto", "/auto", "watch"),
    ("goal", "/goal", "long"),
    ("help", "/help", "all"),
]
COMMAND_DETAILS = {
    "/search": "find and queue new public-domain works",
    "/download": "fetch pending files into the raw bucket",
    "/process": "extract readable text from downloaded files",
    "/archive-raw": "ship processed originals to S3-compatible storage",
    "/cycle": "run one discover-download-process pass",
    "/auto": "start, stop, or inspect autonomous work",
    "/goal": "delegate or inspect sustained corpus goals",
    "/help": "show every slash command and option",
}
ACTION_EXAMPLES = {
    "overview": [
        ("status", "/status", "refresh pipeline counts"),
        ("cycle", '/cycle --query "public domain philosophy" --download-limit 25 --process-limit 25', "run one balanced pass"),
        ("auto", "/auto --once --download-limit 50 --process-limit 50", "let the harness pick focused queries"),
    ],
    "queue": [
        ("download", "/download --limit 25 --domain-workers", "fetch pending files"),
        ("process", "/process --limit 25", "extract downloaded raw files"),
        ("archive", "/archive-raw --limit 25", "ship processed originals"),
    ],
    "failures": [
        ("status", "/status", "inspect failed stages"),
        ("retry text", "/process --limit 25", "retry text extraction after review"),
        ("retry raw", "/archive-raw --limit 25", "retry raw-object archival"),
    ],
    "activity": [
        ("auto status", "/auto --status", "check autonomous run state"),
        ("goal", "/goal", "inspect the active long-running goal"),
        ("memory", "/memory --limit 10", "review saved operating context"),
    ],
    "controls": [
        ("search", '/search "public domain labor history" --max-results 5', "add new works"),
        ("drain", "/download --limit 25 --domain-workers", "start clearing queued downloads"),
        ("loop", '/cycle --query "public domain philosophy" --download-limit 25 --process-limit 25', "run a complete pass"),
        ("goal", '/goal --run --forever --sleep-seconds 300 "Expand the corpus and process everything that lands"', "delegate sustained work"),
    ],
}
COMPACT_HEIGHT = 28
STALE_SECONDS = 30 * 60
VIEWS = ("overview", "queue", "failures", "activity", "controls")
VIEW_DETAILS = {
    "overview": ("overview", "pipeline health and next action"),
    "queue": ("queue", "work waiting at each pipeline stage"),
    "failures": ("failures", "recent breakages that need triage"),
    "activity": ("activity", "latest agent and tool movement"),
    "controls": ("controls", "slash commands for direct operation"),
}
VIEW_KEYS = {
    "o": "overview",
    "1": "overview",
    "w": "queue",
    "2": "queue",
    "f": "failures",
    "3": "failures",
    "a": "activity",
    "4": "activity",
    "c": "controls",
    "5": "controls",
}
NEXT_VIEW_KEYS = {"\t", "n", "l"}
PREVIOUS_VIEW_KEYS = {"p", "h"}


def _count_style(value):
    return "success" if not value else "warning"


def _pending_total(backlog):
    return (
        backlog["pending_downloads"]
        + backlog["pending_extractions"]
        + backlog["pending_raw_archives"]
    )


def _failure_total(stats, backlog, workers):
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    return (
        backlog["failed_downloads"]
        + int(extractions.get("failed", 0) or 0)
        + int(raw_archives.get("failed", 0) or 0)
        + workers["failed"]
    )


def _health_state(backlog, workers, scans, stats=None, freshness=None):
    stats = stats or {}
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    if scans.get("infected", 0):
        return "quarantine", "danger", "infected files require review"
    if workers["failed"]:
        return "worker issue", "danger", "a background worker failed"
    if backlog["failed_downloads"] or extractions.get("failed", 0) or raw_archives.get("failed", 0):
        return "attention", "danger", "pipeline failures need triage"
    if workers["running"]:
        return "running", "tool", "background work is active"
    if freshness and freshness.get("stale"):
        return "stale", "warning", "no recent agent activity"
    if _pending_total(backlog):
        return "queued", "warning", "pipeline has work ready"
    return "clear", "success", "no queued work"


def _panel(renderable, title, border_style="pond", padding=(0, 1)):
    return Panel(renderable, title=title, border_style=border_style, box=ROUNDED, padding=padding)


def _metric_table(title, rows, border_style="pond"):
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="muted", ratio=1)
    table.add_column(justify="right", style="highlight", no_wrap=True)
    for label, value, style in rows:
        table.add_row(label, Text(str(value), style=style))
    return _panel(table, title, border_style=border_style)


def _summary_panel(stats, backlog, workers, scans, freshness=None):
    downloads = stats.get("downloads_by_status", {})
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    pending_total = _pending_total(backlog)
    _state, state_style, state_reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    table = Table.grid(expand=True, padding=(0, 2))
    for _index in range(6):
        table.add_column(justify="center")
    table.add_row(
        Text("works", style="muted"),
        Text("files", style="muted"),
        Text("queue", style="muted"),
        Text("downloaded", style="muted"),
        Text("texts", style="muted"),
        Text("s3 raw", style="muted"),
    )
    table.add_row(
        Text(str(stats["total_works"]), style="highlight"),
        Text(str(stats["total_files"]), style="highlight"),
        Text(str(pending_total), style=_count_style(pending_total)),
        Text(str(downloads.get("downloaded", 0)), style="success"),
        Text(str(extractions.get("processed", 0)), style="success"),
        Text(str(raw_archives.get("archived", 0)), style="success"),
    )
    flow = Table.grid(expand=True, padding=(0, 1))
    flow.add_column(style="muted", no_wrap=True)
    flow.add_column(no_wrap=True)
    flow.add_column(justify="right", style="label", no_wrap=True)
    total_files = max(0, int(stats.get("total_files") or 0))
    downloaded = int(downloads.get("downloaded", 0) or 0)
    processed = int(extractions.get("processed", 0) or 0)
    text_total = (
        processed
        + int(extractions.get("failed", 0) or 0)
        + int(extractions.get("skipped", 0) or 0)
        + backlog["pending_extractions"]
    )
    archived = int(raw_archives.get("archived", 0) or 0)
    raw_total = sum(int(value or 0) for value in raw_archives.values())
    flow.add_row("raw", _bar(downloaded, total_files, complete_style="tool"), _percent(downloaded, total_files))
    flow.add_row("text", _bar(processed, text_total), _percent(processed, text_total))
    flow.add_row("s3", _bar(archived, raw_total, complete_style="highlight"), _percent(archived, raw_total))
    worker_line = Text()
    worker_line.append("workers ", style="muted")
    worker_line.append(str(workers["running"]), style="tool" if workers["running"] else "muted")
    worker_line.append(" running  ", style="muted")
    worker_line.append(str(workers["idle"]), style="success")
    worker_line.append(" idle  ", style="muted")
    worker_line.append(str(workers["failed"]), style="danger" if workers["failed"] else "success")
    worker_line.append(" failed", style="muted")
    reason = Text.assemble(("health: ", "muted"), (state_reason, state_style))
    queue = Text()
    for index, (label, value, style) in enumerate([
        ("download", backlog["pending_downloads"], "warning"),
        ("text", backlog["pending_extractions"], "tool"),
        ("raw", backlog["pending_raw_archives"], "highlight"),
    ]):
        if index:
            queue.append("  ")
        queue.append(label, style="muted")
        queue.append(" ")
        queue.append(str(value), style=style if value else "muted")
    return Group(
        _panel(Group(table, flow), "Pipeline", border_style=state_style),
        Align.center(worker_line),
        Align.center(reason),
        Align.center(_freshness_line(freshness)),
        Align.center(queue),
    )


def _compact_summary_panel(stats, backlog, workers, scans, freshness=None, width=None):
    downloads = stats.get("downloads_by_status", {})
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    pending_total = _pending_total(backlog)
    failure_total = _failure_total(stats, backlog, workers)
    _state, state_style, state_reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(style="muted", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_row(
        "queue",
        Text(str(pending_total), style=_count_style(pending_total)),
        "fail",
        Text(str(failure_total), style="danger" if failure_total else "success"),
    )
    table.add_row(
        "raw",
        Text(str(downloads.get("downloaded", 0)), style="tool"),
        "text",
        Text(str(extractions.get("processed", 0)), style="success"),
    )
    table.add_row(
        "s3",
        Text(str(raw_archives.get("archived", 0)), style="highlight"),
        "seen",
        Text(_freshness_age(freshness), style=_freshness_style(freshness)),
    )
    detail = Text()
    detail.append(state_reason, style=state_style)
    if not (width and width < 60):
        detail.append("  ")
        detail.append("workers ", style="muted")
        detail.append(str(workers["running"]), style="tool" if workers["running"] else "muted")
        detail.append(" run  ", style="muted")
        detail.append(str(workers["failed"]), style="danger" if workers["failed"] else "success")
        detail.append(" fail", style="muted")
    return _panel(Group(table, Align.center(detail)), "Pipeline / Backlog", border_style=state_style)


def _failure_summary_panel(stats, backlog, workers, scans):
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    failures = [
        ("downloads", backlog["failed_downloads"]),
        ("text", int(extractions.get("failed", 0) or 0)),
        ("raw", int(raw_archives.get("failed", 0) or 0)),
        ("workers", workers["failed"]),
        ("quarantine", scans.get("infected", 0)),
    ]
    total = _failure_total(stats, backlog, workers) + int(scans.get("infected", 0) or 0)
    table = Table.grid(expand=True, padding=(0, 1))
    for _index in range(6):
        table.add_column(justify="center")
    table.add_row(
        Text("total", style="muted"),
        *(Text(label, style="muted") for label, _value in failures),
    )
    table.add_row(
        Text(str(total), style="danger" if total else "success"),
        *(Text(str(value), style="danger" if value else "success") for _label, value in failures),
    )
    return _panel(table, "Failure Summary", border_style="danger" if total else "success")


def _status_token(label, value, style):
    token = Text()
    token.append(f"{label} ", style="muted")
    token.append(str(value), style=style)
    return token


def _ratio(done, total):
    total = int(total or 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, float(done or 0) / total))


def _bar(done, total, width=16, complete_style="success", pending_style="muted"):
    ratio = _ratio(done, total)
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    bar = Text()
    if filled:
        bar.append("█" * filled, style=complete_style)
    if filled < width:
        bar.append("░" * (width - filled), style=pending_style)
    return bar


def _percent(done, total):
    return f"{int(round(_ratio(done, total) * 100)):>3}%"


def _hero(stats, backlog, workers, scans, freshness=None, view="overview", width=None):
    pending_total = _pending_total(backlog)
    failure_total = _failure_total(stats, backlog, workers)
    state, state_style, _reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    view_label, view_description = _view_detail(view)
    narrow = bool(width and width <= 60)
    table = Table.grid(expand=True)
    table.add_column(ratio=1, no_wrap=True)
    title = Text("ALGE", style="bold highlight")
    metrics = Text()
    tokens = [
        _status_token("state", state, state_style),
        _status_token("queue", pending_total, _count_style(pending_total)),
        _status_token("fail", failure_total, "danger" if failure_total else "success"),
    ]
    if not narrow:
        table.add_column(justify="right", ratio=1)
        tokens.extend([
            _status_token("run", workers["running"], "tool" if workers["running"] else "muted"),
            _status_token("texts", stats.get("extractions_by_status", {}).get("processed", 0), "success"),
            _status_token("seen", _freshness_age(freshness), _freshness_style(freshness)),
        ])
    for index, token in enumerate(tokens):
        if index:
            metrics.append("  " if narrow else "   ")
        metrics.append(token)
    if narrow:
        table.add_row(Text.assemble(title, ("  "), metrics))
    else:
        table.add_row(title, metrics)
    detail = Text()
    detail.append(view_label, style="label")
    if not narrow:
        detail.append("  ")
        detail.append(view_description, style="muted")
    focus = _view_focus_text(view, backlog, workers, scans, stats, freshness=freshness)
    if focus:
        detail.append("  " if narrow else "  |  ", style="muted")
        detail.append(focus)
    return _panel(Group(table, Align.center(detail)), "Command Deck", border_style=state_style)


def _view_focus_text(view, backlog, workers, scans, stats, freshness=None):
    view = _normalize_view(view)
    if view == "queue":
        if backlog["pending_extractions"]:
            return Text(f"{backlog['pending_extractions']} files need text", style="tool")
        if backlog["pending_downloads"]:
            return Text(f"{backlog['pending_downloads']} downloads ready", style="warning")
        if backlog["pending_raw_archives"]:
            return Text(f"{backlog['pending_raw_archives']} raw files need s3", style="highlight")
        return Text("queue is clear", style="success")
    if view == "failures":
        total = _failure_total(stats, backlog, workers) + int(scans.get("infected", 0) or 0)
        return Text(f"{total} issues in triage", style="danger" if total else "success")
    if view == "activity":
        if workers["running"]:
            return Text(f"{workers['running']} workers active", style="tool")
        return Text(f"last event {_freshness_age(freshness)}", style=_freshness_style(freshness))
    if view == "controls":
        _label, command, _hint = _primary_action(view, backlog=backlog, workers=workers, scans=scans, stats=stats)
        return Text(f"primary {command.split()[0]}", style="highlight")
    _state, state_style, state_reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    return Text(_shorten(state_reason, 42), style=state_style)


def _top_chrome(stats, backlog, workers, scans, freshness=None, view="overview", interactive=False, width=None):
    return Group(
        _hero(stats, backlog, workers, scans, freshness=freshness, view=view, width=width),
        _view_bar(view, interactive=interactive, width=width),
    )


def _recent_activity(limit=6):
    rows = db.get_recent_agent_statuses(limit=limit)
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1)
    if not rows:
        table.add_row("", "", Text("No agent activity recorded yet.", style="muted"))
        return _panel(table, "Activity")

    for row in rows:
        created_at = _activity_time_label(row.get("created_at"))
        phase = str(row.get("phase") or "update")
        message = _activity_message(str(row.get("message") or "").strip())
        label, style = _activity_label(phase, message)
        table.add_row(created_at, Text(label, style=style), Text(_shorten(message, 68), overflow="ellipsis", no_wrap=True))
    return _panel(table, "Activity")


def _activity_counts(rows):
    counts = {"done": 0, "run": 0, "wait": 0, "fail": 0, "note": 0}
    for row in rows:
        message = _activity_message(str(row.get("message") or "").strip())
        label, _style = _activity_label(str(row.get("phase") or "update"), message)
        if label in counts:
            counts[label] += 1
        else:
            counts["note"] += 1
    return counts


def _activity_decision(counts):
    if counts["fail"]:
        return Text(f"{counts['fail']} recent failures need review", style="danger")
    if counts["run"]:
        return Text(f"{counts['run']} active starts; watch for done/fail", style="tool")
    if counts["wait"] and not counts["done"]:
        return Text("agent is waiting; choose the next command", style="warning")
    if counts["done"]:
        return Text(f"{counts['done']} recent completions", style="success")
    return Text("no clear activity signal yet", style="muted")


def _activity_count_style(label, count):
    if not count:
        return "muted"
    return {
        "done": "success",
        "run": "tool",
        "wait": "warning",
        "fail": "danger",
        "note": "muted",
    }.get(label, "tool")


def _activity_summary_panel(limit=24):
    rows = db.get_recent_agent_statuses(limit=limit)
    counts = _activity_counts(rows)
    table = Table.grid(expand=True, padding=(0, 2))
    for _index in range(len(counts)):
        table.add_column(justify="center")
    table.add_row(*(Text(label, style="muted") for label in counts))
    table.add_row(
        *(Text(str(counts[label]), style=_activity_count_style(label, counts[label])) for label in counts)
    )
    decision = Align.center(_activity_decision(counts))
    return _panel(Group(table, decision), "Activity Summary", border_style="danger" if counts["fail"] else "pond")


def _activity_time_label(created_at, now=None):
    age = _age_from_timestamp(created_at, now=now)
    if age:
        return age
    return str(created_at or "")[-8:]


def _activity_message(message):
    message = " ".join(str(message or "").split())
    replacements = [
        (r"^Batch\s+[-\w]+\s+complete:\s*", ""),
        (r"^Batch\s+[-\w]+\s+started:\s*", ""),
        (r"^Batch\s+[-\w]+\s+failed:\s*", "failed "),
        (r"^Tool\s+[-\w]+\s+complete:\s*", ""),
        (r"^Tool\s+[-\w]+\s+failed:\s*", "failed "),
    ]
    for pattern, replacement in replacements:
        message = re.sub(pattern, replacement, message, flags=re.IGNORECASE)
    return message


def _activity_label(phase, message):
    phase = str(phase or "").lower()
    lowered = str(message or "").lower()
    if "failed" in lowered or phase in {"error", "failed"}:
        return "fail", "danger"
    if phase in {"start", "started"}:
        return "run", "tool"
    if phase in {"idle", "waiting"}:
        return "wait", "warning"
    if phase in {"end", "complete", "completed"}:
        return "done", "success"
    return phase[:4] or "note", "tool"


def _parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _freshness(latest_status=None, now=None):
    latest_status = latest_status if latest_status is not None else db.get_latest_agent_status()
    created_at = _parse_timestamp((latest_status or {}).get("created_at"))
    now = now or datetime.now(timezone.utc)
    if created_at is None:
        return {"age_seconds": None, "age": "unknown", "stale": True, "message": "no agent activity recorded"}
    age_seconds = max(0, int((now - created_at).total_seconds()))
    return {
        "age_seconds": age_seconds,
        "age": _format_age(age_seconds),
        "stale": age_seconds >= STALE_SECONDS,
        "message": _activity_message((latest_status or {}).get("message") or ""),
    }


def _freshness_age(freshness):
    return (freshness or {}).get("age") or "unknown"


def _freshness_style(freshness):
    if not freshness or freshness.get("age_seconds") is None:
        return "warning"
    return "warning" if freshness.get("stale") else "success"


def _freshness_line(freshness):
    freshness = freshness or {}
    text = Text()
    text.append("last activity ", style="muted")
    text.append(_freshness_age(freshness), style=_freshness_style(freshness))
    message = _shorten(freshness.get("message") or "", 64)
    if message:
        text.append("  ", style="muted")
        text.append(message, style="muted")
    return text


def _age_from_timestamp(value, now=None):
    parsed = _parse_timestamp(value)
    if parsed is None:
        return ""
    now = now or datetime.now(timezone.utc)
    return _format_age(max(0, int((now - parsed).total_seconds())))


def _command_reference():
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="highlight", no_wrap=True)
    table.add_column(style="label", no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(Text("command", style="muted"), Text("role", style="muted"), Text("use", style="muted"))
    for label, command, hint in COMMANDS:
        detail = COMMAND_DETAILS.get(command, hint)
        table.add_row(command, label, Text(detail, style="muted", overflow="fold"))
    return _panel(table, "Controls")


def _compact_command_reference():
    table = Table.grid(expand=True, padding=(0, 1))
    for _index in range(2):
        table.add_column(ratio=1)
    for row_start in range(0, len(COMMANDS), 2):
        cells = []
        for label, command, hint in COMMANDS[row_start: row_start + 2]:
            cells.append(Text.assemble((command, "highlight"), ("  "), (label, "label"), (" / "), (hint, "muted")))
        while len(cells) < 2:
            cells.append(Text(""))
        table.add_row(*cells)
    return _panel(table, "Command Reference")


def _action_rows(view="overview", backlog=None, workers=None, scans=None, stats=None, limit=None):
    view = _normalize_view(view)
    rows = list(ACTION_EXAMPLES.get(view) or ACTION_EXAMPLES["overview"])
    if view == "queue" and backlog:
        priority = None
        if backlog["pending_extractions"]:
            priority = "/process --limit 25"
        elif backlog["pending_downloads"]:
            priority = "/download --limit 25 --domain-workers"
        elif backlog["pending_raw_archives"]:
            priority = "/archive-raw --limit 25"
        if priority:
            rows.sort(key=lambda row: 0 if row[1] == priority else 1)
    if view == "failures":
        scans = scans or {}
        workers = workers or {"failed": 0}
        if scans.get("infected", 0):
            rows.insert(0, ("quarantine", "/status", "review quarantine before more promotion"))
        elif workers.get("failed", 0):
            rows.insert(0, ("workers", "/status", "locate failed background work"))
    if limit:
        rows = rows[:limit]
    return rows


def _command_examples(view="overview", backlog=None, workers=None, scans=None, stats=None, limit=None):
    rows = _action_rows(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=limit)

    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(style="label", no_wrap=True)
    table.add_column(ratio=2)
    table.add_column(ratio=1)
    for index, (label, command, hint) in enumerate(rows, start=1):
        marker = "now" if index == 1 else "next" if index == 2 else "later"
        table.add_row(
            Text(marker, style="highlight" if index == 1 else "muted"),
            label,
            Text(command, style="highlight", overflow="ellipsis", no_wrap=True),
            Text(hint, style="muted", overflow="ellipsis", no_wrap=True),
        )
    return _panel(table, "Next Actions", border_style="tool")


def _primary_action(view="overview", backlog=None, workers=None, scans=None, stats=None):
    rows = _action_rows(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=1)
    return rows[0] if rows else ("status", "/status", "refresh pipeline counts")


def _primary_command_line(view, backlog, workers, scans, stats, interactive=False):
    _label, command, _hint = _primary_action(view, backlog=backlog, workers=workers, scans=scans, stats=stats)
    prefix = "enter paste " if interactive and os.environ.get("TMUX") else "primary "
    return Text.assemble(
        (prefix, "muted"),
        (_shorten(command, 60), "highlight"),
    )


def _command_tray(view, backlog, workers, scans, stats, interactive=False):
    label, command, hint = _primary_action(view, backlog=backlog, workers=workers, scans=scans, stats=stats)
    action = "enter pastes into agent pane" if interactive and os.environ.get("TMUX") else "type in the agent pane"
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(action, Text(command, style="highlight", overflow="ellipsis", no_wrap=True))
    table.add_row("why", Text.assemble((label, "label"), (" / "), (hint, "muted")))
    return _panel(
        table,
        "Primary Command",
        border_style="tool",
    )


def _format_title(row):
    title = str(row.get("title") or "Untitled").strip()
    author = str(row.get("author") or "").strip()
    if author:
        return f"{title} - {author}"
    return title


def _shorten(value, limit):
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "..."


def _source_label(row, limit=18):
    return _shorten(str(row.get("site") or row.get("format") or ""), limit)


def _stage_badge(label, status=None):
    text = Text()
    label = str(label or "")
    if not label:
        return text
    text.append(label, style="label")
    if status:
        text.append(f":{status}", style="danger")
    return text


def _meta_label(row, limit=22):
    parts = []
    age = _age_from_timestamp(row.get("updated_at"))
    if age:
        parts.append(age)
    source = _source_label(row, limit=limit)
    if source:
        parts.append(source)
    return _shorten("  ".join(parts), limit)


def _queue_preview(download_limit=2, extraction_limit=2, raw_limit=1):
    pending_downloads = db.get_pending_download_files(limit=download_limit)
    pending_extractions = db.get_pending_extractions(limit=extraction_limit, extractor=processor.EXTRACTOR_VERSION)
    raw_archives = db.get_raw_archive_candidates(limit=raw_limit)
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="label", no_wrap=True)
    table.add_column(ratio=1)
    table.add_column(style="muted", no_wrap=True)

    rows = [
        ("download", pending_downloads, "No downloads queued."),
        ("text", pending_extractions, "No files need text."),
        ("raw", raw_archives, "No raw files ready."),
    ]
    for label, queue_rows, empty in rows:
        if not queue_rows:
            table.add_row(label, Text(empty, style="muted"), "")
            continue
        for index, row in enumerate(queue_rows):
            source = _source_label(row, limit=18)
            if row.get("updated_at"):
                meta = _meta_label(row, limit=18)
            else:
                meta = source
            table.add_row(
                _stage_badge(label if index == 0 else ""),
                Text(_shorten(_format_title(row), 66), overflow="ellipsis", no_wrap=True),
                meta,
            )
    return _panel(table, "Queue")


def _overview_queue_panel(backlog):
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="label", no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column(ratio=2)
    rows = [
        ("download", backlog["pending_downloads"], db.get_pending_download_files(limit=1), "No downloads queued."),
        ("text", backlog["pending_extractions"], db.get_pending_extractions(limit=1, extractor=processor.EXTRACTOR_VERSION), "No files need text."),
        ("raw", backlog["pending_raw_archives"], db.get_raw_archive_candidates(limit=1), "No raw files ready."),
    ]
    for label, count, queue_rows, empty in rows:
        preview = _shorten(_format_title(queue_rows[0]), 46) if queue_rows else empty
        table.add_row(
            label,
            Text(str(count), style=_count_style(count)),
            Text(preview, style="muted" if not queue_rows else "label", overflow="ellipsis", no_wrap=True),
        )
    return _panel(table, "Work Queue")


def _triage_panel(limit=6):
    rows = db.get_recent_pipeline_failures(limit=limit)
    if not rows:
        return None

    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="danger", no_wrap=True)
    table.add_column(ratio=2)
    table.add_column(ratio=1)
    table.add_column(style="muted", no_wrap=True)
    for row in rows:
        stage = str(row.get("stage") or "fail")
        status = row.get("status_code")
        error = _shorten(row.get("error") or f"{stage} failed", 28)
        title = _shorten(_format_title(row), 34)
        table.add_row(
            _stage_badge(stage, status=status),
            Text(title, style="label", overflow="ellipsis", no_wrap=True),
            Text(error, style="muted", overflow="ellipsis", no_wrap=True),
            _meta_label(row, limit=18),
        )
    return _panel(table, "Triage", border_style="danger")


def _attention_panel(backlog, compact=False):
    triage = _triage_panel(limit=1 if compact else 6)
    if triage:
        return triage
    return _queue_preview()


def _normalize_view(view):
    view = str(view or "overview").strip().lower()
    aliases = {
        "work": "queue",
        "fail": "failures",
        "failure": "failures",
        "triage": "failures",
        "log": "activity",
        "logs": "activity",
        "command": "controls",
        "commands": "controls",
        "help": "controls",
    }
    view = aliases.get(view, view)
    return view if view in VIEWS else "overview"


def _view_detail(view):
    return VIEW_DETAILS[_normalize_view(view)]


def _cycle_view(view, direction=1):
    view = _normalize_view(view)
    index = VIEWS.index(view)
    return VIEWS[(index + direction) % len(VIEWS)]


def _view_for_key(key, current_view):
    if not key:
        return current_view
    key = key.lower()
    if key in NEXT_VIEW_KEYS:
        return _cycle_view(current_view, 1)
    if key in PREVIOUS_VIEW_KEYS:
        return _cycle_view(current_view, -1)
    return VIEW_KEYS.get(key, current_view)


def _view_bar(active_view, interactive=False, width=None):
    active_view = _normalize_view(active_view)
    line = Text()
    narrow = bool(width and width < 60)
    for index, view in enumerate(VIEWS):
        if index:
            line.append(" " if narrow else "  ")
        key = {"overview": "o", "queue": "w", "failures": "f", "activity": "a", "controls": "c"}[view]
        label = {"overview": "overview", "queue": "queue", "failures": "fail", "activity": "activity", "controls": "controls"}[view]
        style = "reverse highlight" if view == active_view else "muted"
        line.append(f" {key} " if narrow else f" {key} {label} ", style=style)
    if interactive:
        if narrow:
            hint = "enter paste   tab/n   p/h   q" if os.environ.get("TMUX") else "tab/n   p/h   q"
        else:
            hint = "enter paste   tab/n next   p/h prev   q quit" if os.environ.get("TMUX") else "tab/n next   p/h prev   q quit"
        keys = Text(hint, style="muted")
        return Group(Align.center(line), Align.center(keys))
    line.append("  --view" if narrow else "   use --view", style="muted")
    return Align.center(line)


def _compact_goal():
    active_goal = goals.GoalStore().active()
    if not active_goal:
        return Text("goal: none", style="muted")
    objective = _shorten(active_goal.get("objective", ""), 92)
    return Text.assemble(
        ("goal: ", "muted"),
        (str(active_goal.get("status", "active")), "highlight"),
        ("  "),
        (objective, "label"),
    )


def _operation_hint(backlog, workers, scans=None, stats=None, view="overview"):
    scans = scans or {}
    stats = stats or {}
    view = _normalize_view(view)
    extractions = stats.get("extractions_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})
    if view == "queue":
        if backlog["pending_extractions"]:
            return Text("Run /process --limit 25", style="highlight")
        if backlog["pending_downloads"]:
            return Text("Run /download --limit 25 --domain-workers", style="highlight")
        if backlog["pending_raw_archives"]:
            return Text("Run /archive-raw --limit 25", style="highlight")
        return Text("Queue clear. Use /search or /auto.", style="success")
    if view == "failures":
        if scans.get("infected", 0):
            return Text("Inspect quarantine before promotion.", style="danger")
        if workers["failed"]:
            return Text("Run /status, then restart failed work.", style="danger")
        if backlog["failed_downloads"] or extractions.get("failed", 0) or raw_archives.get("failed", 0):
            return Text("Review Triage, then retry or exclude.", style="danger")
        return Text("No recent pipeline failures in triage.", style="success")
    if view == "activity":
        if workers["running"]:
            return Text("Watch for fresh done/fail rows.", style="tool")
        return Text("Check /auto --status or /goal.", style="highlight")
    if view == "controls":
        return Text("Type a slash command in the agent pane.", style="highlight")
    if scans.get("infected", 0):
        return Text("Next: review quarantine.", style="danger")
    if workers["failed"]:
        return Text("Next: inspect /status, then restart failed work.", style="danger")
    if backlog["failed_downloads"] or extractions.get("failed", 0) or raw_archives.get("failed", 0):
        return Text("Next: review pipeline failures before queue growth.", style="danger")
    if workers["running"]:
        return Text("Workers active. Watch activity for stalls.", style="tool")
    if backlog["pending_extractions"]:
        return Text("Next: /process --limit 25", style="highlight")
    if backlog["pending_downloads"]:
        return Text("Next: /download --limit 25 --domain-workers", style="highlight")
    if backlog["pending_raw_archives"]:
        return Text("Next: /archive-raw --limit 25", style="highlight")
    return Text("Queue clear. Use /search or /auto.", style="success")


def _collect_state():
    stats = db.get_stats()
    backlog = db.get_backlog_counts()
    workers = db.get_agent_worker_counts()
    scans = stats.get("scans_by_status", {})
    freshness = _freshness()
    return stats, backlog, workers, scans, freshness


def _notice_line(notice):
    if not notice:
        return None
    style = "success"
    message = notice
    if isinstance(notice, tuple):
        style, message = notice
    style = style if style in {"success", "warning", "danger", "tool", "highlight", "muted"} else "success"
    return Align.center(Text(str(message), style=style))


def _footer(backlog, workers, scans, stats, state_style, view="overview", interactive=False, compact=False, notice=None):
    cue = _operation_hint(backlog, workers, scans, stats, view=view)
    if compact:
        lines = [Align.center(_primary_command_line(view, backlog, workers, scans, stats, interactive=interactive)), Align.center(cue)]
        if notice:
            lines.insert(0, _notice_line(notice))
        return _panel(Group(*lines), "Operator Cue", border_style=state_style)
    parts = [
        Align.center(_compact_goal()),
        _command_tray(view, backlog, workers, scans, stats, interactive=interactive),
        _panel(Align.center(cue), "Operator Cue", border_style=state_style),
    ]
    if notice:
        parts.insert(1, _notice_line(notice))
    parts.append(Align.center(Text("tmux persistent  close terminal anytime  run alge to reconnect", style="muted")))
    return Group(
        *parts
    )


def _render_full(stats, backlog, workers, scans, freshness=None, view="overview", interactive=False, notice=None, width=None):
    view = _normalize_view(view)
    state, state_style, _reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    top = _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width)
    if view == "queue":
        return Group(
            top,
            _summary_panel(stats, backlog, workers, scans, freshness=freshness),
            _queue_preview(download_limit=8, extraction_limit=6, raw_limit=4),
            _command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, notice=notice),
        )
    if view == "failures":
        return Group(
            top,
            _summary_panel(stats, backlog, workers, scans, freshness=freshness),
            _failure_summary_panel(stats, backlog, workers, scans),
            _triage_panel(limit=8) or _panel(Text("No recent pipeline failures.", style="success"), "Triage", border_style="success"),
            _command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, notice=notice),
        )
    if view == "activity":
        return Group(
            top,
            _activity_summary_panel(limit=24),
            _recent_activity(limit=12),
            _command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, notice=notice),
        )
    if view == "controls":
        return Group(
            top,
            _command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats),
            _command_reference(),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, notice=notice),
        )
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    triage = _triage_panel() if backlog["failed_downloads"] else None
    grid.add_row(_overview_queue_panel(backlog), triage or _recent_activity(limit=3))
    return Group(
        top,
        _summary_panel(stats, backlog, workers, scans, freshness=freshness),
        grid,
        _recent_activity(limit=4) if triage else Group(),
        _command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=3),
        _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, notice=notice),
    )


def _render_compact(stats, backlog, workers, scans, freshness=None, view="overview", interactive=False, height=None, notice=None, width=None):
    view = _normalize_view(view)
    state, state_style, _reason = _health_state(backlog, workers, scans, stats, freshness=freshness)
    tight = bool(height and height <= 20)
    if view == "queue":
        parts = [
            _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width),
            _queue_preview(download_limit=5, extraction_limit=4, raw_limit=2),
        ]
        if not tight:
            parts.append(_command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=2))
        parts.append(_footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, compact=True, notice=notice))
        return Group(*parts)
    if view == "failures":
        parts = [
            _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width),
            _triage_panel(limit=5) or _panel(Text("No recent pipeline failures.", style="success"), "Triage", border_style="success"),
        ]
        if not tight:
            parts.append(_command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=2))
        parts.append(_footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, compact=True, notice=notice))
        return Group(*parts)
    if view == "activity":
        return Group(
            _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width),
            _recent_activity(limit=7),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, compact=True, notice=notice),
        )
    if view == "controls":
        parts = [
            _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width),
        ]
        if not tight:
            parts.append(_command_examples(view, backlog=backlog, workers=workers, scans=scans, stats=stats, limit=3))
        parts.extend([
            _compact_command_reference(),
            _footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, compact=True, notice=notice),
        ])
        return Group(*parts)
    parts = [
        _top_chrome(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, width=width),
        _compact_summary_panel(stats, backlog, workers, scans, freshness=freshness, width=width),
    ]
    has_attention = _pending_total(backlog) or _failure_total(stats, backlog, workers) or scans.get("infected", 0)
    if has_attention or not tight:
        parts.append(_attention_panel(backlog, compact=True))
    parts.append(_footer(backlog, workers, scans, stats, state_style, view=view, interactive=interactive, compact=True, notice=notice))
    return Group(*parts)


def render_tui(logo_lines, height=None, view="overview", interactive=False, notice=None, width=None):
    stats, backlog, workers, scans, freshness = _collect_state()
    height = terminal_theme.console.height if height is None else height
    width = terminal_theme.console.width if width is None else width
    if height and height < COMPACT_HEIGHT:
        return _render_compact(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, height=height, notice=notice, width=width)
    return _render_full(stats, backlog, workers, scans, freshness=freshness, view=view, interactive=interactive, notice=notice, width=width)


@contextlib.contextmanager
def _raw_input(enabled=True):
    if not enabled or not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_key():
    if not sys.stdin.isatty():
        return None
    readable, _writable, _errors = select.select([sys.stdin], [], [], 0)
    if not readable:
        return None
    return sys.stdin.read(1)


def _tmux_agent_target():
    if not os.environ.get("TMUX"):
        return ""
    return os.environ.get("ALGE_TUI_AGENT_TARGET") or "{down-of}"


def _send_command_to_agent(command):
    target = _tmux_agent_target()
    if not target:
        return False, "not running inside tmux"
    command = str(command or "").strip()
    if not command:
        return False, "no command selected"
    send = subprocess.run(
        ["tmux", "send-keys", "-t", target, "-l", command],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if send.returncode:
        message = send.stderr.strip() or "tmux send-keys failed"
        return False, message
    subprocess.run(
        ["tmux", "select-pane", "-t", target],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True, f"pasted {command}; review and press Enter"


def _send_primary_action(view):
    stats, backlog, workers, scans, _freshness_value = _collect_state()
    _label, command, _hint = _primary_action(view, backlog=backlog, workers=workers, scans=scans, stats=stats)
    return _send_command_to_agent(command)


def run_tui(logo_lines, watch=False, interval=2.0, view="overview"):
    view = _normalize_view(view)
    if not watch:
        terminal_theme.console.print(render_tui(logo_lines, view=view))
        return

    notice = None
    notice_until = 0
    with _raw_input(enabled=True) as interactive:
        with Live(
            render_tui(logo_lines, view=view, interactive=interactive),
            console=terminal_theme.console,
            refresh_per_second=max(1, int(1 / max(interval, 0.25))),
            screen=True,
        ) as live:
            while True:
                if notice and time.monotonic() >= notice_until:
                    notice = None
                key = _read_key() if interactive else None
                if key:
                    lowered = key.lower()
                    if lowered == "q":
                        break
                    if lowered in {"\r", "\n"}:
                        ok, message = _send_primary_action(view)
                        notice = ("success", message) if ok else ("danger", f"tmux: {message}")
                        notice_until = time.monotonic() + 4
                    else:
                        view = _view_for_key(lowered, view)
                live.update(render_tui(logo_lines, view=view, interactive=interactive, notice=notice))
                time.sleep(interval)
