#!/usr/bin/env python3
"""Outreach pipeline: the entry point.

    discover → research → draft → review → send → sync

Each stage reads what the previous one left in outreach.db, so they can run one
at a time or all together:

    python pipeline.py                 dashboard: what's waiting at each stage
    python pipeline.py run             every stage in order
    python pipeline.py run --from draft --to send
    python pipeline.py draft --limit 3 one stage on its own
    python pipeline.py doctor          check keys, profile and setup

Supporting commands (lead, company, exp, pow, report) live in outreach/commands.py.
"""

import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Callable, Optional

import typer
from rich.columns import Columns
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from outreach import commands, config, db, drafting, experiments, mail, proof, research, review, ui
from outreach.ui import console

# ---------------------------------------------------------------------------
# Stage context and results
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """What every stage gets. The model and profile load only if a stage needs them."""
    conn: object
    limit: int = 5
    live: bool = False
    yes: bool = False
    interactive: bool = True
    anytime: bool = False
    min_stipend: int = 30000
    classify: bool = False
    _model: object = None
    _profile: object = None

    @property
    def model(self):
        if self._model is None:
            from outreach.llm import OpenAIModel
            self._model = OpenAIModel()
        return self._model

    @property
    def profile(self):
        if self._profile is None:
            self._profile = config.load_profile()
        return self._profile


@dataclass
class Result:
    status: str          # done | skipped | failed
    summary: str
    seconds: float = 0.0


@dataclass
class Stage:
    key: str
    title: str
    purpose: str
    waiting: Callable      # conn -> int: how much work is queued for this stage
    note: Callable         # conn -> str: extra context for the dashboard
    run: Callable          # Context -> Result
    needs: list[str] = field(default_factory=list)  # env vars the stage requires


def _count(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _last_event(conn, type_: str) -> str:
    row = conn.execute("SELECT MAX(at) FROM events WHERE type = ?", (type_,)).fetchone()[0]
    if not row:
        return "never run"
    days = (datetime.now(timezone.utc) - datetime.fromisoformat(row)).days
    return "last run today" if days == 0 else f"last run {days}d ago"


# ---------------------------------------------------------------------------
# 1. discover
# ---------------------------------------------------------------------------

def discover_run(ctx: Context) -> Result:
    from outreach import discover

    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                  TimeElapsedColumn(), console=console, transient=True) as bar:
        task = bar.add_task("Scanning Internshala feeds", total=None)

        def on_feed(i, total, name):
            bar.update(task, total=total, completed=i, description=f"Scanning {name}")

        cards = discover.scan_feeds(ctx.conn, ctx.min_stipend, on_feed)
        cards = cards[:ctx.limit]
        task = bar.add_task("Checking listings for contacts", total=len(cards))
        added = []
        for card in cards:
            bar.update(task, description=f"Checking {card['company']}")
            added.append(discover.add_listing(ctx.conn, card))
            bar.advance(task)
    if not added:
        db.log(ctx.conn, "discovered", None, found=0)
        ctx.conn.commit()
        return Result("done", f"No new listings at ₹{ctx.min_stipend:,}+/month.")
    t = ui.table("id", "Company", "Role", "Stipend", "Contact")
    for a in added:
        t.add_row(str(a["lead_id"]), a["company"], a["role"], a["stipend"],
                  a["email"] or "[warn]none found[/]")
    console.print(t)
    with_email = sum(1 for a in added if a["email"])
    return Result("done", f"{len(added)} new lead(s), {with_email} with a published address.")


def discover_note(conn) -> str:
    return _last_event(conn, "discovered")


# ---------------------------------------------------------------------------
# 2. research
# ---------------------------------------------------------------------------

NEEDS_RESEARCH = """
    SELECT c.* FROM companies c
    WHERE EXISTS (SELECT 1 FROM leads l WHERE l.company_id = c.id
                  AND l.status IN ('new', 'ready', 'manual'))
      AND (SELECT COUNT(*) FROM evidence e WHERE e.company_id = c.id) < 2
"""


def research_waiting(conn) -> int:
    return _count(conn, f"SELECT COUNT(*) FROM ({NEEDS_RESEARCH}) WHERE website IS NOT NULL")


