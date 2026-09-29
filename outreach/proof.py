"""Branch B: proof of work for Tier A companies.

For the companies you want most, the tool builds a research pack (site pages,
public GitHub repos, open beginner issues), asks the model for small build ideas
that each point at that evidence, and tracks the build from idea to shipped.
A shipped project changes how the email is written: it leads with the thing
you built for them.
"""

import json
import re
from datetime import datetime, timezone

from . import config, db, research

KINDS = {
    "demo": "a small working app on their public API, SDK or open data",
    "oss_fix": "a pull request that fixes an open issue in one of their public repos",
    "teardown": "a written analysis of something publicly visible in their product",
    "eval": "a benchmark or evaluation of their open model, library or approach",
    "integration": "a plugin or connector between their product and a common tool",
}
MAX_HOURS = 4
MONTHLY_TARGET = 5

IDEAS_SYSTEM = f"""You help a CS student plan one small project to show a specific company
they can do the job. The project must be finished in {MAX_HOURS} hours or less and be
visible publicly (a repo, a demo link or a short write-up).

You get the company's evidence as lines tagged [E<id>], the target role, and the
student's real projects. Propose 3 to 5 ideas. Rules:
- Every idea must cite the evidence ids it is based on. Use only facts in the evidence.
- Allowed kinds: {json.dumps(KINDS)}.
- An oss_fix idea must cite a github_issue evidence line.
- Nothing that needs private access, sends heavy traffic to their systems,
  probes their security, or scrapes personal data.
- Prefer ideas that build on one of the student's existing projects.
- "email_hook" is one sentence the student could open the email with once it's built.

Return JSON: {{"ideas": [{{"title": str, "kind": str, "what": str, "why_they_care": str,
"evidence_ids": [int], "builds_on": str, "hours": number, "steps": [str],
"deliverable": str, "email_hook": str}}]}}
"builds_on" must be one of the student's project keys or "none"."""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def candidates(conn) -> list:
    """Companies worth a proof-of-work build: Tier A first, then by how much evidence exists."""
    return conn.execute(
        """SELECT c.id, c.name, c.tier, c.github_org,
                  (SELECT COUNT(*) FROM evidence e WHERE e.company_id = c.id) AS evidence,
                  (SELECT COUNT(*) FROM projects p WHERE p.company_id = c.id
                     AND p.status IN ('building','shipped')) AS active,
                  (SELECT COUNT(*) FROM leads l WHERE l.company_id = c.id
                     AND l.status IN ('ready','drafted','approved')) AS open_leads
           FROM companies c ORDER BY c.tier, evidence DESC, c.name"""
    ).fetchall()


def validate_ideas(raw: dict, evidence_rows, proof_keys) -> tuple[list[dict], list[str]]:
    """Keep ideas that cite real evidence and fit the time box. Return (kept, reasons dropped)."""
    by_id = {r["id"]: r for r in evidence_rows}
    kept, dropped = [], []
    for idea in raw.get("ideas", []):
        title = str(idea.get("title", "")).strip() or "untitled"
        ids = [i for i in map(research.evidence_id, idea.get("evidence_ids", [])) if i is not None]
        problems = []
        if not ids or any(i not in by_id for i in ids):
            problems.append("cites evidence that doesn't exist")
        if idea.get("kind") not in KINDS:
            problems.append(f"unknown kind {idea.get('kind')!r}")
        if idea.get("kind") == "oss_fix" and not any(
                by_id.get(i) is not None and by_id[i]["kind"] == "github_issue" for i in ids):
            problems.append("oss_fix without an open issue")
        try:
            hours = float(idea.get("hours", 99))
        except (TypeError, ValueError):
            hours = 99
        if hours > MAX_HOURS:
            problems.append(f"{hours:g} hours is over the {MAX_HOURS}-hour limit")
        if idea.get("builds_on") not in (*proof_keys, "none"):
            idea["builds_on"] = "none"
        if problems:
            dropped.append(f"{title}: {', '.join(problems)}")
            continue
        idea["evidence_ids"], idea["hours"] = ids, hours
        kept.append(idea)
    return kept, dropped


def build_pack(conn, company_name: str, model, profile, refresh: bool = False,
               job_url: str | None = None) -> dict:
    company = db.find_company(conn, company_name)
    if job_url:
        research.add_job_post(conn, company["id"], job_url)
    if refresh or len(db.evidence_for(conn, company["id"])) < 3:
        research.research_company(conn, company["id"])
        company = db.find_company(conn, str(company["id"]))
    evidence = db.evidence_for(conn, company["id"])
    if len(evidence) < 2:
        raise SystemExit(
            f"❌ Only {len(evidence)} evidence item(s) for {company['name']}. Set its website "
            f"(`outreach company {company['id']} --website URL`) or add a job post with --job-url."
        )
    roles = [r["role"] for r in conn.execute(
        "SELECT DISTINCT role FROM leads WHERE company_id = ?", (company["id"],))]
    user = (
        f"Company: {company['name']}\nTarget roles: {', '.join(roles) or 'software / AI intern'}\n\n"
        f"Student's projects (key: description):\n"
        + "\n".join(f"- {k}: {v}" for k, v in profile.proof_points.items())
        + f"\n\nEvidence:\n{research.format_evidence(evidence)}"
    )
    kept, dropped = validate_ideas(model.json(IDEAS_SYSTEM, user), evidence, profile.proof_points)
    new_ids = []
    for idea in kept:
        exists = conn.execute(
            "SELECT id FROM projects WHERE company_id = ? AND title = ? COLLATE NOCASE",
            (company["id"], idea["title"]),
        ).fetchone()
        if exists:
            continue
        new_ids.append(conn.execute(
            "INSERT INTO projects (company_id, title, kind, idea_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (company["id"], idea["title"], idea["kind"], json.dumps(idea), db.now()),
        ).lastrowid)
    path = write_pack(conn, company, evidence, dropped)
    db.log(conn, "pack_built", None, company=company["name"], ideas=len(kept), dropped=len(dropped))
    return {"path": path, "new": new_ids, "dropped": dropped}


