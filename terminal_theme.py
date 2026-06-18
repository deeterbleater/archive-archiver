from io import StringIO

from rich.console import Console
from rich.errors import MarkupError
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


POND_SCUM = "#6f8f1f"
LOGO_GRADIENT = ("#c8ff7a", "#9ccd3e", "#6f8f1f", "#486b16", "#c9d1c8")
READLINE_START_IGNORE = "\001"
READLINE_END_IGNORE = "\002"

THEME = Theme({
    "highlight": POND_SCUM,
    "pond": POND_SCUM,
    "success": "bold #8fbc2f",
    "warning": "bold #d6a84f",
    "danger": "bold #d95757",
    "muted": "#7f8a78",
    "agent": "bold #9fbd4a",
    "tool": "#62d9ff",
    "label": "bold #c4d0ba",
})

console = Console(theme=THEME, highlight=False)


def print_markup(text="", **kwargs):
    try:
        console.print(text, markup=True, **kwargs)
    except MarkupError:
        console.print(escape(str(text)), markup=True, **kwargs)


def print_panel(text, title=None, style="pond", border_style="pond"):
    try:
        renderable = Text.from_markup(str(text), style=style)
        console.print(Panel(renderable, title=title, border_style=border_style))
    except MarkupError:
        console.print(Panel(escape(str(text)), title=title, style=style, border_style=border_style))


def print_rule(title="", style="pond"):
    console.print(Rule(title, style=style))


def print_logo(lines):
    for line, color in zip(lines, LOGO_GRADIENT):
        console.print(Text(str(line).rstrip(), style=color))


def pip(status="pending"):
    style = {
        "pending": "warning",
        "success": "success",
        "failed": "danger",
        "skipped": "muted",
        "info": "tool",
    }.get(status, "warning")
    return f"[{style}]•[/{style}]"


def print_pip(status, message):
    print_markup(f"{pip(status)} {message}")


def _format_tool_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return repr(value)


def tool_call_renderable(name, arguments=None):
    arguments = arguments or {}
    table = Table.grid(expand=False)
    table.add_column(justify="right", style="label", no_wrap=True)
    table.add_column(style="tool", no_wrap=True)
    table.add_column(style="muted")
    table.add_row(Text("•", style="tool"), Text(str(name), style="highlight"), Text("tool call", style="muted"))
    for key in sorted(arguments):
        table.add_row("", Text(str(key), style="label"), Text(_format_tool_value(arguments[key])))
    return table


def print_tool_call(name, arguments=None):
    console.print(tool_call_renderable(name, arguments))


def make_table(*columns, title=None):
    table = Table(title=title, border_style="pond", header_style="label", show_lines=False)
    for column in columns:
        table.add_column(column)
    return table


def render_markup(text):
    capture = StringIO()
    test_console = Console(
        file=capture,
        theme=THEME,
        force_terminal=True,
        color_system="truecolor",
        width=100,
        highlight=False,
    )
    try:
        test_console.print(text, markup=True, end="")
    except MarkupError:
        test_console.print(escape(str(text)), markup=True, end="")
    return capture.getvalue()


def prompt():
    return (
        f"{READLINE_START_IGNORE}\033[38;2;111;143;31m{READLINE_END_IGNORE}"
        "alge>"
        f"{READLINE_START_IGNORE}\033[0m{READLINE_END_IGNORE} "
    )


def visible_prompt(prompt_text=None):
    text = prompt_text if prompt_text is not None else prompt()
    return text.replace(READLINE_START_IGNORE, "").replace(READLINE_END_IGNORE, "")
