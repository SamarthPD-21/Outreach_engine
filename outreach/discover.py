"""Discover stage: find engineering internships on Internshala and add them as leads.

Contact addresses are only kept when they were actually published on a page;
nothing is guessed. The listing's "about the company" text is saved as
evidence, so a new lead can often be drafted without a separate research run.
"""

import random
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Internshala searches already filtered to ₹30,000+/month, Bengaluru or remote.
FEED = "https://internshala.com/internships/{}/stipend-30000"
CATEGORY_URLS = [FEED.format(slug) for slug in (
    "artificial-intelligence-ai-internship-in-bangalore",
    "work-from-home-artificial-intelligence-ai-internships",
    "machine-learning-internship-in-bangalore",
    "work-from-home-machine-learning-internships",
    "software-development-internship-in-bangalore",
    "work-from-home-software-development-internships",
    "backend-development-internship-in-bangalore",
    "work-from-home-backend-development-internships",
    "computer-science-internship-in-bangalore",
    "full-stack-development-internship-in-bangalore",
)]

RELEVANT_ROLE_WORDS = (
    "software", "sde", "backend", "full stack", "fullstack", "ai", "artificial intelligence",
    "ml", "machine learning", "deep learning", "llm", "python", "developer", "engineer",
    "data science", "data engineer",
)
IRRELEVANT_ROLE_WORDS = (
    "sales", "business development", "marketing", "hr", "human resources", "content",
    "graphic", "telecaller", "operations", "recruitment",
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
NOISE_PREFIXES = (
    "noreply", "no-reply", "donotreply", "privacy", "abuse", "admin", "webmaster",
    "postmaster", "mailer-daemon", "support", "newsletter", "unsubscribe", "feedback",
    "legal", "compliance", "billing", "complaints", "grievance", "spam",
)
NOISE_DOMAINS = {
    "internshala.com", "linkedin.com", "facebook.com", "google.com", "twitter.com",
    "instagram.com", "youtube.com", "github.com", "example.com", "sentry.io", "cloudflare.com",
}
FILE_SUFFIXES = (".png", ".jpg", ".gif", ".svg", ".css", ".js", ".webp")
# Best inboxes for a job enquiry, first to last.
PREFERRED_PREFIXES = ("careers", "hr", "hiring", "jobs", "recruit", "talent",
                      "hello", "hi", "hey", "contact", "info", "team")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def clean_company_name(raw: str) -> str:
    """Drop legal suffixes and bracketed locations: 'Acme Pvt. Ltd. (Pune)' → 'Acme'."""
    name = re.sub(r"\s*\([^)]*\)", "", raw.strip())
    name = re.sub(r"(?i)\s*(pvt\.?|private)\s*(ltd\.?|limited)", "", name)
    name = re.sub(r"(?i)\s*\b(llp|inc)\b\.?", "", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize(name: str) -> str:
    """Key for deduplication: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def max_stipend(text: str) -> int:
    """Highest monthly figure in '₹ 25,000 - 45,000 /month' style text."""
    numbers = re.findall(r"\b\d{4,6}\b", (text or "").replace(",", "").replace(" ", ""))
    return max(map(int, numbers), default=0)


def is_engineering_role(title: str) -> bool:
    t = title.lower()
    return (not any(w in t for w in IRRELEVANT_ROLE_WORDS)
            and any(w in t for w in RELEVANT_ROLE_WORDS))


def parse_cards(html: str | bytes) -> list[dict]:
    """Listings on an Internshala search page."""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for card in soup.find_all("div", class_="individual_internship"):
        title_el = card.find("h3", class_="job-internship-name") or card.find("a", class_="job-title-href")
        if not title_el:
            continue
        link = title_el if title_el.has_attr("href") else title_el.find("a", href=True)
        href = link["href"] if link else ""
        company_el = card.find("p", class_="company-name") or card.find("div", class_="company_name")
        stipend_el = card.find("span", class_="stipend")
        location_el = card.find("a", class_="location_link") or card.find("div", class_="locations")
        cards.append({
            "company": clean_company_name(company_el.get_text(strip=True) if company_el else ""),
            "title": title_el.get_text(strip=True),
            "stipend": stipend_el.get_text(strip=True) if stipend_el else "",
            "location": location_el.get_text(strip=True) if location_el else "Bengaluru / Remote",
            "detail_url": f"https://internshala.com{href}" if href.startswith("/") else href,
        })
    return cards


def emails_in(html: str | bytes) -> list[str]:
    """Real-looking addresses from mailto links and page text, without noise."""
    soup = BeautifulSoup(html, "html.parser")
    found = {a["href"][7:].split("?")[0].strip().lower()
             for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")}
    if soup.body:
        found |= {m.lower() for m in EMAIL_RE.findall(soup.body.get_text(" "))}
    return sorted(e for e in found
                  if EMAIL_RE.fullmatch(e)
                  and not e.split("@")[0].startswith(NOISE_PREFIXES)
                  and e.split("@")[1] not in NOISE_DOMAINS
                  and not e.endswith(FILE_SUFFIXES))


def rank_email(email: str) -> int:
    local = email.split("@")[0]
    return next((i for i, p in enumerate(PREFERRED_PREFIXES) if local.startswith(p)), 100)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 10) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None
    return resp if resp.status_code == 200 else None


def fetch_cards(url: str) -> list[dict]:
    resp = _get(url, timeout=12)
    return parse_cards(resp.content) if resp else []


def known_companies(conn) -> set[str]:
    return {normalize(clean_company_name(r["name"])) for r in conn.execute("SELECT name FROM companies")}


def scan_feeds(conn, min_stipend: int, on_feed=None) -> list[dict]:
    """Every new, relevant, well-paid listing across the feeds, deduplicated."""
    seen = known_companies(conn)
    found = []
    for i, url in enumerate(CATEGORY_URLS, start=1):
        for card in fetch_cards(url):
            key = normalize(card["company"])
            if (not key or key in seen or max_stipend(card["stipend"]) < min_stipend
                    or not is_engineering_role(card["title"])):
                continue
            seen.add(key)
            found.append(card)
        if on_feed:
            on_feed(i, len(CATEGORY_URLS), url.split("/")[4])
        time.sleep(random.uniform(0.6, 1.2))
    return found


def listing_details(detail_url: str) -> tuple[str, str]:
    """(about text, company website) from an Internshala listing page."""
    resp = _get(detail_url)
    if resp is None:
        return "", ""
    soup = BeautifulSoup(resp.content, "html.parser")
    about = soup.find("div", class_="about_company_text_container") or soup.find("div", class_="about_company")
    link = soup.find("a", class_="website_link")
    return (about.get_text(" ", strip=True) if about else "",
            link["href"] if link and link.has_attr("href") else "")


def found_address(detail_url: str, website: str) -> str:
    """An address published on the listing or the company's own site. Never a guess."""
    resp = _get(detail_url) if detail_url else None
    emails = emails_in(resp.content) if resp else []
    domain = (urlparse(website).hostname or "").removeprefix("www.") if website else ""
    if not emails and website:
        for path in ("", "/contact", "/careers", "/about", "/team"):
            resp = _get(website.rstrip("/") + path, timeout=8)
            emails = [e for e in (emails_in(resp.content) if resp else [])
                      if not domain or e.endswith("@" + domain)]
            if emails:
                break
    return min(emails, key=rank_email) if emails else ""


def add_listing(conn, card: dict) -> dict:
    about, website = listing_details(card["detail_url"])
    email = found_address(card["detail_url"], website)
    role = card["title"] if "intern" in card["title"].lower() else f"{card['title']} Intern"
    lead_id = db.add_lead(conn, card["company"], role, email, source="internshala",
                          website=website or None)
    lead = db.lead_view(conn, lead_id)
    if about:
        db.add_evidence(conn, lead["company_id"], "job_post", card["detail_url"],
                        f"{about}\nListing: {card['title']}, {card['stipend']}, {card['location']}.")
    db.log(conn, "discovered", lead_id, url=card["detail_url"], stipend=card["stipend"])
    conn.commit()
    return {"lead_id": lead_id, "company": card["company"], "role": role, "email": email,
            "stipend": card["stipend"], "status": lead["status"]}
