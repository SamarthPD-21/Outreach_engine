"""Supporting command groups: leads, companies, experiments, proof of work, reports.

The pipeline stages themselves (discover → sync) and `run` live in pipeline.py,
which mounts these groups onto the same app.
"""

import csv
import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.panel import Panel

from . import config, db, experiments, legacy, mail, proof, research, ui
from .ui import console


def connect():
    return db.shared()


def model():
    from .llm import OpenAIModel
    return OpenAIModel()


# ---------------------------------------------------------------------------
# lead
# ---------------------------------------------------------------------------

lead_app = typer.Typer(help="Add, list and inspect leads.", no_args_is_help=True)


@lead_app.command("add")
def lead_add(
    company: str,
    role: str,
    email: Annotated[str, typer.Option(help="Contact's address.")] = "",
    name: Annotated[str, typer.Option(help="Contact's name.")] = "",
    title: Annotated[str, typer.Option(help="Contact's job title.")] = "",
    website: Annotated[str, typer.Option(help="Company website.")] = "",
    job_url: Annotated[str, typer.Option(help="Job posting to save as evidence.")] = "",
    source: Annotated[str, typer.Option(help="Where you found it.")] = "manual",
):
    """Add a lead you found yourself."""
    conn = connect()
    lead_id = db.add_lead(conn, company, role, email, name, title, source,
                          research.normalize_url(website) if website else None)
    lead = db.lead_view(conn, lead_id)
    if job_url:
        research.add_job_post(conn, lead["company_id"], job_url)
    conn.commit()
    ui.ok(f"Lead #{lead_id}: {lead['company']}, {lead['role']} ({ui.status(lead['status'])})")
    if lead["status"] == "manual":
        ui.warn("No usable address, so it's in the manual list. Add one with "
                + ui.cmd(f"lead add \"{company}\" \"{role}\" --email ..."))
    elif lead["email_status"] == "generic":
        ui.warn(f"{lead['email']} is a shared inbox. A named person replies far more often.")


@lead_app.command("import")
def lead_import(path: Path, source: Annotated[str, typer.Option()] = "import"):
    """Import leads from a CSV with Company, Role and optionally Email, Name, Title, Website."""
    conn = connect()
    added = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("Company") or not row.get("Role"):
                continue
            db.add_lead(conn, row["Company"], row["Role"], row.get("Email", ""),
                        row.get("Name", ""), row.get("Title", ""), source,
                        research.normalize_url(row["Website"]) if row.get("Website") else None)
            added += 1
    conn.commit()
    ui.ok(f"Imported {added} row(s) from {path}.")


@lead_app.command("list")
def lead_list(
    status: Annotated[Optional[str], typer.Option(help="Only this status.")] = None,
    company: Annotated[Optional[str], typer.Option(help="Only this company.")] = None,
):
    """List leads, grouped by status."""
    conn = connect()
    q = """SELECT l.id, c.name, c.tier, l.role, ct.email, l.source, l.status
           FROM leads l JOIN companies c ON c.id = l.company_id
           LEFT JOIN contacts ct ON ct.id = l.contact_id WHERE 1=1"""
    params = []
    if status:
        q += " AND l.status = ?"
        params.append(status)
    if company:
        q += " AND c.name = ? COLLATE NOCASE"
        params.append(company)
    rows = conn.execute(q + " ORDER BY l.status, c.tier, l.id", params).fetchall()
    t = ui.table("id", "Company", "Tier", "Role", "Email", "Source", "Status")
    for r in rows:
        t.add_row(str(r["id"]), r["name"], r["tier"], r["role"], r["email"] or "[muted]–[/]",
                  r["source"], ui.status(r["status"]))
    console.print(t)
    console.print(f"[muted]{len(rows)} lead(s)[/]")


