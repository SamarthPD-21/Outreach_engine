"""Write grounded drafts: every claim about the company must cite saved evidence."""

import json
import re

from . import db, experiments, proof, research

PROMPT_VERSION = "v1-grounded"

DRAFT_SYSTEM = """You write one short cold email from a CS student to a company about a role.

You get the company's evidence as lines tagged [E<id>], the role, the student's real
background, style instructions, and sometimes a project the student built for this company.

Rules:
- Every statement about the company must come from the evidence. List each one in
  "claims" with the evidence id it came from. If the evidence doesn't support a fact,
  leave it out.
- Only use the student's facts as given. Pick the 2 or 3 that fit this role.
- If a project built for this company is supplied, open with it: what it is, the link,
  and what it shows. That is the main point of the email.
- Plain, direct, warm. Contractions are fine. No flattery, no buzzwords, no
  "I hope this finds you well", no "I am writing to express my interest".
- One clear, low-pressure ask at the end.
- Write the body only: no greeting line, no sign-off, no signature. They are added later.
- No links in the body except ones given to you.
- Follow every style instruction exactly.

Return JSON: {"subject": str, "body": str,
              "claims": [{"text": str, "evidence_id": int}]}"""

BUILT_IN_BANNED = [
    "i hope this email finds you well", "i am writing to express", "passionate",
    "cutting-edge", "synergy", "leverage", "rockstar", "dream company", "revolutionary",
    "hiring team",
]
URL_RE = re.compile(r"https?://\S+|\b[\w-]+\.(?:dev|io|com|ai|app)/\S*")


def lint(draft: dict, evidence_ids: set[int], profile, allowed_links: list[str]) -> list[dict]:
    """Return problems. 'blocking' ones must be fixed before a draft can be approved."""
    issues = []
    body, subject = draft.get("body", ""), draft.get("subject", "")
    claims = draft.get("claims", [])

    def add(level, msg):
        issues.append({"level": level, "msg": msg})

    if not claims:
        add("blocking", "no claims about the company, so nothing shows the email is specific")
    for c in claims:
        if research.evidence_id(c.get("evidence_id")) not in evidence_ids:
            add("blocking", f"unsupported claim: {c.get('text', '')[:80]!r}")
    words = len(body.split())
    if not 50 <= words <= 180:
        add("blocking", f"body is {words} words (keep it 50–180)")
    if len(subject.split()) > 10 or not subject.strip():
        add("warning", "subject should be 1–10 words")
    lowered = f"{subject}\n{body}".lower()
    for phrase in BUILT_IN_BANNED + [p.lower() for p in profile.banned_phrases]:
        if phrase in lowered:
            add("blocking", f"banned phrase: {phrase!r}")
    for url in URL_RE.findall(body):
        if not any(link and link.rstrip("/") in url for link in allowed_links):
            add("blocking", f"link not from your profile or project: {url}")
    if body.count("?") > 2:
        add("warning", "more than two questions")
    if re.match(r"(?i)\s*(hi|hello|hey|dear)\b", body):
        add("warning", "body starts with a greeting, which gets added separately")
    return issues


def blocking(issues: list[dict]) -> bool:
    return any(i["level"] == "blocking" for i in issues)


def greeting(contact_name: str | None) -> str:
    name = (contact_name or "").strip()
    if not name or re.search(r"(?i)team|hiring|careers|hr\b|founder", name):
        return "Hi there,"
    return f"Hi {name.split()[0]},"


def compose(lead, profile, body: str) -> str:
    return f"{greeting(lead['contact_name'])}\n\n{body.strip()}\n\n{profile.signature}\n"