def research_note(conn) -> str:
    missing = _count(conn, f"SELECT COUNT(*) FROM ({NEEDS_RESEARCH}) WHERE website IS NULL")
    return f"[warn]{missing} have no website[/]" if missing else ""


def research_run(ctx: Context) -> Result:
    rows = ctx.conn.execute(NEEDS_RESEARCH + " AND c.website IS NOT NULL").fetchall()
    missing = ctx.conn.execute(NEEDS_RESEARCH + " AND c.website IS NULL").fetchall()
    for c in missing:
        ui.warn(f"{c['name']} has no website: {ui.cmd(f'company set {c['id']} --website URL')}")
    if not rows:
        if missing:
            return Result("skipped", f"{len(missing)} compan{'y needs' if len(missing) == 1 else 'ies need'} "
                                     "a website before they can be researched.")
        return Result("skipped", "Every open lead's company already has evidence.")
    saved = 0
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                  console=console, transient=True) as bar:
        task = bar.add_task("Researching", total=len(rows))
        for c in rows:
            bar.update(task, description=f"Researching {c['name']}")
            counts = research.research_company(ctx.conn, c["id"])
            ctx.conn.commit()
            total = len(db.evidence_for(ctx.conn, c["id"]))
            saved += counts["site"] + counts["github"]
            (ui.ok if total >= 2 else ui.warn)(
                f"{c['name']}: {counts['site']} page(s), {counts['github']} GitHub item(s)"
                + ("" if total >= 2 else " — not enough to draft from"))
            bar.advance(task)
    return Result("done", f"{saved} evidence item(s) saved across {len(rows)} companies.")


# ---------------------------------------------------------------------------
# 3. draft
# ---------------------------------------------------------------------------

def draftable(conn) -> list[int]:
    return [r["id"] for r in experiments.prioritized_ready_leads(conn)
            if len(db.evidence_for(conn, db.lead_view(conn, r["id"])["company_id"])) >= 2]


def draft_waiting(conn) -> int:
    return len(draftable(conn))


def draft_note(conn) -> str:
    blocked = _count(conn, "SELECT COUNT(*) FROM leads WHERE status = 'ready'") - draft_waiting(conn)
    exp = experiments.running(conn)
    parts = [f"[warn]{blocked} waiting on research[/]"] if blocked else []
    if exp:
        parts.append(f"testing [arm]{exp['name']}[/]")
    return ", ".join(parts)


def draft_run(ctx: Context) -> Result:
    ids = draftable(ctx.conn)[:ctx.limit]
    if not ids:
        return Result("skipped", "No ready leads with enough evidence.")
    made = blocked = 0
    for lead_id in ids:
        lead = db.lead_view(ctx.conn, lead_id)
        with console.status(f"Drafting for {lead['company']}…"):
            try:
                r = drafting.draft_lead(ctx.conn, lead_id, ctx.model, ctx.profile)
            except ValueError as exc:
                ui.warn(f"Lead #{lead_id}: {exc}")
                continue
        ctx.conn.commit()
        made += 1
        tags = [f"[arm]{k}={v}[/]" for k, v in r["arms"].items()]
        if r["project"]:
            tags.append("[ok]proof of work[/]")
        if drafting.blocking(r["issues"]):
            blocked += 1
            ui.warn(f"Draft #{r['draft_id']} for {lead['company']} needs fixes  {' '.join(tags)}")
        else:
            ui.ok(f"Draft #{r['draft_id']} for {lead['company']}  {' '.join(tags)}")
    return Result("done", f"{made} draft(s), {blocked} need fixes before approval.")


# ---------------------------------------------------------------------------
# 4. review
# ---------------------------------------------------------------------------

def review_waiting(conn) -> int:
    return len(drafting.pending_drafts(conn))


def review_note(conn) -> str:
    n = sum(1 for d in drafting.pending_drafts(conn) if '"blocking"' in d["lint_json"])
    return f"[warn]{n} need fixes[/]" if n else ""


def review_run(ctx: Context) -> Result:
    if not review_waiting(ctx.conn):
        return Result("skipped", "Nothing to review.")
    if not ctx.interactive:
        return Result("skipped", f"{review_waiting(ctx.conn)} draft(s) wait for you: "
                                 f"python pipeline.py review")
    c = review.run(ctx.conn, ctx.profile)
    return Result("done", f"{c['approved']} approved, {c['rejected']} rejected, "
                          f"{c['skipped']} left for later.")


