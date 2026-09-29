"""SQLite storage. One database holds every lead, draft, send and outcome."""

import atexit
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    website     TEXT,
    github_org  TEXT,
    tier        TEXT NOT NULL DEFAULT 'C' CHECK (tier IN ('A','B','C')),
    notes       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id),
    name           TEXT,
    title          TEXT,
    email          TEXT UNIQUE COLLATE NOCASE,
    email_status   TEXT NOT NULL DEFAULT 'unverified',
    do_not_contact INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    contact_id  INTEGER REFERENCES contacts(id),
    role        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    status      TEXT NOT NULL DEFAULT 'new',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (company_id, contact_id, role)
);
CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    kind        TEXT NOT NULL,
    url         TEXT NOT NULL,
    snippet     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    UNIQUE (company_id, url, kind)
);
CREATE TABLE IF NOT EXISTS experiments (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    variable     TEXT NOT NULL,
    hypothesis   TEXT,
    status       TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','concluded')),
    created_at   TEXT NOT NULL,
    concluded_at TEXT,
    winner_arm   INTEGER REFERENCES arms(id),
    conclusion   TEXT
);
CREATE TABLE IF NOT EXISTS arms (
    id            INTEGER PRIMARY KEY,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    name          TEXT NOT NULL,
    instruction   TEXT NOT NULL,
    UNIQUE (experiment_id, name)
);
CREATE TABLE IF NOT EXISTS assignments (
    lead_id       INTEGER NOT NULL REFERENCES leads(id),
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    arm_id        INTEGER NOT NULL REFERENCES arms(id),
    PRIMARY KEY (lead_id, experiment_id)
);
-- Winning instructions from concluded experiments, used when no test is running.
CREATE TABLE IF NOT EXISTS style_defaults (
    variable      TEXT PRIMARY KEY,
    instruction   TEXT NOT NULL,
    experiment_id INTEGER REFERENCES experiments(id)
);
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id),
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    idea_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idea'
                CHECK (status IN ('idea','building','shipped','dropped')),
    repo_url    TEXT,
    demo_url    TEXT,
    summary     TEXT,
    hours_spent REAL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    shipped_at  TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY,
    lead_id        INTEGER NOT NULL REFERENCES leads(id),
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    claims_json    TEXT NOT NULL DEFAULT '[]',
    arms_json      TEXT NOT NULL DEFAULT '{}',
    project_id     INTEGER REFERENCES projects(id),
    prompt_version TEXT NOT NULL,
    lint_json      TEXT NOT NULL DEFAULT '[]',
    decision       TEXT NOT NULL DEFAULT 'pending'
                   CHECK (decision IN ('pending','approved','rejected')),
    reason         TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    lead_id     INTEGER NOT NULL REFERENCES leads(id),
    draft_id    INTEGER REFERENCES drafts(id),
    message_id  TEXT UNIQUE,
    to_email    TEXT NOT NULL COLLATE NOCASE,
    subject     TEXT,
    kind        TEXT NOT NULL DEFAULT 'initial',
    sent_at     TEXT NOT NULL,
    legacy      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outcomes (
    lead_id    INTEGER PRIMARY KEY REFERENCES leads(id),
    outcome    TEXT NOT NULL CHECK (outcome IN ('replied','bounced','auto_reply')),
    sentiment  TEXT CHECK (sentiment IN ('positive','neutral','negative')),
    at         TEXT NOT NULL,
    source     TEXT NOT NULL,
    snippet    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    lead_id  INTEGER,
    type     TEXT NOT NULL,
    payload  TEXT,
    at       TEXT NOT NULL
);
"""

# Allowed lead status moves. Anything else is a bug and raises.
TRANSITIONS = {
    "new": {"ready", "manual"},
    "manual": {"ready"},
    "ready": {"drafted", "manual"},
    "drafted": {"drafted", "approved", "ready"},
    "approved": {"sent", "drafted"},
    "sent": {"replied", "bounced", "closed"},
    "replied": {"closed"},
    "bounced": {"closed"},
    "closed": set(),
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
GENERIC_PREFIXES = ("support", "info", "contact", "noreply", "no-reply", "admin", "sales")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


_shared: dict[str, sqlite3.Connection] = {}


def shared(path: Path | None = None) -> sqlite3.Connection:
    """The process's one connection to the database, opened on first use."""
    key = str(path or config.DB_PATH)
    if key not in _shared:
        _shared[key] = connect(Path(key))
    return _shared[key]


