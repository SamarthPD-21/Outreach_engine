"""Branch A: the experiment loop.

One experiment runs at a time and changes one variable. Each new draft is
assigned to the arm with the fewest leads so far, the arm's instruction goes
into the prompt, and replies found by `outreach sync` decide which arm wins.
The winner's instruction becomes the default for every later draft.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from . import config, db, stats

# Ready-made experiments. Each changes exactly one thing about the email.
PRESETS = {
    "subject-style": {
        "variable": "subject",
        "hypothesis": "A subject that names the role and one concrete fit gets more replies than a casual lowercase one.",
        "arms": {
            "role-fit": 'Subject names the role and one concrete fit, in sentence case, under 9 words. '
                        'Example: "Backend intern: built a sharded Redis autocomplete".',
            "casual": 'Subject is 2 to 4 lowercase words that read like a personal note and do not '
                      'name the role. Example: "your search latency".',
        },
    },
    "opening": {
        "variable": "opening",
        "hypothesis": "Opening with a specific technical question beats opening with an observation.",
        "arms": {
            "observation": "The first sentence states one specific observation about their product or "
                           "engineering, taken from the evidence.",
            "question": "The first sentence asks one specific technical question about how they built "
                        "something described in the evidence.",
        },
    },
    "proof-lead": {
        "variable": "proof",
        "hypothesis": "Leading with shipped production work beats leading with the strongest side project.",
        "arms": {
            "production": "Lead the proof section with the freelance production work (sole engineer, "
                          "25+ deployments), then one project.",
            "project": "Lead the proof section with the single most relevant technical project, then "
                       "mention production work in one short clause.",
        },
    },
    "length": {
        "variable": "length",
        "hypothesis": "A 60 to 90 word email gets more replies than a 120 to 160 word one.",
        "arms": {
            "short": "The whole body is 60 to 90 words in three short paragraphs.",
            "standard": "The whole body is 120 to 160 words.",
        },
    },
    "ask": {
        "variable": "ask",
        "hypothesis": "Offering a concrete 15-minute walkthrough beats a general request to be considered.",
        "arms": {
            "consider": "Close by asking whether they would consider you for the role.",
            "walkthrough": "Close by offering a 15-minute walkthrough of the most relevant project.",
        },
    },
}

MIN_MATURE_PER_ARM = 20


def running(conn):
    return conn.execute("SELECT * FROM experiments WHERE status = 'running'").fetchone()


def create(conn, name: str, variable: str, arms: dict[str, str], hypothesis: str = "") -> int:
    if len(arms) < 2:
        raise SystemExit("❌ An experiment needs at least two arms.")
    current = running(conn)
    if current:
        raise SystemExit(
            f"❌ Experiment {current['name']!r} is still running. Change one thing at a time: "
            f"conclude it first with `outreach exp conclude {current['name']}`."
        )
    exp_id = conn.execute(
        "INSERT INTO experiments (name, variable, hypothesis, created_at) VALUES (?, ?, ?, ?)",
        (name, variable, hypothesis, db.now()),
    ).lastrowid
    for arm, instruction in arms.items():
        conn.execute(
            "INSERT INTO arms (experiment_id, name, instruction) VALUES (?, ?, ?)",
            (exp_id, arm, instruction),
        )
    db.log(conn, "experiment_started", None, name=name, variable=variable, arms=list(arms))
    return exp_id


def create_preset(conn, preset: str, name: str | None = None) -> int:
    if preset not in PRESETS:
        raise SystemExit(f"❌ Unknown preset {preset!r}. Choose from: {', '.join(PRESETS)}.")
    p = PRESETS[preset]
    return create(conn, name or preset, p["variable"], p["arms"], p["hypothesis"])


def assign(conn, lead_id: int):
    """Return the lead's arm in the running experiment, assigning one if needed."""
    exp = running(conn)
    if exp is None:
        return None
    row = conn.execute(
        """SELECT a.* FROM assignments s JOIN arms a ON a.id = s.arm_id
           WHERE s.lead_id = ? AND s.experiment_id = ?""",
        (lead_id, exp["id"]),
    ).fetchone()
    if row:
        return row
    arms = conn.execute(
        """SELECT a.*, COUNT(s.lead_id) AS n FROM arms a
           LEFT JOIN assignments s ON s.arm_id = a.id
           WHERE a.experiment_id = ? GROUP BY a.id""",
        (exp["id"],),
    ).fetchall()
    fewest = min(r["n"] for r in arms)
    tied = [r for r in arms if r["n"] == fewest]
    # Deterministic tie-break so re-running a draft never reshuffles arms.
    pick = tied[int(hashlib.sha256(str(lead_id).encode()).hexdigest(), 16) % len(tied)]
    conn.execute(
        "INSERT INTO assignments (lead_id, experiment_id, arm_id) VALUES (?, ?, ?)",
        (lead_id, exp["id"], pick["id"]),
    )
    db.log(conn, "assigned", lead_id, experiment=exp["name"], arm=pick["name"])
    return pick