# ---------------------------------------------------------------------------
# 5. send
# ---------------------------------------------------------------------------

def send_waiting(conn) -> int:
    return sum(1 for _, _, skip in mail.sendable(conn) if not skip)


def send_note(conn) -> str:
    window = "[ok]window open[/]" if mail.in_send_window() else "[muted]window closed[/]"
    return f"{mail.sent_today(conn)}/{config.DAILY_SEND_CAP} today, {window}"


def send_run(ctx: Context) -> Result:
    queue = [(d, lead) for d, lead, skip in mail.sendable(ctx.conn) if not skip]
    for _, lead, skip in mail.sendable(ctx.conn):
        if skip:
            ui.info(f"{lead['company']} <{lead['email']}>: skipped, {skip}")
    if not queue:
        return Result("skipped", "No approved drafts to send.")
    t = ui.table("Company", "To", "Subject")
    for d, lead in queue:
        t.add_row(lead["company"], lead["email"], d["subject"])
    console.print(t)
    if not ctx.live:
        return Result("done", f"Preview of {len(queue)} email(s). Add --send to send them.")
    if not ctx.anytime and not mail.in_send_window():
        return Result("skipped", "Outside the send window (Tue–Thu, 9:30–11:30 IST). "
                                 "Wait, or pass --anytime.")
    if not ctx.yes and not typer.confirm(f"Send {len(queue)} email(s) now?", default=False):
        return Result("skipped", "Not sent.")
    n = mail.send_approved(ctx.conn, ctx.profile, live=True, limit=ctx.limit,
                           printer=console.print)
    ctx.conn.commit()
    return Result("done", f"Sent {n} email(s).")


# ---------------------------------------------------------------------------
# 6. sync
# ---------------------------------------------------------------------------

def sync_waiting(conn) -> int:
    return _count(conn, """SELECT COUNT(*) FROM messages m
                           LEFT JOIN outcomes o ON o.lead_id = m.lead_id
                           WHERE o.lead_id IS NULL OR o.outcome = 'auto_reply'""")


def sync_note(conn) -> str:
    row = conn.execute("SELECT MAX(at) FROM events WHERE type = 'synced'").fetchone()[0]
    if not row:
        return "[warn]never synced[/]"
    hours = (datetime.now(timezone.utc) - datetime.fromisoformat(row)).total_seconds() / 3600
    return f"synced {hours:.0f}h ago" if hours >= 1 else "synced just now"


def sync_run(ctx: Context) -> Result:
    if not _count(ctx.conn, "SELECT COUNT(*) FROM messages"):
        return Result("skipped", "Nothing has been sent yet.")
    with console.status("Checking Gmail for replies and bounces…"):
        r = mail.sync(ctx.conn, model=ctx.model if ctx.classify else None, printer=console.print)
    db.log(ctx.conn, "synced", None, **r)
    ctx.conn.commit()
    return Result("done", f"Checked {r['checked']} inbox message(s), {r['recorded']} new outcome(s).")


STAGES = [
    Stage("discover", "Discover", "Find new internship listings", lambda c: 0, discover_note, discover_run),
    Stage("research", "Research", "Save facts about each company", research_waiting, research_note, research_run),
    Stage("draft", "Draft", "Write grounded drafts", draft_waiting, draft_note, draft_run, ["OPENAI_API_KEY"]),
    Stage("review", "Review", "Approve, edit or reject", review_waiting, review_note, review_run),
    Stage("send", "Send", "Email approved drafts", send_waiting, send_note, send_run,
          ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]),
    Stage("sync", "Sync", "Read replies and bounces", sync_waiting, sync_note, sync_run,
          ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]),
]
BY_KEY = {s.key: s for s in STAGES}


def missing_env(stage: Stage, ctx: Context) -> list[str]:
    needs = list(stage.needs)
    if stage.key == "send" and not ctx.live:
        needs = []  # a preview never logs in
    if stage.key == "sync" and ctx.classify:
        needs.append("OPENAI_API_KEY")
    return [n for n in needs if not os.environ.get(n, "").strip()]


