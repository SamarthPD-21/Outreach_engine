"""Review stage: read each draft next to the evidence it cites, then decide."""

import json

import click
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from . import db, drafting, research, ui

CHOICES = ["a", "e", "r", "s", "q"]
PROMPT = "[bold]a[/]pprove  [bold]e[/]dit  [bold]r[/]eject  [bold]s[/]kip  [bold]q[/]uit"


def render(conn, draft, lead, profile, position: str) -> None:
    c = ui.console
    tags = [f"[arm]{k} → {v}[/]" for k, v in json.loads(draft["arms_json"]).items()]
    if draft["project_id"]:
        tags.append("[ok]proof-of-work pitch[/]")
    c.rule(f"[stage]Draft #{draft['id']}[/]  [muted]{position}[/]")
    c.print(f"[bold]{lead['company']}[/] · {lead['role']} · [accent]{lead['email']}[/]"
            + (f"   {'  '.join(tags)}" if tags else ""))
    email = Text(drafting.compose(lead, profile, draft["body"]).strip())
    c.print(Panel(email, title=f"[bold]{draft['subject']}[/]", title_align="left",
                  border_style="accent", padding=(1, 2)))

    evidence = {e["id"]: e for e in db.evidence_for(conn, lead["company_id"])}
    claims = json.loads(draft["claims_json"])
    if claims:
        t = ui.table("Claim", "Source", title="What it says about them, and where that came from")
        for claim in claims:
            eid = research.evidence_id(claim.get("evidence_id"))
            ev = evidence.get(eid)
            source = (f"[muted]E{eid} {ev['kind']}[/]\n{ev['snippet'][:160]}…" if ev
                      else "[bad]no such evidence[/]")
            t.add_row(claim.get("text", ""), source)
        c.print(t)
    for issue in json.loads(draft["lint_json"]):
        if issue["level"] == "blocking":
            ui.fail(issue["msg"])
        else:
            ui.warn(issue["msg"])


def run(conn, profile) -> dict:
    drafts = drafting.pending_drafts(conn)
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    if not drafts:
        ui.info("No drafts waiting for review.")
        return counts
    for n, d in enumerate(drafts, start=1):
        lead = db.lead_view(conn, d["lead_id"])
        while True:
            draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (d["id"],)).fetchone()
            render(conn, draft, lead, profile, f"{n} of {len(drafts)}")
            choice = Prompt.ask(PROMPT, choices=CHOICES, default="s",
                                show_choices=False, console=ui.console)
            if choice == "a":
                blockers = drafting.decide(conn, draft["id"], "approved")
                if blockers:
                    ui.fail("Fix the ✗ issues before approving. Press e to edit.")
                    continue
                conn.commit()
                ui.ok("Approved.")
                counts["approved"] += 1
            elif choice == "e":
                edited = click.edit(f"{draft['subject']}\n---\n{draft['body']}\n", extension=".txt")
                if edited is None:
                    ui.info("No changes.")
                    continue
                subject, _, body = edited.partition("\n---\n")
                issues = drafting.update_text(conn, draft["id"], subject, body, profile)
                conn.commit()
                if not drafting.blocking(issues):
                    ui.ok("Edited. The draft passes every check.")
                continue
            elif choice == "r":
                reason = Prompt.ask("Why? (used in later analysis)", default="", console=ui.console)
                drafting.decide(conn, draft["id"], "rejected", reason)
                conn.commit()
                ui.info("Rejected. The lead is back in the ready list.")
                counts["rejected"] += 1
            elif choice == "s":
                counts["skipped"] += 1
            else:
                counts["skipped"] += len(drafts) - n + 1
                return counts
            break
    return counts