@lead_app.command("show")
def lead_show(lead_id: int):
    """Everything about one lead: contact, drafts, sends, outcome and history."""
    conn = connect()
    lead = db.lead_view(conn, lead_id)
    console.print(Panel(
        f"[bold]{lead['company']}[/] · {lead['role']}\n"
        f"Contact: {lead['contact_name'] or '–'} <{lead['email'] or 'no address'}> "
        f"[muted]({lead['email_status'] or 'none'})[/]\n"
        f"Status: {ui.status(lead['status'])}   Tier: {lead['tier']}   Source: {lead['source']}",
        title=f"Lead #{lead_id}", title_align="left", border_style="accent"))
    for d in conn.execute("SELECT * FROM drafts WHERE lead_id = ? ORDER BY id", (lead_id,)):
        console.print(f"Draft #{d['id']} [muted]{d['created_at'][:10]}[/] {ui.status(d['decision'])}: "
                      f"{d['subject']}")
    for m in conn.execute("SELECT * FROM messages WHERE lead_id = ?", (lead_id,)):
        console.print(f"Sent {m['sent_at'][:16].replace('T', ' ')} UTC to {m['to_email']}"
                      + (" [muted](old script)[/]" if m["legacy"] else ""))
    o = conn.execute("SELECT * FROM outcomes WHERE lead_id = ?", (lead_id,)).fetchone()
    if o:
        console.print(f"Outcome: {ui.status(o['outcome'])}"
                      + (f" ({o['sentiment']})" if o["sentiment"] else "") + f" [muted]via {o['source']}[/]")
    t = ui.table("When", "Event", "Details", title="History")
    for e in conn.execute("SELECT * FROM events WHERE lead_id = ? ORDER BY id", (lead_id,)):
        t.add_row(e["at"][:16].replace("T", " "), e["type"], (e["payload"] or "")[:80])
    console.print(t)


# ---------------------------------------------------------------------------
# company
# ---------------------------------------------------------------------------

company_app = typer.Typer(help="Inspect and edit companies.", no_args_is_help=True)


@company_app.command("show")
def company_show(company: Annotated[str, typer.Argument(help="Name or id.")]):
    """A company's details and saved evidence."""
    conn = connect()
    c = db.find_company(conn, company)
    console.print(f"[bold]#{c['id']} {c['name']}[/]  tier {c['tier']}  "
                  f"website {c['website'] or '–'}  github {c['github_org'] or '–'}")
    t = ui.table("id", "Kind", "Source", "Snippet")
    for e in db.evidence_for(conn, c["id"]):
        t.add_row(f"E{e['id']}", e["kind"], e["url"], e["snippet"][:90] + "…")
    console.print(t)


@company_app.command("set")
def company_set(
    company: Annotated[str, typer.Argument(help="Name or id.")],
    website: Annotated[Optional[str], typer.Option()] = None,
    github: Annotated[Optional[str], typer.Option(help="GitHub org or user.")] = None,
    tier: Annotated[Optional[str], typer.Option(help="A, B or C.")] = None,
):
    """Set a company's website, GitHub org or tier."""
    conn = connect()
    c = db.find_company(conn, company)
    if tier and tier.upper() not in ("A", "B", "C"):
        raise SystemExit("Tier must be A, B or C.")
    for column, value in (("website", research.normalize_url(website) if website else None),
                          ("github_org", github), ("tier", tier.upper() if tier else None)):
        if value:
            conn.execute(f"UPDATE companies SET {column} = ? WHERE id = ?", (value, c["id"]))
    conn.commit()
    c = db.find_company(conn, str(c["id"]))
    ui.ok(f"#{c['id']} {c['name']}: tier {c['tier']}, website {c['website'] or '–'}, "
          f"github {c['github_org'] or '–'}")


# ---------------------------------------------------------------------------
# exp (branch A)
# ---------------------------------------------------------------------------

exp_app = typer.Typer(help="Experiment loop: test one change at a time.", no_args_is_help=True)