def execute(stage: Stage, ctx: Context, index: int | None = None) -> Result:
    label = f"{index}. " if index else ""
    console.rule(f"[stage]{label}{stage.title}[/]  [muted]{stage.purpose}[/]", align="left")
    missing = missing_env(stage, ctx)
    if missing:
        result = Result("skipped", f"Set {', '.join(missing)} in .env to run this stage.")
    else:
        start = time.monotonic()
        try:
            result = stage.run(ctx)
        except SystemExit as exc:
            result = Result("failed", str(exc.code).lstrip("❌ "))
        except Exception as exc:  # one stage failing shouldn't lose the others' work
            ctx.conn.rollback()
            result = Result("failed", f"{type(exc).__name__}: {exc}")
        result.seconds = time.monotonic() - start
    style = {"done": "ok", "skipped": "muted", "failed": "bad"}[result.status]
    console.print(f"[{style}]{result.summary}[/]\n")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="Cold outreach pipeline: discover → research → draft → review → send → sync.",
    rich_markup_mode="rich", no_args_is_help=False, add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

LimitOpt = Annotated[int, typer.Option("--limit", "-n", help="Most items to handle.")]


def make_ctx(**kwargs) -> Context:
    return Context(conn=db.shared(), **kwargs)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Run with no command to see the dashboard."""
    if ctx.invoked_subcommand is None:
        status()


@app.command(rich_help_panel="Pipeline")
def status():
    """Dashboard: what's waiting at each stage and what to do next."""
    conn = db.shared()
    sent = _count(conn, "SELECT COUNT(*) FROM messages WHERE kind = 'initial'")
    replied = _count(conn, "SELECT COUNT(*) FROM outcomes WHERE outcome = 'replied'")
    positive = _count(conn, "SELECT COUNT(*) FROM outcomes WHERE sentiment = 'positive'")
    leads = _count(conn, "SELECT COUNT(*) FROM leads")
    console.print(Panel(
        f"[bold]{leads}[/] leads   [bold]{sent}[/] sent   [bold]{replied}[/] replied   "
        f"[bold]{positive}[/] positive"
        + (f"   reply rate [bold]{replied / sent:.0%}[/]" if sent else ""),
        title="[bold]Outreach pipeline[/]", title_align="left", border_style="accent"))

    t = ui.table("#", "Stage", "Waiting", "Notes", "Run")
    for i, s in enumerate(STAGES, start=1):
        n = s.waiting(conn)
        t.add_row(str(i), f"[bold]{s.title}[/]",
                  f"[bold]{n}[/]" if n else "[muted]0[/]", s.note(conn),
                  ui.cmd(f"pipeline.py {s.key}"))
    console.print(t)

    panels = []
    exp = experiments.running(conn)
    if exp:
        rows = experiments.arm_results(conn, exp["id"])
        body = "\n".join(f"[arm]{r['arm']}[/]  {r['replied']}/{r['mature']} replied  "
                         f"{ui.pct(r['rate'])}" for r in rows)
        body += f"\n[muted]{experiments.verdict(rows)['text']}[/]"
        panels.append(Panel(body, title=f"Experiment · {exp['name']}", title_align="left"))
    else:
        panels.append(Panel(f"No test running.\n{ui.cmd('pipeline.py exp presets')}",
                            title="Experiment", title_align="left"))
    b = proof.board(conn)
    by_status = {}
    for r in b["rows"]:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    panels.append(Panel(
        f"{by_status.get('building', 0)} building · {by_status.get('idea', 0)} ideas · "
        f"{by_status.get('shipped', 0)} shipped\n"
        f"This month: [bold]{b['shipped_this_month']}[/] of {b['target']}\n"
        + ui.cmd("pipeline.py pow list"),
        title="Proof of work", title_align="left"))
    console.print(Columns(panels, equal=True, expand=True))
    console.print(f"Next: {next_step(conn)}")