def write_pack(conn, company, evidence, dropped: list[str]):
    config.PACKS_DIR.mkdir(exist_ok=True)
    path = config.PACKS_DIR / f"{slug(company['name'])}.md"
    projects = conn.execute(
        "SELECT * FROM projects WHERE company_id = ? ORDER BY id", (company["id"],)
    ).fetchall()
    lines = [
        f"# {company['name']}: research pack",
        "",
        f"- Website: {company['website'] or 'unknown'}",
        f"- GitHub: {'https://github.com/' + company['github_org'] if company['github_org'] else 'none found'}",
        f"- Tier: {company['tier']}",
        f"- Built: {datetime.now(timezone.utc).date().isoformat()}",
        "",
        "## Build ideas",
        "",
    ]
    for p in projects:
        idea = json.loads(p["idea_json"])
        lines += [
            f"### #{p['id']} {p['title']} ({p['kind']}, ~{idea.get('hours', '?')}h, {p['status']})",
            "",
            idea.get("what", ""),
            "",
            f"**Why they'd care:** {idea.get('why_they_care', '')}",
            f"**Builds on:** {idea.get('builds_on', 'none')}  ",
            f"**Deliverable:** {idea.get('deliverable', '')}  ",
            f"**Sources:** {', '.join(f'E{i}' for i in idea.get('evidence_ids', []))}",
            "",
            "Steps:",
            *[f"{n}. {s}" for n, s in enumerate(idea.get("steps", []), 1)],
            "",
            f"> Email opener once shipped: {idea.get('email_hook', '')}",
            "",
        ]
        if p["repo_url"] or p["demo_url"]:
            lines += [f"Shipped: {p['repo_url'] or ''} {p['demo_url'] or ''}".strip(), ""]
    if dropped:
        lines += ["## Ideas rejected by the checks", "", *[f"- {d}" for d in dropped], ""]
    lines += ["## Evidence", ""]
    for e in evidence:
        lines += [f"**E{e['id']}** ({e['kind']}) <{e['url']}>  ", f"{e['snippet'][:500]}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def project(conn, project_id: int):
    row = conn.execute(
        "SELECT p.*, c.name AS company FROM projects p JOIN companies c ON c.id = p.company_id "
        "WHERE p.id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"❌ No project #{project_id}. See `outreach pow list`.")
    return row


def start(conn, project_id: int) -> None:
    p = project(conn, project_id)
    if p["status"] != "idea":
        raise SystemExit(f"❌ Project #{project_id} is {p['status']}, not an idea.")
    conn.execute("UPDATE projects SET status = 'building', started_at = ? WHERE id = ?",
                 (db.now(), project_id))
    db.log(conn, "project_started", None, project=project_id, company=p["company"])


def ship(conn, project_id: int, summary: str, repo: str | None, demo: str | None,
         hours: float | None) -> None:
    p = project(conn, project_id)
    if p["status"] not in ("idea", "building"):
        raise SystemExit(f"❌ Project #{project_id} is already {p['status']}.")
    if not (repo or demo):
        raise SystemExit("❌ A shipped project needs a public --repo or --demo link.")
    conn.execute(
        "UPDATE projects SET status = 'shipped', shipped_at = ?, repo_url = ?, demo_url = ?, "
        "summary = ?, hours_spent = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
        (db.now(), repo, demo, summary, hours, db.now(), project_id),
    )
    # Proof of work makes the company worth the best treatment.
    conn.execute("UPDATE companies SET tier = 'A' WHERE id = ?", (p["company_id"],))
    db.log(conn, "project_shipped", None, project=project_id, company=p["company"])


def drop(conn, project_id: int, reason: str) -> None:
    project(conn, project_id)
    conn.execute("UPDATE projects SET status = 'dropped', summary = ? WHERE id = ?",
                 (reason, project_id))


def shipped_for(conn, company_id: int):
    return conn.execute(
        "SELECT * FROM projects WHERE company_id = ? AND status = 'shipped' "
        "ORDER BY shipped_at DESC LIMIT 1", (company_id,)
    ).fetchone()


def board(conn) -> dict:
    rows = conn.execute(
        "SELECT p.*, c.name AS company FROM projects p JOIN companies c ON c.id = p.company_id "
        "ORDER BY CASE p.status WHEN 'building' THEN 0 WHEN 'idea' THEN 1 "
        "WHEN 'shipped' THEN 2 ELSE 3 END, p.id"
    ).fetchall()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    shipped_this_month = sum(1 for r in rows if r["status"] == "shipped"
                             and (r["shipped_at"] or "").startswith(month))
    return {"rows": rows, "shipped_this_month": shipped_this_month, "target": MONTHLY_TARGET}
