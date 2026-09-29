"""One-time import of the old scripts' CSV/JSON files (data/legacy/) into outreach.db.

Safe to run again: nothing is imported twice.
"""

import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db


def _rows(name: str) -> list[dict]:
    path = config.LEGACY_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


TABLES = {"companies": "companies", "evidence": "evidence", "leads": "leads", "sends": "messages"}


def _sizes(conn) -> dict:
    return {k: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for k, t in TABLES.items()}


def import_all(conn) -> dict:
    """Import everything and return how many new rows each table got."""
    before = _sizes(conn)

    research_file = config.LEGACY_DIR / "company_research.json"
    if research_file.exists():
        for entry in json.loads(research_file.read_text(encoding="utf-8")).values():
            company_id = db.get_or_create_company(conn, entry["company"], entry.get("website"))
            base = (entry.get("website") or "").rstrip("/")
            # Only scraped text becomes evidence. The old ai_summary field is skipped
            # because some summaries were written from empty scrapes.
            for kind, path in (("homepage", ""), ("about", "/about"), ("careers", "/careers")):
                text = entry.get(kind) or ""
                if text and not text.startswith("[Could not"):
                    db.add_evidence(conn, company_id, kind, base + path, text)

    for row in _rows("new_recruiters.csv"):
        db.add_lead(conn, row["Company"], row["Role"], row.get("Email", ""),
                    row.get("RecruiterName", ""), source="internshala")
    for row in _rows("recruiters.csv"):
        db.add_lead(conn, row["Company"], row["Role"], row.get("Email", ""),
                    row.get("RecruiterName", ""), source="legacy")

    for row in _rows("sent_log.csv"):
        lead_id = db.add_lead(conn, row["Company"], row["Role"], row["Email"],
                              row.get("RecruiterName", ""), source="legacy")
        if conn.execute("SELECT 1 FROM messages WHERE lead_id = ? AND legacy = 1",
                        (lead_id,)).fetchone():
            continue
        # sent_log.csv holds naive local times from the machine that sent them.
        sent_at = datetime.fromisoformat(row["Timestamp"]).astimezone()
        conn.execute(
            "INSERT INTO messages (lead_id, to_email, subject, sent_at, legacy) VALUES (?, ?, ?, ?, 1)",
            (lead_id, row["Email"], None, sent_at.astimezone(ZoneInfo("UTC")).isoformat()),
        )
        # These went out before the pipeline existed, so they skip the draft steps.
        conn.execute("UPDATE leads SET status = 'sent', updated_at = ? WHERE id = ?",
                     (db.now(), lead_id))
        db.log(conn, "legacy_send_imported", lead_id, email=row["Email"])
    conn.commit()
    after = _sizes(conn)
    return {k: after[k] - before[k] for k in TABLES}