def style_instructions(conn, lead_id: int) -> tuple[list[str], dict]:
    """Instructions for this lead's draft: past winners, plus the running experiment's arm."""
    instructions = {r["variable"]: r["instruction"]
                    for r in conn.execute("SELECT * FROM style_defaults")}
    arms = {}
    exp = running(conn)
    if exp:
        arm = assign(conn, lead_id)
        instructions[exp["variable"]] = arm["instruction"]
        arms[exp["name"]] = arm["name"]
    return list(instructions.values()), arms


def _mature_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=config.MATURE_AFTER_DAYS)).isoformat()


def arm_results(conn, exp_id: int) -> list[dict]:
    """Per-arm counts. Only sends that had time to get a reply count toward the rate."""
    rows = conn.execute(
        """SELECT a.id, a.name, a.instruction,
                  COUNT(m.id) AS sent,
                  SUM(o.outcome = 'bounced') AS bounced,
                  SUM(o.outcome = 'replied') AS replied,
                  SUM(o.outcome = 'replied' AND o.sentiment = 'positive') AS positive,
                  SUM(o.outcome = 'replied' OR (o.outcome IS NOT 'bounced' AND m.sent_at <= ?)) AS mature
           FROM arms a
           LEFT JOIN assignments s ON s.arm_id = a.id
           LEFT JOIN messages m ON m.lead_id = s.lead_id AND m.kind = 'initial'
           LEFT JOIN outcomes o ON o.lead_id = s.lead_id
           WHERE a.experiment_id = ? GROUP BY a.id ORDER BY a.id""",
        (_mature_cutoff(), exp_id),
    ).fetchall()
    out = []
    for r in rows:
        mature, replied = r["mature"] or 0, r["replied"] or 0
        lo, hi = stats.wilson(replied, mature)
        out.append({
            "arm": r["name"], "instruction": r["instruction"], "arm_id": r["id"],
            "sent": r["sent"], "bounced": r["bounced"] or 0, "replied": replied,
            "positive": r["positive"] or 0, "mature": mature,
            "rate": replied / mature if mature else None, "ci": (lo, hi),
        })
    return out


def verdict(results: list[dict]) -> dict:
    """Plain-language reading of the results, including what can't be concluded yet."""
    need = max(0, MIN_MATURE_PER_ARM - min(r["mature"] for r in results))
    total_sent = sum(r["sent"] for r in results)
    bounced = sum(r["bounced"] for r in results)
    out = {"p_value": None, "leader": None, "text": ""}
    warnings = []
    if total_sent and bounced / total_sent > 0.03:
        warnings.append(f"Bounce rate is {bounced / total_sent:.0%}. Above 3%, fix address checks "
                        "before sending more.")
    if need:
        out["text"] = f"Too early. Each arm needs {need} more sends that have had "\
                      f"{config.MATURE_AFTER_DAYS} days to get a reply."
    else:
        ranked = sorted(results, key=lambda r: r["rate"], reverse=True)
        best, second = ranked[0], ranked[1]
        p = stats.fisher_exact(best["replied"], best["mature"] - best["replied"],
                               second["replied"], second["mature"] - second["replied"])
        out["p_value"] = p
        n = min(r["mature"] for r in results)
        pooled = sum(r["replied"] for r in results) / sum(r["mature"] for r in results)
        detectable = stats.detectable_rate(pooled, n)
        if p < 0.05:
            out["leader"] = best["arm"]
            out["text"] = (f"{best['arm']} is ahead ({best['rate']:.0%} vs {second['rate']:.0%}, "
                           f"p = {p:.3f}). You can conclude with it as the winner.")
        else:
            limit = (f"At {n} sends per arm you could only tell {pooled:.0%} apart from "
                     f"{detectable:.0%} or more." if detectable else "")
            out["text"] = (f"No clear difference yet ({best['arm']} {best['rate']:.0%} vs "
                           f"{second['arm']} {second['rate']:.0%}, p = {p:.2f}). {limit}").strip()
    out["warnings"] = warnings
    return out