@exp_app.command("presets")
def exp_presets():
    """Ready-made experiments."""
    for key, p in experiments.PRESETS.items():
        console.print(f"\n[bold]{key}[/] [muted]changes {p['variable']}[/]\n  {p['hypothesis']}")
        for arm, instruction in p["arms"].items():
            console.print(f"  [arm]{arm}[/]: {instruction}")
    console.print(f"\nStart one with {ui.cmd('exp new --preset subject-style')}")


@exp_app.command("new")
def exp_new(
    preset: Annotated[Optional[str], typer.Option(help="A preset from `exp presets`.")] = None,
    name: Annotated[Optional[str], typer.Option()] = None,
    variable: Annotated[Optional[str], typer.Option(help="The one thing being changed.")] = None,
    hypothesis: Annotated[str, typer.Option()] = "",
    arm: Annotated[Optional[list[str]], typer.Option(help="name=instruction, once per arm.")] = None,
):
    """Start an experiment from a preset or your own arms."""
    conn = connect()
    if preset:
        experiments.create_preset(conn, preset, name)
        name = name or preset
    else:
        if not (name and variable and arm and len(arm) >= 2):
            raise SystemExit("Use --preset, or give --name, --variable and at least two --arm.")
        bad = [a for a in arm if "=" not in a]
        if bad:
            raise SystemExit(f"Arms look like name=instruction. Got: {bad[0]!r}")
        experiments.create(conn, name, variable, dict(a.split("=", 1) for a in arm), hypothesis)
    conn.commit()
    ui.ok(f"Started [bold]{name}[/]. New drafts are split evenly between its arms.")


@exp_app.command("list")
def exp_list():
    """All experiments, and the style every draft uses now."""
    conn = connect()
    t = ui.table("Name", "Changes", "Status", "Winner", "Started")
    for r in conn.execute("""SELECT e.*, a.name AS winner FROM experiments e
                             LEFT JOIN arms a ON a.id = e.winner_arm ORDER BY e.id"""):
        t.add_row(r["name"], r["variable"], ui.status(r["status"]), r["winner"] or "",
                  r["created_at"][:10])
    console.print(t)
    defaults = conn.execute("SELECT variable, instruction FROM style_defaults").fetchall()
    if defaults:
        console.print("[bold]Style used on every draft, from past winners[/]")
        for d in defaults:
            console.print(f"  [arm]{d['variable']}[/]: {d['instruction']}")


def render_experiment(conn, exp) -> None:
    results = experiments.arm_results(conn, exp["id"])
    t = ui.table("Arm", "Sent", "Matured", "Bounced", "Replied", "Positive", "Reply rate (95% CI)",
                 title=f"{exp['name']} · changes {exp['variable']} · {exp['status']}")
    for r in results:
        t.add_row(f"[arm]{r['arm']}[/]", str(r["sent"]), str(r["mature"]), str(r["bounced"]),
                  str(r["replied"]), str(r["positive"]), ui.pct(r["rate"], r["ci"]))
    console.print(t)
    v = experiments.verdict(results)
    (ui.ok if v["leader"] else ui.info)(v["text"])
    for w in v["warnings"]:
        ui.warn(w)


@exp_app.command("status")
def exp_status(name: Annotated[Optional[str], typer.Argument()] = None):
    """Results so far, with an honest reading of what they show."""
    conn = connect()
    exp = (conn.execute("SELECT * FROM experiments WHERE name = ?", (name,)).fetchone()
           if name else experiments.running(conn))
    if exp is None:
        raise SystemExit("No running experiment. Start one: exp new --preset subject-style")
    if exp["hypothesis"]:
        console.print(f"[muted]Hypothesis:[/] {exp['hypothesis']}")
    render_experiment(conn, exp)


