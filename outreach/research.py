"""Collect facts about a company and save each one as evidence with its source URL.

Nothing here calls an LLM. Drafts and proof-of-work ideas may only use what
this module saved, which is what keeps them from inventing company facts.
"""

import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import db

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = 10
SNIPPET_CHARS = 1500

# Paths tried on the company site, grouped by the kind of evidence they give.
SITE_PAGES = {
    "homepage": [""],
    "about": ["/about", "/about-us", "/company"],
    "careers": ["/careers", "/jobs"],
    "product": ["/product", "/products", "/platform", "/features"],
    "engineering": ["/blog", "/engineering", "/changelog"],
    "developers": ["/docs", "/developers", "/api"],
}
GITHUB_SKIP = {"orgs", "features", "about", "sponsors", "login", "topics", "marketplace"}


def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                            allow_redirects=True, **kwargs)
        return resp if resp.status_code < 400 else None
    except requests.RequestException:
        return None


def page_text(html: str | bytes) -> str:
    """Readable text of a page: meta description first, then the main content.

    Pass bytes when possible so BeautifulSoup reads the page's own charset
    instead of requests' ISO-8859-1 guess.
    """
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            parts.append(tag["content"].strip())
            break
    for tag in soup(["script", "style", "nav", "footer", "iframe", "noscript", "svg", "form"]):
        tag.decompose()
    main = soup.select_one("main, article, [role=main]") or soup.body
    if main:
        parts.append(re.sub(r"\s+", " ", main.get_text(" ")).strip())
    return "\n".join(parts)[:SNIPPET_CHARS]


def find_github_org(html: str) -> str | None:
    """Return the first github.com/<org> linked from a page, if any."""
    for href in re.findall(r'href="(https?://(?:www\.)?github\.com/[^"#?]+)"', html):
        parts = urlparse(href).path.strip("/").split("/")
        if parts and parts[0] and parts[0].lower() not in GITHUB_SKIP:
            return parts[0]
    return None


def research_site(conn, company_id: int, website: str) -> int:
    """Fetch the company's main pages into evidence. Returns how many were saved."""
    base = website.rstrip("/")
    saved = 0
    github_org = None
    seen_final_urls = set()
    for kind, paths in SITE_PAGES.items():
        for path in paths:
            resp = _get(base + path)
            if resp is None or resp.url in seen_final_urls:
                continue
            # Many sites redirect unknown paths to the homepage; don't save that twice.
            seen_final_urls.add(resp.url)
            if path and resp.url.rstrip("/") == base:
                continue
            github_org = github_org or find_github_org(resp.text)
            if db.add_evidence(conn, company_id, kind, resp.url, page_text(resp.content)):
                saved += 1
                break
            time.sleep(0.3)
    if github_org:
        conn.execute(
            "UPDATE companies SET github_org = COALESCE(github_org, ?) WHERE id = ?",
            (github_org, company_id),
        )
    return saved


def _github(path: str, params: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(f"https://api.github.com{path}", headers=headers,
                            params=params, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    return resp.json() if resp.status_code == 200 else None


def research_github(conn, company_id: int, org: str, max_repos: int = 6) -> int:
    """Save the org's most active public repos and their beginner-friendly issues."""
    repos = _github(f"/orgs/{org}/repos", {"per_page": 100, "type": "public"}) \
        or _github(f"/users/{org}/repos", {"per_page": 100}) or []
    repos = [r for r in repos if not r.get("fork") and not r.get("archived")]
    repos.sort(key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at", "")), reverse=True)
    saved = 0
    for repo in repos[:max_repos]:
        snippet = (
            f"Repository {repo['full_name']}: {repo.get('description') or 'no description'}. "
            f"Language: {repo.get('language') or 'unknown'}. Stars: {repo.get('stargazers_count', 0)}. "
            f"Open issues: {repo.get('open_issues_count', 0)}. Last push: {repo.get('pushed_at', '')[:10]}. "
            f"Topics: {', '.join(repo.get('topics') or []) or 'none'}."
        )
        if db.add_evidence(conn, company_id, "github_repo", repo["html_url"], snippet):
            saved += 1
    for repo in repos[:3]:
        for label in ("good first issue", "help wanted"):
            issues = _github(f"/repos/{repo['full_name']}/issues",
                             {"labels": label, "state": "open", "per_page": 5}) or []
            for issue in issues:
                if "pull_request" in issue:
                    continue
                body = re.sub(r"\s+", " ", issue.get("body") or "")[:600]
                snippet = f"Open issue in {repo['full_name']} ({label}): {issue['title']}. {body}"
                if db.add_evidence(conn, company_id, "github_issue", issue["html_url"], snippet):
                    saved += 1
    return saved


def add_job_post(conn, company_id: int, url: str) -> int | None:
    resp = _get(url)
    if resp is None:
        return None
    return db.add_evidence(conn, company_id, "job_post", resp.url, page_text(resp.content))


def research_company(conn, company_id: int, include_github: bool = True) -> dict:
    company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    counts = {"site": 0, "github": 0}
    if company["website"]:
        counts["site"] = research_site(conn, company_id, company["website"])
    org = conn.execute("SELECT github_org FROM companies WHERE id = ?", (company_id,)).fetchone()[0]
    if include_github and org:
        counts["github"] = research_github(conn, company_id, org)
    db.log(conn, "researched", None, company=company["name"], **counts)
    return counts


def format_evidence(rows) -> str:
    """Evidence as numbered lines for a prompt: [E12] (careers) url — snippet."""
    return "\n".join(f"[E{r['id']}] ({r['kind']}) {r['url']}\n{r['snippet']}" for r in rows)


def evidence_id(value) -> int | None:
    """Accept 12, "12" or "E12" from the model."""
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def normalize_url(url: str) -> str:
    url = url.strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    return urljoin(url, "/") if url else url