def conclude(conn, name: str, winner: str | None, note: str = "") -> str:
    exp = conn.execute("SELECT * FROM experiments WHERE name = ?", (name,)).fetchone()
    if exp is None:
        raise SystemExit(f"❌ No experiment called {name!r}.")
    if exp["status"] != "running":
        raise SystemExit(f"❌ {name!r} is already concluded.")
    results = arm_results(conn, exp["id"])
    v = verdict(results)
    winner_row = None
    if winner:
        winner_row = next((r for r in results if r["arm"] == winner), None)
        if winner_row is None:
            raise SystemExit(f"❌ {name!r} has no arm called {winner!r}.")
        conn.execute(
            """INSERT INTO style_defaults (variable, instruction, experiment_id) VALUES (?, ?, ?)
               ON CONFLICT (variable) DO UPDATE SET instruction = excluded.instruction,
               experiment_id = excluded.experiment_id""",
            (exp["variable"], winner_row["instruction"], exp["id"]),
        )
    conn.execute(
        "UPDATE experiments SET status = 'concluded', concluded_at = ?, winner_arm = ?, "
        "conclusion = ? WHERE id = ?",
        (db.now(), winner_row["arm_id"] if winner_row else None, note or v["text"], exp["id"]),
    )
    db.log(conn, "experiment_concluded", None, name=name, winner=winner, verdict=v["text"])
    entry = lessons_entry(exp, results, v, winner, note)
    with open(config.LESSONS_FILE, "a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write("# Outreach lessons\n\nOne entry per concluded experiment, newest last.\n")
        f.write(entry)
    return entry


def lessons_entry(exp, results, v, winner, note) -> str:
    lines = [
        f"\n## {exp['name']} ({datetime.now().date().isoformat()})\n",
        f"- Variable: {exp['variable']}",
        f"- Hypothesis: {exp['hypothesis'] or 'none recorded'}",
        f"- Winner: {winner or 'none, kept previous style'}",
        f"- Reading: {v['text']}",
    ]
    if note:
        lines.append(f"- Note: {note}")
    lines += ["", "| Arm | Sent | Matured | Replied | Positive | Reply rate (95% CI) |",
              "|---|---|---|---|---|---|"]
    for r in results:
        rate = f"{r['rate']:.0%} ({r['ci'][0]:.0%}–{r['ci'][1]:.0%})" if r["rate"] is not None else "–"
        lines.append(f"| {r['arm']} | {r['sent']} | {r['mature']} | {r['replied']} | "
                     f"{r['positive']} | {rate} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Breakdowns and feedback into lead priority
# ---------------------------------------------------------------------------

DIMENSIONS = {
    "source": "l.source",
    "tier": "c.tier",
    "pitch": "CASE WHEN d.project_id IS NOT NULL THEN 'proof of work' "
             "WHEN m.legacy = 1 THEN 'legacy script' ELSE 'standard' END",
    "prompt": "COALESCE(d.prompt_version, 'legacy')",
}


def breakdown(conn, dimension: str) -> list[dict]:
    expr = DIMENSIONS[dimension]
    rows = conn.execute(
        f"""SELECT {expr} AS bucket, COUNT(m.id) AS sent,
                   SUM(o.outcome = 'bounced') AS bounced,
                   SUM(o.outcome = 'replied') AS replied,
                   SUM(o.outcome = 'replied' AND o.sentiment = 'positive') AS positive
            FROM messages m JOIN leads l ON l.id = m.lead_id
            JOIN companies c ON c.id = l.company_id
            LEFT JOIN drafts d ON d.id = m.draft_id
            LEFT JOIN outcomes o ON o.lead_id = m.lead_id
            WHERE m.kind = 'initial' GROUP BY bucket ORDER BY sent DESC"""
    ).fetchall()
    out = []
    for r in rows:
        replied = r["replied"] or 0
        delivered = r["sent"] - (r["bounced"] or 0)
        out.append({**dict(r), "replied": replied, "bounced": r["bounced"] or 0,
                    "positive": r["positive"] or 0,
                    "rate": replied / delivered if delivered else None,
                    "ci": stats.wilson(replied, delivered)})
    return out


def source_weights(conn) -> dict[str, float]:
    """Smoothed reply rate per lead source, so sources that produce replies rise.

    Uses a Beta(1, 9) prior (a 10% guess) so one lucky reply doesn't dominate.
    """
    return {r["bucket"]: (r["replied"] + 1) / (r["sent"] - r["bounced"] + 10)
            for r in breakdown(conn, "source")}


def prioritized_ready_leads(conn) -> list:
    weights = source_weights(conn)
    rows = conn.execute(
        """SELECT l.id, l.source, c.tier FROM leads l JOIN companies c ON c.id = l.company_id
           WHERE l.status = 'ready'"""
    ).fetchall()
    return sorted(rows, key=lambda r: (r["tier"], -weights.get(r["source"], 0.1), r["id"]))
