"""Send approved drafts over Gmail SMTP and read outcomes back over Gmail IMAP.

Both use GMAIL_ADDRESS and GMAIL_APP_PASSWORD from .env. Every send stores its
Message-ID so replies can be matched to the exact email (and experiment arm).
"""

import email
import imaplib
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from zoneinfo import ZoneInfo

from . import config, db, drafting

IST = ZoneInfo("Asia/Kolkata")
SEND_DAYS = {1, 2, 3}  # Tuesday to Thursday
SEND_HOURS = (9.5, 11.5)
FREE_MAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
AUTO_SUBJECT = re.compile(r"(?i)^(auto(matic)?[ -]?reply|out of (the )?office|autoreply|away)")


def in_send_window(at: datetime | None = None) -> bool:
    t = (at or datetime.now(IST)).astimezone(IST)
    hour = t.hour + t.minute / 60
    return t.weekday() in SEND_DAYS and SEND_HOURS[0] <= hour <= SEND_HOURS[1]


def sent_today(conn) -> int:
    start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT COUNT(*) FROM messages WHERE sent_at >= ? AND legacy = 0",
                        (start.astimezone(timezone.utc).isoformat(),)).fetchone()[0]


def sendable(conn) -> list:
    """Approved drafts whose lead can still be emailed, with the reason for any skip."""
    rows = conn.execute(
        """SELECT d.id AS draft_id, d.subject, d.body, l.id AS lead_id
           FROM drafts d JOIN leads l ON l.id = d.lead_id
           WHERE d.decision = 'approved' AND l.status = 'approved' ORDER BY d.id"""
    ).fetchall()
    out = []
    for r in rows:
        lead = db.lead_view(conn, r["lead_id"])
        skip = None
        if not lead["email"]:
            skip = "no address"
        elif lead["do_not_contact"]:
            skip = "marked do-not-contact"
        elif lead["email_status"] == "invalid":
            skip = "address bounced before"
        elif db.already_emailed(conn, lead["email"]):
            skip = "already emailed this address"
        out.append((r, lead, skip))
    return out


def build_message(lead, draft, profile) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = draft["subject"]
    msg["From"] = f"{profile.name} <{config.require('GMAIL_ADDRESS')}>"
    msg["To"] = lead["email"]
    msg["Message-ID"] = make_msgid(domain=config.require("GMAIL_ADDRESS").split("@")[1])
    msg.set_content(drafting.compose(lead, profile, draft["body"]))
    if profile.resume_path.exists():
        msg.add_attachment(profile.resume_path.read_bytes(), maintype="application",
                           subtype="pdf", filename=profile.resume_path.name)
    return msg


def send_approved(conn, profile, live: bool, limit: int | None = None, printer=print) -> int:
    queue = sendable(conn)
    room = max(0, config.DAILY_SEND_CAP - sent_today(conn))
    if limit is not None:
        room = min(room, limit)
    server = None
    sent = 0
    try:
        for draft, lead, skip in queue:
            if skip:
                printer(f"↪ {lead['company']} <{lead['email']}>: skipped, {skip}.")
                continue
            if sent >= room:
                printer(f"Daily limit reached ({config.DAILY_SEND_CAP}). The rest wait for tomorrow.")
                break
            msg = build_message(lead, draft, profile)
            if not live:
                printer(f"[preview] would send to {lead['email']} ({lead['company']}): {draft['subject']}")
                sent += 1
                continue
            if server is None:
                server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context())
                server.login(config.require("GMAIL_ADDRESS"), config.require("GMAIL_APP_PASSWORD"))
            try:
                server.send_message(msg)
            except smtplib.SMTPException as exc:
                printer(f"❌ {lead['email']}: {exc}")
                db.log(conn, "send_failed", lead["id"], error=str(exc))
                conn.commit()
                continue
            conn.execute(
                "INSERT INTO messages (lead_id, draft_id, message_id, to_email, subject, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (lead["id"], draft["draft_id"], msg["Message-ID"], lead["email"],
                 draft["subject"], db.now()),
            )
            db.set_status(conn, lead["id"], "sent")
            conn.commit()
            sent += 1
            printer(f"✅ Sent to {lead['email']} ({lead['company']})")
            if sent < room:
                pause = random.uniform(30, 90)
                printer(f"Waiting {pause:.0f}s before the next email…")
                time.sleep(pause)
    finally:
        if server is not None:
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
    return sent


# ---------------------------------------------------------------------------
# Inbox sync
# ---------------------------------------------------------------------------

def _text(msg: email.message.Message, limit: int = 4000) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        elif part.get_content_type() == "message/rfc822":
            parts.append(str(part.get_payload(0)) if part.get_payload() else "")
    return "\n".join(parts)[:limit]