def draft_lead(conn, lead_id: int, model, profile) -> dict:
    lead = db.lead_view(conn, lead_id)
    if lead["status"] != "ready":
        raise ValueError(f"lead {lead_id} is {lead['status']}, not ready")
    evidence = db.evidence_for(conn, lead["company_id"])
    if len(evidence) < 2:
        raise ValueError(
            f"{lead['company']} has {len(evidence)} evidence item(s). "
            f"Run `outreach research {lead['company_id']}` first."
        )
    instructions, arms = experiments.style_instructions(conn, lead_id)
    project = proof.shipped_for(conn, lead["company_id"])
    allowed_links = list(profile.links)
    project_text = "None."
    if project:
        allowed_links += [project["repo_url"] or "", project["demo_url"] or ""]
        project_text = (f"{project['title']}: {project['summary'] or ''} "
                        f"Repo: {project['repo_url'] or 'none'}. Demo: {project['demo_url'] or 'none'}.")
    keys = profile.proof_points_for(lead["role"])
    background = profile.summary + "\n" + "\n".join(
        f"- {profile.proof_points[k]}" for k in keys if k in profile.proof_points)
    user = (
        f"Company: {lead['company']}\nRole: {lead['role']}\n\n"
        f"Student background:\n{background}\n\n"
        f"Links you may use: {', '.join(l for l in allowed_links if l) or 'none'}\n\n"
        f"Project built for this company: {project_text}\n\n"
        f"Style instructions:\n" + ("\n".join(f"- {i}" for i in instructions) or "- none") +
        f"\n\nEvidence:\n{research.format_evidence(evidence)}"
    )
    ids = {e["id"] for e in evidence}
    result = model.json(DRAFT_SYSTEM, user)
    issues = lint(result, ids, profile, allowed_links)
    if blocking(issues):
        # One retry with the problems spelled out.
        feedback = "\n".join(f"- {i['msg']}" for i in issues if i["level"] == "blocking")
        result = model.json(DRAFT_SYSTEM, f"{user}\n\nYour last draft had these problems. "
                                          f"Fix all of them:\n{feedback}")
        issues = lint(result, ids, profile, allowed_links)
    draft_id = conn.execute(
        """INSERT INTO drafts (lead_id, subject, body, claims_json, arms_json, project_id,
                               prompt_version, lint_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (lead_id, result.get("subject", "").strip(), result.get("body", "").strip(),
         json.dumps(result.get("claims", [])), json.dumps(arms),
         project["id"] if project else None, PROMPT_VERSION, json.dumps(issues), db.now()),
    ).lastrowid
    db.set_status(conn, lead_id, "drafted")
    db.log(conn, "drafted", lead_id, draft=draft_id, arms=arms, blocking=blocking(issues))
    return {"draft_id": draft_id, "issues": issues, "arms": arms, "project": bool(project)}


def pending_drafts(conn) -> list:
    return conn.execute(
        """SELECT d.* FROM drafts d JOIN leads l ON l.id = d.lead_id
           WHERE d.decision = 'pending' AND l.status = 'drafted' ORDER BY d.id"""
    ).fetchall()


def allowed_links(conn, draft, profile) -> list[str]:
    links = list(profile.links)
    if draft["project_id"]:
        p = conn.execute("SELECT repo_url, demo_url FROM projects WHERE id = ?",
                         (draft["project_id"],)).fetchone()
        links += [p["repo_url"] or "", p["demo_url"] or ""]
    return links


def update_text(conn, draft_id: int, subject: str, body: str, profile) -> list[dict]:
    """Save edited text and re-lint it. The claims stay as the model cited them."""
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    lead = db.lead_view(conn, d["lead_id"])
    ids = {e["id"] for e in db.evidence_for(conn, lead["company_id"])}
    issues = lint({"subject": subject, "body": body, "claims": json.loads(d["claims_json"])},
                  ids, profile, allowed_links(conn, d, profile))
    conn.execute("UPDATE drafts SET subject = ?, body = ?, lint_json = ? WHERE id = ?",
                 (subject.strip(), body.strip(), json.dumps(issues), draft_id))
    db.log(conn, "draft_edited", d["lead_id"], draft=draft_id)
    return issues


def decide(conn, draft_id: int, decision: str, reason: str = "") -> list[dict]:
    """Approve or reject a draft. Returns the blocking issues that stopped an approval."""
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    issues = json.loads(d["lint_json"])
    if decision == "approved" and blocking(issues):
        return [i for i in issues if i["level"] == "blocking"]
    conn.execute("UPDATE drafts SET decision = ?, reason = ? WHERE id = ?",
                 (decision, reason or None, draft_id))
    db.set_status(conn, d["lead_id"], "approved" if decision == "approved" else "ready")
    db.log(conn, f"draft_{decision}", d["lead_id"], draft=draft_id, reason=reason)
    return []