def next_step(conn) -> str:
    if sync_waiting(conn) and "never" in sync_note(conn):
        return f"{ui.cmd('pipeline.py sync')} to find out what happened to the emails already sent."
    for key, why in (("review", "drafts are waiting for you"), ("send", "approved emails are ready"),
                     ("draft", "leads are ready to draft"), ("research", "companies need facts")):
        if BY_KEY[key].waiting(conn):
            return f"{ui.cmd(f'pipeline.py {key}')}, {why}."
    manual = _count(conn, "SELECT COUNT(*) FROM leads WHERE status = 'manual'")
    if manual:
        return (f"{manual} lead(s) have no address. Find a person and add them with "
                f"{ui.cmd('pipeline.py lead add')}, or run {ui.cmd('pipeline.py discover')}.")
    return f"{ui.cmd('pipeline.py discover')} to find new leads."


@app.command(rich_help_panel="Pipeline")
def run(
    from_: Annotated[str, typer.Option("--from", help="First stage.")] = "discover",
    to: Annotated[str, typer.Option(help="Last stage.")] = "sync",
    only: Annotated[Optional[list[str]], typer.Option(help="Run just these stages.")] = None,
    skip: Annotated[Optional[list[str]], typer.Option(help="Leave out these stages.")] = None,
    limit: LimitOpt = 5,
    send: Annotated[bool, typer.Option("--send", help="Really send (otherwise preview).")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No prompts; review is left for later.")] = False,
    anytime: Annotated[bool, typer.Option("--anytime", help="Ignore the Tue–Thu morning send window.")] = False,
    classify: Annotated[bool, typer.Option("--classify", help="Label replies with the model during sync.")] = False,
    keep_going: Annotated[bool, typer.Option("--keep-going", help="Continue after a stage fails.")] = False,
):
    """Run the stages in order. Stages with nothing to do are skipped."""
    keys = [s.key for s in STAGES]
    for name in [from_, to, *(only or []), *(skip or [])]:
        if name not in keys:
            raise SystemExit(f"Unknown stage {name!r}. Stages: {', '.join(keys)}")
    selected = only or keys[keys.index(from_):keys.index(to) + 1]
    selected = [k for k in keys if k in selected and k not in (skip or [])]
    ctx = make_ctx(limit=limit, live=send, yes=yes, anytime=anytime, classify=classify,
                   interactive=not yes and sys.stdin.isatty())
    results = []
    for key in selected:
        stage = BY_KEY[key]
        result = execute(stage, ctx, keys.index(key) + 1)
        results.append((stage, result))
        if result.status == "failed" and not keep_going:
            ui.fail(f"Stopped at {stage.title}. Fix it and resume with "
                    + ui.cmd(f"pipeline.py run --from {key}"))
            break
    t = ui.table("Stage", "Result", "Time", "Summary", title="Run summary")
    for stage, r in results:
        style = {"done": "ok", "skipped": "muted", "failed": "bad"}[r.status]
        t.add_row(stage.title, f"[{style}]{r.status}[/]", f"{r.seconds:.1f}s", r.summary)
    console.print(t)
    if any(r.status == "failed" for _, r in results):
        raise typer.Exit(1)


def _single(key: str, **kwargs) -> None:
    result = execute(BY_KEY[key], make_ctx(**kwargs))
    if result.status == "failed":
        raise typer.Exit(1)


@app.command(rich_help_panel="Stages")
def discover(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Most new listings to add.")] = 10,
    min_stipend: Annotated[int, typer.Option(help="Minimum monthly stipend in ₹.")] = 30000,
):
    """1. Find engineering internships on Internshala and add them as leads."""
    _single("discover", limit=limit, min_stipend=min_stipend)


@app.command("research", rich_help_panel="Stages")
def research_cmd(
    company: Annotated[Optional[str], typer.Argument(help="One company (name or id). Default: all that need it.")] = None,
    job_url: Annotated[str, typer.Option(help="Job posting to add as evidence.")] = "",
    github: Annotated[bool, typer.Option("--github/--no-github", help="Also fetch public GitHub repos and issues.")] = True,
):
    """2. Save facts about companies as evidence, with their sources."""
    if company is None:
        return _single("research")
    conn = db.shared()
    c = db.find_company(conn, company)
    if job_url:
        research.add_job_post(conn, c["id"], job_url)
    if not c["website"] and not job_url:
        raise SystemExit(f"{c['name']} has no website. Set it: company set {c['id']} --website URL")
    with console.status(f"Researching {c['name']}…"):
        counts = research.research_company(conn, c["id"], include_github=github) \
            if c["website"] else {"site": 0, "github": 0}
    conn.commit()
    ui.ok(f"{c['name']}: {counts['site']} page(s), {counts['github']} GitHub item(s), "
          f"{len(db.evidence_for(conn, c['id']))} evidence items in total")


@app.command(rich_help_panel="Stages")
def draft(
    lead_ids: Annotated[Optional[list[int]], typer.Argument(help="Specific leads. Default: best ready leads.")] = None,
    limit: LimitOpt = 5,
):
    """3. Write drafts that only use saved evidence."""
    if not lead_ids:
        return _single("draft", limit=limit)
    ctx = make_ctx()
    for lead_id in lead_ids:
        try:
            r = drafting.draft_lead(ctx.conn, lead_id, ctx.model, ctx.profile)
        except ValueError as exc:
            ui.warn(f"Lead #{lead_id}: {exc}")
            continue
        ctx.conn.commit()
        (ui.warn if drafting.blocking(r["issues"]) else ui.ok)(f"Draft #{r['draft_id']} for lead #{lead_id}")


@app.command("review", rich_help_panel="Stages")
def review_cmd():
    """4. Read each draft beside its evidence; approve, edit or reject."""
    _single("review")


@app.command("send", rich_help_panel="Stages")
def send_cmd(
    send: Annotated[bool, typer.Option("--send", help="Really send (otherwise preview).")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    anytime: Annotated[bool, typer.Option("--anytime", help="Ignore the Tue–Thu morning send window.")] = False,
    limit: LimitOpt = config.DAILY_SEND_CAP,
):
    """5. Email approved drafts. Preview unless --send."""
    _single("send", live=send, yes=yes, anytime=anytime, limit=limit)


@app.command("sync", rich_help_panel="Stages")
def sync_cmd(classify: Annotated[bool, typer.Option("--classify", help="Label replies with the model.")] = False):
    """6. Read replies, bounces and auto-replies from Gmail."""
    _single("sync", classify=classify)


@app.command(rich_help_panel="Setup")
def doctor():
    """Check keys, profile, resume, database and old hardcoded secrets."""
    checks = []

    def check(name, passed, fix=""):
        checks.append((name, passed, fix))

    for var in ("OPENAI_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        check(f"{var} in .env", bool(os.environ.get(var, "").strip()), f"add {var}=... to .env")
    check("GITHUB_TOKEN (optional)", bool(os.environ.get("GITHUB_TOKEN")),
          "optional: raises GitHub's limit from 60 to 5,000 requests an hour")
    try:
        profile = config.load_profile()
        check("profile.toml loads", True)
        check("resume file exists", profile.resume_path.exists(), f"put your PDF at {profile.resume_path}")
    except (SystemExit, Exception) as exc:
        check("profile.toml loads", False, str(exc))
    try:
        conn = db.shared()
        imported = _count(conn, "SELECT COUNT(*) FROM messages WHERE legacy = 1")
        check("database ready", True)
        check("old sends imported", imported > 0 or not (config.LEGACY_DIR / "sent_log.csv").exists(),
              "run: pipeline.py init")
    except Exception as exc:
        check("database ready", False, str(exc))
    key_pattern = re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")
    leaked = [p.name for p in config.ROOT.glob("*.py")
              if key_pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]
    check("no API keys in .py files", not leaked, f"remove the key from {', '.join(leaked)}")
    check("git ignores .env", (config.ROOT / ".gitignore").exists()
          and ".env" in (config.ROOT / ".gitignore").read_text(), "add .env to .gitignore")

    t = ui.table("Check", "", "Fix")
    for name, passed, fix in checks:
        optional = "optional" in name
        mark = "[ok]✓[/]" if passed else ("[muted]–[/]" if optional else "[bad]✗[/]")
        t.add_row(name, mark, "" if passed else f"[muted]{fix}[/]")
    console.print(t)
    if any(not p and "optional" not in n for n, p, _ in checks):
        raise typer.Exit(1)


# Registered after the stage commands so the help lists the pipeline first.
commands.register(app)


def main() -> None:
    try:
        app()
    except SystemExit as exc:
        # Modules raise SystemExit("message") for user errors; show it cleanly.
        if isinstance(exc.code, str):
            ui.fail(exc.code.lstrip("❌ "))
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
