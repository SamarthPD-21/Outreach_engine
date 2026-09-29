"""Settings, secrets and the candidate profile.

Secrets come from .env (never from code). The candidate's facts come from
profile.toml so no resume text lives in Python.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("OUTREACH_HOME", Path(__file__).resolve().parent.parent))


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env without overriding the real environment."""
    env_file = path or ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


load_env()


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"❌ {name} is not set. Add it to {ROOT / '.env'}.")
    return value


DB_PATH = Path(os.environ.get("OUTREACH_DB", ROOT / "outreach.db"))
PACKS_DIR = ROOT / "packs"
LESSONS_FILE = ROOT / "lessons.md"
PROFILE_PATH = ROOT / "profile.toml"
# CSV/JSON files from the old scripts, imported once by `pipeline.py init`.
LEGACY_DIR = ROOT / "data" / "legacy"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
DAILY_SEND_CAP = int(os.environ.get("OUTREACH_DAILY_CAP", "15"))
# A send only counts as "no reply" once it has had this long to get one.
MATURE_AFTER_DAYS = int(os.environ.get("OUTREACH_MATURE_DAYS", "7"))


@dataclass
class Profile:
    name: str
    email: str
    summary: str
    signature: str
    resume_path: Path
    links: list[str]
    proof_points: dict[str, str]
    role_proof_points: dict[str, list[str]]
    banned_phrases: list[str] = field(default_factory=list)

    def proof_points_for(self, role: str) -> list[str]:
        """Pick the proof-point keys that fit a role title, most relevant first."""
        role_l = role.lower()
        is_ai = re.search(r"\b(ai|ml|genai|llm|machine learning|applied ai)\b", role_l)
        is_sde = re.search(r"\b(sde|software|backend|full[- ]?stack|developer)\b", role_l)
        key = "ai_sde" if is_ai and is_sde else "ai" if is_ai else "sde"
        return self.role_proof_points.get(key, list(self.proof_points))


def load_profile(path: Path | None = None) -> Profile:
    p = path or PROFILE_PATH
    if not p.exists():
        raise SystemExit(f"❌ {p} not found. It holds your resume facts and signature.")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    c = data["candidate"]
    return Profile(
        name=c["name"],
        email=c["email"],
        summary=c["summary"].strip(),
        signature=c["signature"].strip(),
        resume_path=ROOT / c["resume_path"],
        links=c.get("links", []),
        proof_points=data["proof_points"],
        role_proof_points=data.get("role_proof_points", {}),
        banned_phrases=data.get("style", {}).get("banned_phrases", []),
    )
