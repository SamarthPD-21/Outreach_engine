"""Terminal output: one console, one theme, a few shared renderers."""

from rich import box
from rich.console import Console
from rich.table import Table
from rich.theme import Theme

THEME = Theme({
    "ok": "green",
    "warn": "yellow",
    "bad": "bold red",
    "muted": "grey58",
    "accent": "cyan",
    "stage": "bold cyan",
    "cmd": "bold magenta",
    "arm": "bold yellow",
})

console = Console(theme=THEME, highlight=False)


def ok(msg: str) -> None:
    console.print(f"[ok]✓[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"[warn]![/] {msg}")


def fail(msg: str) -> None:
    console.print(f"[bad]✗[/] {msg}")


def info(msg: str) -> None:
    console.print(f"[muted]·[/] {msg}")


def cmd(text: str) -> str:
    """Format a command the user can copy."""
    return f"[cmd]{text}[/]"


def pct(rate, ci=None) -> str:
    if rate is None:
        return "[muted]–[/]"
    text = f"{rate:.0%}"
    return text + (f" [muted]({ci[0]:.0%}–{ci[1]:.0%})[/]" if ci else "")


def table(*columns: str, title: str | None = None, **kwargs) -> Table:
    """A borderless table with a header rule. Numeric columns are right-aligned by name."""
    t = Table(box=box.SIMPLE_HEAD, title=title, title_justify="left", title_style="bold",
              header_style="bold", pad_edge=False, **kwargs)
    for col in columns:
        right = col.lower() in {"sent", "matured", "bounced", "replied", "positive", "waiting",
                                "evidence", "hours", "id", "#", "open leads", "active"}
        t.add_column(col, justify="right" if right else "left")
    return t


STATUS_STYLE = {
    "new": "muted", "manual": "warn", "ready": "accent", "drafted": "accent",
    "approved": "ok", "sent": "default", "replied": "ok", "bounced": "bad", "closed": "muted",
    "idea": "muted", "building": "warn", "shipped": "ok", "dropped": "muted",
    "running": "warn", "concluded": "ok",
}


def status(value: str) -> str:
    return f"[{STATUS_STYLE.get(value, 'default')}]{value}[/]"
