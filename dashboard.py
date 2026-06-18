import time

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

import db
import goals
import terminal_theme


def _status_style(count):
    return "success" if not count else "danger"


def _inline_status(parts):
    line = Text()
    for index, (label, value, style) in enumerate(parts):
        if index:
            line.append("  ")
        line.append(f"{label} ", style="muted")
        line.append(str(value), style=style)
    return line


def render_dashboard(logo_lines):
    stats = db.get_stats()
    backlog = db.get_backlog_counts()
    active_goal = goals.GoalStore().active()

    downloads = stats.get("downloads_by_status", {})
    extractions = stats.get("extractions_by_status", {})
    scans = stats.get("scans_by_status", {})
    raw_archives = stats.get("raw_archives_by_status", {})

    logo = Text()
    gradient_lines = list(zip(logo_lines, terminal_theme.LOGO_GRADIENT))
    for index, (line, color) in enumerate(gradient_lines):
        logo.append(str(line).rstrip(), style=color)
        if index < len(gradient_lines) - 1:
            logo.append("\n")

    goal_value = Text()
    if active_goal:
        goal_value.append(active_goal.get("status", "active"), style="highlight")
        goal_value.append(" ")
        goal_value.append(active_goal.get("objective", "")[:52], style="label")
        if active_goal.get("estimated_completion_at"):
            goal_value.append(f" eta {active_goal['estimated_completion_at']}", style="warning")
    else:
        goal_value.append("no active goal", style="muted")

    status = Group(
        _inline_status([
            ("works", stats["total_works"], "highlight"),
            ("files", stats["total_files"], "highlight"),
            ("pend", backlog["pending_downloads"], "warning" if backlog["pending_downloads"] else "success"),
        ]),
        _inline_status([
            ("dl", downloads.get("downloading", 0), "tool"),
            ("txt", extractions.get("processing", 0), "tool"),
            ("done", extractions.get("processed", 0), "success"),
        ]),
        _inline_status([
            ("fail dl", downloads.get("failed", 0), _status_style(downloads.get("failed", 0))),
            ("fail txt", extractions.get("failed", 0), _status_style(extractions.get("failed", 0))),
            ("bad", scans.get("infected", 0), _status_style(scans.get("infected", 0))),
        ]),
        _inline_status([
            ("raw", raw_archives.get("local", 0), "warning" if raw_archives.get("local", 0) else "success"),
            ("s3", raw_archives.get("archived", 0), "success"),
            ("clean", scans.get("clean", 0), "success"),
        ]),
        Group(Text.assemble(("goal ", "muted"), goal_value)),
    )

    layout = Table.grid(expand=True)
    layout.add_column(justify="left", no_wrap=True)
    layout.add_column(justify="center", no_wrap=True)
    layout.add_column(ratio=1)
    layout.add_row(Align.left(logo), Text(" │ \n │ \n │ \n │ \n │ ", style="pond"), status)
    return layout


def run_dashboard(logo_lines, watch=False, interval=2.0):
    if not watch:
        terminal_theme.console.print(render_dashboard(logo_lines))
        return

    with Live(
        render_dashboard(logo_lines),
        console=terminal_theme.console,
        refresh_per_second=max(1, int(1 / max(interval, 0.25))),
        screen=False,
    ) as live:
        while True:
            live.update(render_dashboard(logo_lines))
            time.sleep(interval)