@atexit.register
def close_all() -> None:
    for conn in _shared.values():
        conn.close()
    _shared.clear()


def log(conn: sqlite3.Connection, type_: str, lead_id: int | None = None, **payload) -> None:
    conn.execute(
        "INSERT INTO events (lead_id, type, payload, at) VALUES (?, ?, ?, ?)",
        (lead_id, type_, json.dumps(payload, default=str), now()),
    )


def set_status(conn: sqlite3.Connection, lead_id: int, new: str) -> None:
    row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"lead {lead_id} does not exist")
    old = row["status"]
    if old == new:
        return
    if new not in TRANSITIONS.get(old, set()):
        raise ValueError(f"lead {lead_id}: cannot move from {old} to {new}")
    conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (new, now(), lead_id))
    log(conn, "status", lead_id, old=old, new=new)


def classify_email(email: str) -> str:
    """'invalid', 'generic' (support@, info@...) or 'unverified' (looks like a real inbox)."""
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        return "invalid"
    if email.split("@")[0].lower().startswith(GENERIC_PREFIXES):
        return "generic"
    return "unverified"


def get_or_create_company(conn, name: str, website: str | None = None) -> int:
    name = name.strip()
    row = conn.execute("SELECT id, website FROM companies WHERE name = ?", (name,)).fetchone()
    if row:
        if website and not row["website"]:
            conn.execute("UPDATE companies SET website = ? WHERE id = ?", (website, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO companies (name, website, created_at) VALUES (?, ?, ?)", (name, website, now())
    )
    return cur.lastrowid


def find_company(conn, name_or_id: str) -> sqlite3.Row:
    if str(name_or_id).isdigit():
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (int(name_or_id),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM companies WHERE name = ?", (name_or_id,)).fetchone()
    if row is None:
        raise SystemExit(f"❌ No company called {name_or_id!r}. See `outreach leads`.")
    return row


def add_lead(conn, company: str, role: str, email: str = "", contact_name: str = "",
             title: str = "", source: str = "manual", website: str | None = None) -> int:
    """Create (or return the existing) lead and set its first status from the address."""
    company_id = get_or_create_company(conn, company, website)
    contact_id = None
    status = "manual"
    email = email.strip()
    if email:
        email_status = classify_email(email)
        if email_status != "invalid":
            row = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
            if row:
                contact_id = row["id"]
            else:
                contact_id = conn.execute(
                    "INSERT INTO contacts (company_id, name, title, email, email_status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (company_id, contact_name or None, title or None, email, email_status),
                ).lastrowid
            status = "ready"
    existing = conn.execute(
        "SELECT id FROM leads WHERE company_id = ? AND contact_id IS ? AND role = ?",
        (company_id, contact_id, role),
    ).fetchone()
    if existing:
        return existing["id"]
    ts = now()
    lead_id = conn.execute(
        "INSERT INTO leads (company_id, contact_id, role, source, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company_id, contact_id, role, source, status, ts, ts),
    ).lastrowid
    log(conn, "lead_added", lead_id, company=company, role=role, status=status)
    return lead_id


def lead_view(conn, lead_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT l.*, c.name AS company, c.website, c.tier, c.github_org,
                  ct.name AS contact_name, ct.email, ct.email_status, ct.do_not_contact
           FROM leads l JOIN companies c ON c.id = l.company_id
           LEFT JOIN contacts ct ON ct.id = l.contact_id WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"❌ No lead with id {lead_id}.")
    return row


def already_emailed(conn, email: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE to_email = ?", (email,)).fetchone() is not None


def add_evidence(conn, company_id: int, kind: str, url: str, snippet: str) -> int | None:
    snippet = (snippet or "").strip()
    if len(snippet) < 40:
        return None
    conn.execute(
        """INSERT INTO evidence (company_id, kind, url, snippet, fetched_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (company_id, url, kind) DO UPDATE SET snippet = excluded.snippet,
           fetched_at = excluded.fetched_at""",
        (company_id, kind, url, snippet, now()),
    )
    return conn.execute(
        "SELECT id FROM evidence WHERE company_id = ? AND url = ? AND kind = ?",
        (company_id, url, kind),
    ).fetchone()["id"]


def evidence_for(conn, company_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evidence WHERE company_id = ? ORDER BY id", (company_id,)
    ).fetchall()