@exp_app.command("conclude")
def exp_conclude(
    name: str,
    winner: Annotated[Optional[str], typer.Option(help="Arm to use from now on.")] = None,
    note: Annotated[str, typer.Option()] = "",
):
    """End an experiment, keep the winner and write the lesson to lessons.md."""
    conn = connect()
    entry = experiments.conclude(conn, name, winner, note)
    conn.commit()
    console.print(entry)
    ui.ok(f"Added to {config.LESSONS_FILE.name}.")


# ---------------------------------------------------------------------------
# pow (branch B)
# ---------------------------------------------------------------------------

pow_app = typer.Typer(help="Proof of work for your top companies.", no_args_is_help=True)


@pow_app.command("candidates")
def pow_candidates():
    """Companies worth a build: Tier A first, then by evidence."""
    conn = connect()
    t = ui.table("id", "Company", "Tier", "Evidence", "GitHub", "Open leads", "Active")
    for r in proof.candidates(conn):
        t.add_row(str(r["id"]), r["name"], r["tier"], str(r["evidence"]), r["github_org"] or "",
                  str(r["open_leads"]), str(r["active"]))
    console.print(t)
    console.print(f"Mark top targets with {ui.cmd('company set ID --tier A')}, "
                  f"then {ui.cmd('pow pack ID')}.")


@pow_app.command("pack")
def pow_pack(
    company: Annotated[str, typer.Argument(help="Name or id.")],
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-fetch the site and GitHub first.")] = False,
    job_url: Annotated[str, typer.Option(help="Job posting to add as evidence.")] = "",
):
    """Research a company and generate build ideas tied to its evidence."""
    conn = connect()
    with console.status("Researching and generating ideas…"):
        result = proof.build_pack(conn, company, model(), config.load_profile(),
                                  refresh=refresh, job_url=job_url or None)
    conn.commit()
    ui.ok(f"Pack written to {result['path']}")
    for pid in result["new"]:
        p = proof.project(conn, pid)
        idea = json.loads(p["idea_json"])
        console.print(f"  [bold]#{pid}[/] {p['title']} [muted]({p['kind']}, ~{idea['hours']:g}h)[/]")
    for d in result["dropped"]:
        ui.warn(f"dropped: {d}")
    if result["new"]:
        console.print(f"Pick one: {ui.cmd(f'pow start {result['new'][0]}')}")


@pow_app.command("list")
def pow_list(all_: Annotated[bool, typer.Option("--all", help="Include dropped ideas.")] = False):
    """The build board."""
    conn = connect()
    b = proof.board(conn)
    t = ui.table("id", "Company", "Status", "Kind", "Title", "Hours")
    for r in b["rows"]:
        if r["status"] == "dropped" and not all_:
            continue
        hours = r["hours_spent"] if r["hours_spent"] is not None else json.loads(r["idea_json"]).get("hours")
        t.add_row(str(r["id"]), r["company"], ui.status(r["status"]), r["kind"], r["title"],
                  f"{hours:g}" if isinstance(hours, (int, float)) else "")
    console.print(t)
    console.print(f"Shipped this month: [bold]{b['shipped_this_month']}[/] of {b['target']}")


@pow_app.command("start")
def pow_start(project_id: int):
    """Start building an idea."""
    conn = connect()
    proof.start(conn, project_id)
    conn.commit()
    p = proof.project(conn, project_id)
    ui.ok(f"Building #{p['id']}: {p['title']} for {p['company']}. Time box: {proof.MAX_HOURS} hours.")
    console.print(f"Steps are in {config.PACKS_DIR.name}/{proof.slug(p['company'])}.md")