def is_auto_reply(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").lower()
    return (auto != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond")
            or (msg.get("Precedence") or "").lower() in ("auto_reply", "bulk")
            or bool(AUTO_SUBJECT.match(msg.get("Subject") or "")))


def match_message(msg, by_msgid: dict, by_email: dict, by_domain: dict):
    """Return (lead_id, outcome, how) for an inbox message, or None if it isn't ours."""
    sender = parseaddr(msg.get("From") or "")[1].lower()
    local = sender.split("@")[0]
    if local in ("mailer-daemon", "postmaster") or "mailer-daemon" in sender:
        body = _text(msg)
        for mid, lead_id in by_msgid.items():
            if mid in body:
                return lead_id, "bounced", "message-id"
        for addr, lead_id in by_email.items():
            if addr in body.lower():
                return lead_id, "bounced", "address"
        return None
    outcome = "auto_reply" if is_auto_reply(msg) else "replied"
    refs = f"{msg.get('In-Reply-To') or ''} {msg.get('References') or ''}"
    for mid in re.findall(r"<[^>]+>", refs):
        if mid in by_msgid:
            return by_msgid[mid], outcome, "thread"
    if sender in by_email:
        return by_email[sender], outcome, "address"
    domain = sender.split("@")[-1]
    if domain in by_domain and domain not in FREE_MAIL_DOMAINS:
        return by_domain[domain], outcome, "domain"
    return None


def record_outcome(conn, lead_id: int, outcome: str, source: str, snippet: str = "",
                   sentiment: str | None = None, at: str | None = None) -> bool:
    """Save an outcome. A real reply replaces an auto-reply; nothing else is overwritten."""
    current = conn.execute("SELECT outcome FROM outcomes WHERE lead_id = ?", (lead_id,)).fetchone()
    if current and not (current["outcome"] == "auto_reply" and outcome == "replied"):
        return False
    conn.execute(
        """INSERT INTO outcomes (lead_id, outcome, sentiment, at, source, snippet)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (lead_id) DO UPDATE SET outcome = excluded.outcome,
           sentiment = excluded.sentiment, at = excluded.at, source = excluded.source,
           snippet = excluded.snippet""",
        (lead_id, outcome, sentiment, at or db.now(), source, snippet[:1000]),
    )
    lead = db.lead_view(conn, lead_id)
    if outcome in ("replied", "bounced") and lead["status"] == "sent":
        db.set_status(conn, lead_id, outcome)
    if outcome == "bounced" and lead["contact_id"]:
        conn.execute("UPDATE contacts SET email_status = 'invalid' WHERE id = ?", (lead["contact_id"],))
    if sentiment == "negative" and lead["contact_id"]:
        conn.execute("UPDATE contacts SET do_not_contact = 1 WHERE id = ?", (lead["contact_id"],))
    db.log(conn, "outcome", lead_id, outcome=outcome, sentiment=sentiment, source=source)
    return True


CLASSIFY_SYSTEM = """Label a reply to a student's job-application email.
positive: interested, wants to talk, asks for more information, or refers them to someone.
neutral: acknowledges, points to an application portal, or says they'll keep the resume on file.
negative: no openings, not interested, or asks not to be contacted.
Return JSON: {"sentiment": "positive" | "neutral" | "negative", "summary": str}"""


def sync(conn, days: int = 60, model=None, printer=print) -> dict:
    sent = conn.execute("SELECT lead_id, message_id, to_email, sent_at FROM messages").fetchall()
    if not sent:
        printer("Nothing has been sent yet.")
        return {"checked": 0, "recorded": 0}
    by_msgid = {r["message_id"]: r["lead_id"] for r in sent if r["message_id"]}
    by_email = {r["to_email"].lower(): r["lead_id"] for r in sent}
    by_domain = {r["to_email"].split("@")[1].lower(): r["lead_id"] for r in sent}
    earliest = min(r["sent_at"] for r in sent)[:10]
    since = max(datetime.fromisoformat(earliest), datetime.now() - timedelta(days=days))

    box = imaplib.IMAP4_SSL("imap.gmail.com")
    box.login(config.require("GMAIL_ADDRESS"), config.require("GMAIL_APP_PASSWORD"))
    box.select("INBOX", readonly=True)
    _, data = box.search(None, "SINCE", since.strftime("%d-%b-%Y"))
    ids = data[0].split()
    recorded = 0
    for num in ids:
        _, fetched = box.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(fetched[0][1])
        match = match_message(msg, by_msgid, by_email, by_domain)
        if not match:
            continue
        lead_id, outcome, how = match
        snippet = _text(msg, 1500).strip()
        sentiment = None
        if outcome == "replied" and model is not None:
            try:
                sentiment = model.json(CLASSIFY_SYSTEM, snippet).get("sentiment")
            except Exception as exc:  # classification is optional; keep the outcome
                printer(f"⚠️  Could not classify reply for lead {lead_id}: {exc}")
            if sentiment not in ("positive", "neutral", "negative"):
                sentiment = None
        if record_outcome(conn, lead_id, outcome, f"imap:{how}", snippet, sentiment):
            recorded += 1
            lead = db.lead_view(conn, lead_id)
            printer(f"• {lead['company']}: {outcome}{f' ({sentiment})' if sentiment else ''}"
                    f", matched by {how}")
    box.logout()
    conn.commit()
    return {"checked": len(ids), "recorded": recorded}