@pow_app.command("ship")
def pow_ship(
    project_id: int,
    summary: Annotated[str, typer.Option(help="One sentence on what it shows.")],
    repo: Annotated[Optional[str], typer.Option()] = None,
    demo: Annotated[Optional[str], typer.Option()] = None,
    hours: Annotated[Optional[float], typer.Option()] = None,
):
    """Mark a build shipped. The company's next draft will lead with it."""
    conn = connect()
    proof.ship(conn, project_id, summary, repo, demo, hours)
    conn.commit()
    p = proof.project(conn, project_id)
    ui.ok(f"Shipped #{p['id']} for {p['company']}. It's Tier A now.")
    ready = conn.execute("SELECT COUNT(*) FROM leads WHERE company_id = ? AND status = 'ready'",
                         (p["company_id"],)).fetchone()[0]
    if ready:
        console.print(f"Its next draft leads with this build: {ui.cmd('draft')}")
    else:
        ui.warn("No ready contact there. Add a named person with " + ui.cmd("lead add"))


@pow_app.command("drop")
def pow_drop(project_id: int, reason: Annotated[str, typer.Option()]):
    """Drop an idea."""
    conn = connect()
    proof.drop(conn, project_id, reason)
    conn.commit()
    ui.info(f"Dropped #{project_id}.")


# ---------------------------------------------------------------------------
# single commands
# ---------------------------------------------------------------------------

def init():
    """Create the database and import the old CSV/JSON files. Safe to re-run."""
    conn = connect()
    counts = legacy.import_all(conn)
    config.load_profile()
    ui.ok(f"Database ready at {config.DB_PATH}")
    if any(counts.values()):
        ui.ok(f"Imported {counts['companies']} companies, {counts['evidence']} evidence items, "
              f"{counts['leads']} leads and {counts['sends']} past sends")
    else:
        ui.info("Old data already imported. Nothing new.")
    ui.ok("profile.toml loaded")


def outcome(
    lead_id: int,
    result: Annotated[str, typer.Argument(help="replied, bounced or auto_reply")],
    sentiment: Annotated[Optional[str], typer.Option(help="positive, neutral or negative")] = None,
    note: Annotated[str, typer.Option()] = "",
):
    """Record a reply or bounce by hand."""
    if result not in ("replied", "bounced", "auto_reply"):
        raise SystemExit("Outcome must be replied, bounced or auto_reply.")
    if sentiment not in (None, "positive", "neutral", "negative"):
        raise SystemExit("Sentiment must be positive, neutral or negative.")
    conn = connect()
    if not mail.record_outcome(conn, lead_id, result, "manual", note, sentiment):
        raise SystemExit(f"Lead #{lead_id} already has an outcome.")
    conn.commit()
    ui.ok(f"Lead #{lead_id}: {result}" + (f" ({sentiment})" if sentiment else ""))


def report():
    """Reply rates by source, tier, pitch and prompt version."""
    conn = connect()
    for dim, title in (("source", "By source"), ("tier", "By tier"),
                       ("pitch", "By pitch"), ("prompt", "By prompt version")):
        t = ui.table(dim.title(), "Sent", "Bounced", "Replied", "Positive", "Reply rate (95% CI)",
                     title=title)
        for r in experiments.breakdown(conn, dim):
            t.add_row(str(r["bucket"]), str(r["sent"]), str(r["bounced"]), str(r["replied"]),
                      str(r["positive"]), ui.pct(r["rate"], r["ci"]))
        console.print(t)
    unknown = conn.execute("""SELECT COUNT(*) FROM messages m LEFT JOIN outcomes o
                              ON o.lead_id = m.lead_id WHERE o.lead_id IS NULL""").fetchone()[0]
    if unknown:
        ui.info(f"{unknown} sent email(s) have no outcome yet. {ui.cmd('sync')} checks Gmail.")


def register(app: typer.Typer) -> None:
    app.add_typer(lead_app, name="lead", rich_help_panel="Data")
    app.add_typer(company_app, name="company", rich_help_panel="Data")
    app.add_typer(exp_app, name="exp", rich_help_panel="Loops")
    app.add_typer(pow_app, name="pow", rich_help_panel="Loops")
    app.command(rich_help_panel="Data")(outcome)
    app.command(rich_help_panel="Insight")(report)
    app.command(rich_help_panel="Setup")(init)
