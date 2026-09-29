"""Offline tests: a fake model stands in for OpenAI and nothing touches the network."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from outreach import config, db, discover, drafting, experiments, legacy, mail, proof, stats

ROOT = Path(__file__).resolve().parent.parent
PROFILE = config.load_profile(ROOT / "profile.toml")


class FakeModel:
    """Returns queued responses in order and records the prompts it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def json(self, system, user):
        self.prompts.append(user)
        return self.responses.pop(0)


def body(words=80, extra=""):
    return (extra + " " + " ".join(["word"] * words)).strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (config.LESSONS_FILE, config.PACKS_DIR)
        config.LESSONS_FILE = self.tmp / "lessons.md"
        config.PACKS_DIR = self.tmp / "packs"
        self.conn = db.connect(Path(":memory:"))

    def tearDown(self):
        config.LESSONS_FILE, config.PACKS_DIR = self._saved
        self.conn.close()
        shutil.rmtree(self.tmp)

    def company_with_evidence(self, name="Acme", n=3):
        lead_id = db.add_lead(self.conn, name, "Backend Intern", f"priya@{name.lower()}.com", "Priya Rao")
        company_id = db.lead_view(self.conn, lead_id)["company_id"]
        ids = [db.add_evidence(self.conn, company_id, "homepage", f"https://{name}.com/{i}",
                               f"{name} builds a low-latency search API for retailers, item {i}.")
               for i in range(n)]
        return lead_id, company_id, ids

    def mark_sent(self, lead_id, days_ago=10, message_id=None):
        lead = db.lead_view(self.conn, lead_id)
        sent_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.conn.execute(
            "INSERT INTO messages (lead_id, message_id, to_email, sent_at) VALUES (?, ?, ?, ?)",
            (lead_id, message_id, lead["email"], sent_at))
        self.conn.execute("UPDATE leads SET status = 'sent' WHERE id = ?", (lead_id,))


class StatsTests(unittest.TestCase):
    def test_wilson_zero_of_ten(self):
        lo, hi = stats.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)

    def test_fisher_matches_textbook(self):
        # Classic tea-tasting style table; two-sided p = 0.0350
        self.assertAlmostEqual(stats.fisher_exact(8, 2, 1, 5), 0.0350, places=3)

    def test_fisher_identical_arms_is_one(self):
        self.assertAlmostEqual(stats.fisher_exact(3, 17, 3, 17), 1.0, places=6)

    def test_sample_size_is_large_for_small_lifts(self):
        self.assertGreater(stats.n_per_arm(0.08, 0.16), 200)
        self.assertLess(stats.n_per_arm(0.05, 0.40), 30)


class DbTests(Base):
    def test_illegal_transition_raises(self):
        lead_id = db.add_lead(self.conn, "Acme", "SDE Intern", "priya@acme.com")
        with self.assertRaises(ValueError):
            db.set_status(self.conn, lead_id, "sent")

    def test_invalid_address_goes_to_manual(self):
        lead_id = db.add_lead(self.conn, "Matrice AI", "Intern", "Careers / hiring team")
        self.assertEqual(db.lead_view(self.conn, lead_id)["status"], "manual")

    def test_generic_address_is_flagged(self):
        lead_id = db.add_lead(self.conn, "Acme", "Intern", "support@acme.com")
        self.assertEqual(db.lead_view(self.conn, lead_id)["email_status"], "generic")

    def test_add_lead_is_idempotent(self):
        a = db.add_lead(self.conn, "Acme", "Intern", "priya@acme.com")
        b = db.add_lead(self.conn, "acme", "Intern", "PRIYA@acme.com")
        self.assertEqual(a, b)


class ExperimentTests(Base):
    def test_assignment_is_balanced_and_stable(self):
        experiments.create_preset(self.conn, "subject-style")
        leads = [db.add_lead(self.conn, f"Co{i}", "Intern", f"a@co{i}.com") for i in range(10)]
        arms = [experiments.assign(self.conn, lead)["name"] for lead in leads]
        self.assertEqual(arms.count("role-fit"), 5)
        self.assertEqual(arms.count("casual"), 5)
        self.assertEqual(experiments.assign(self.conn, leads[0])["name"], arms[0])

    def test_only_one_experiment_at_a_time(self):
        experiments.create_preset(self.conn, "opening")
        with self.assertRaises(SystemExit):
            experiments.create_preset(self.conn, "length")

    def _run(self, replies_a, replies_b, n=25):
        exp_id = experiments.create_preset(self.conn, "opening")
        by_arm = {"observation": [], "question": []}
        for i in range(2 * n):
            lead = db.add_lead(self.conn, f"Co{i}", "Intern", f"a@co{i}.com")
            by_arm[experiments.assign(self.conn, lead)["name"]].append(lead)
            self.mark_sent(lead)
        for arm, k in (("observation", replies_a), ("question", replies_b)):
            for lead in by_arm[arm][:k]:
                mail.record_outcome(self.conn, lead, "replied", "test", sentiment="positive")
        return experiments.arm_results(self.conn, exp_id)

    def test_too_early_before_twenty_matured(self):
        exp_id = experiments.create_preset(self.conn, "opening")
        lead = db.add_lead(self.conn, "Co", "Intern", "a@co.com")
        experiments.assign(self.conn, lead)
        self.mark_sent(lead, days_ago=1)
        v = experiments.verdict(experiments.arm_results(self.conn, exp_id))
        self.assertTrue(v["text"].startswith("Too early"))

    def test_recent_sends_are_not_counted_as_no_reply(self):
        exp_id = experiments.create_preset(self.conn, "opening")
        lead = db.add_lead(self.conn, "Co", "Intern", "a@co.com")
        experiments.assign(self.conn, lead)
        self.mark_sent(lead, days_ago=2)
        results = experiments.arm_results(self.conn, exp_id)
        self.assertEqual(sum(r["sent"] for r in results), 1)
        self.assertEqual(sum(r["mature"] for r in results), 0)

    def test_clear_winner_is_reported(self):
        v = experiments.verdict(self._run(replies_a=1, replies_b=10))
        self.assertEqual(v["leader"], "question")

    def test_small_difference_is_not_called(self):
        v = experiments.verdict(self._run(replies_a=2, replies_b=4))
        self.assertIsNone(v["leader"])
        self.assertIn("could only tell", v["text"])

    def test_conclude_sets_default_and_writes_lesson(self):
        self._run(1, 10)
        experiments.conclude(self.conn, "opening", "question", "questions work")
        default = self.conn.execute("SELECT instruction FROM style_defaults "
                                    "WHERE variable = 'opening'").fetchone()[0]
        self.assertIn("asks one specific technical question", default)
        self.assertIn("## opening", config.LESSONS_FILE.read_text())
        # With nothing running, later drafts get the winner and no arm tag.
        lead = db.add_lead(self.conn, "Later", "Intern", "x@later.com")
        instructions, arms = experiments.style_instructions(self.conn, lead)
        self.assertEqual(arms, {})
        self.assertIn(default, instructions)


class DraftTests(Base):
    def test_grounded_draft_is_saved_with_arm(self):
        experiments.create_preset(self.conn, "length")
        lead_id, _, ev = self.company_with_evidence()
        fake = FakeModel({"subject": "Backend intern", "body": body(),
                          "claims": [{"text": "search API", "evidence_id": f"E{ev[0]}"}]})
        result = drafting.draft_lead(self.conn, lead_id, fake, PROFILE)
        self.assertFalse(drafting.blocking(result["issues"]))
        self.assertIn("length", result["arms"])
        arm = experiments.assign(self.conn, lead_id)
        self.assertIn(arm["instruction"], fake.prompts[0])

    def test_unsupported_claim_is_retried_then_blocked(self):
        lead_id, _, _ = self.company_with_evidence()
        bad = {"subject": "hi", "body": body(), "claims": [{"text": "raised $10M", "evidence_id": 999}]}
        fake = FakeModel(bad, bad)
        result = drafting.draft_lead(self.conn, lead_id, fake, PROFILE)
        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("unsupported claim", fake.prompts[1])
        self.assertTrue(drafting.blocking(result["issues"]))
        self.assertTrue(drafting.decide(self.conn, result["draft_id"], "approved"))

    def test_lint_catches_style_problems(self):
        issues = drafting.lint({"subject": "x", "body": body(80, "I am passionate. See https://evil.example/x"),
                                "claims": [{"evidence_id": 1}]}, {1}, PROFILE, PROFILE.links)
        msgs = " ".join(i["msg"] for i in issues)
        self.assertIn("passionate", msgs)
        self.assertIn("evil.example", msgs)

    def test_profile_links_are_allowed(self):
        issues = drafting.lint({"subject": "x", "body": body(80, "Code: https://github.com/SamarthPD-21/typeahead"),
                                "claims": [{"evidence_id": 1}]}, {1}, PROFILE, PROFILE.links)
        self.assertFalse(drafting.blocking(issues))

    def test_no_evidence_means_no_draft(self):
        lead_id = db.add_lead(self.conn, "Velozity", "Intern", "a@velozity.in")
        with self.assertRaises(ValueError):
            drafting.draft_lead(self.conn, lead_id, FakeModel(), PROFILE)

    def test_team_names_get_neutral_greeting(self):
        self.assertEqual(drafting.greeting("Gnani.ai Hiring Team"), "Hi there,")
        self.assertEqual(drafting.greeting("Nikhil Pareek"), "Hi Nikhil,")

    def test_shipped_project_becomes_the_pitch(self):
        lead_id, company_id, ev = self.company_with_evidence()
        pid = self.conn.execute(
            "INSERT INTO projects (company_id, title, kind, idea_json, created_at) VALUES (?,?,?,?,?)",
            (company_id, "Search latency probe", "demo", "{}", db.now())).lastrowid
        proof.ship(self.conn, pid, "p95 dashboard for their public search API",
                   "https://github.com/SamarthPD-21/acme-probe", None, 3)
        fake = FakeModel({"subject": "built this for acme", "claims": [{"evidence_id": ev[0]}],
                          "body": body(70, "I built https://github.com/SamarthPD-21/acme-probe")})
        result = drafting.draft_lead(self.conn, lead_id, fake, PROFILE)
        self.assertTrue(result["project"])
        self.assertIn("Search latency probe", fake.prompts[0])
        self.assertFalse(drafting.blocking(result["issues"]))
        self.assertEqual(db.find_company(self.conn, "Acme")["tier"], "A")


class ProofTests(Base):
    def test_ideas_are_checked_against_evidence(self):
        _, company_id, ev = self.company_with_evidence()
        issue = db.add_evidence(self.conn, company_id, "github_issue", "https://github.com/acme/x/issues/4",
                                "Open issue in acme/x (good first issue): typo in the CLI help output.")
        rows = db.evidence_for(self.conn, company_id)
        raw = {"ideas": [
            {"title": "ok", "kind": "demo", "evidence_ids": [ev[0]], "hours": 3},
            {"title": "made up", "kind": "demo", "evidence_ids": [12345], "hours": 2},
            {"title": "too big", "kind": "demo", "evidence_ids": [ev[1]], "hours": 12},
            {"title": "fix without issue", "kind": "oss_fix", "evidence_ids": [ev[0]], "hours": 2},
            {"title": "real fix", "kind": "oss_fix", "evidence_ids": [f"E{issue}"], "hours": 2},
        ]}
        kept, dropped = proof.validate_ideas(raw, rows, PROFILE.proof_points)
        self.assertEqual([i["title"] for i in kept], ["ok", "real fix"])
        self.assertEqual(len(dropped), 3)

    def test_pack_writes_file_and_ideas(self):
        _, company_id, ev = self.company_with_evidence()
        fake = FakeModel({"ideas": [{"title": "Latency probe", "kind": "demo", "evidence_ids": [ev[0]],
                                     "hours": 3, "builds_on": "typeahead", "steps": ["a", "b"],
                                     "email_hook": "I measured your search API."}]})
        result = proof.build_pack(self.conn, "Acme", fake, PROFILE)
        self.assertEqual(len(result["new"]), 1)
        text = result["path"].read_text()
        self.assertIn("Latency probe", text)
        self.assertIn(f"E{ev[0]}", text)
        # Running again doesn't duplicate the idea.
        fake2 = FakeModel({"ideas": [{"title": "latency probe", "kind": "demo",
                                      "evidence_ids": [ev[0]], "hours": 3}]})
        self.assertEqual(proof.build_pack(self.conn, "Acme", fake2, PROFILE)["new"], [])

    def test_ship_needs_a_public_link(self):
        _, company_id, _ = self.company_with_evidence()
        pid = self.conn.execute(
            "INSERT INTO projects (company_id, title, kind, idea_json, created_at) VALUES (?,?,?,?,?)",
            (company_id, "x", "demo", "{}", db.now())).lastrowid
        with self.assertRaises(SystemExit):
            proof.ship(self.conn, pid, "summary", None, None, 2)


class MailTests(Base):
    def _msg(self, sender, subject="Re: hello", **headers):
        m = EmailMessage()
        m["From"], m["Subject"] = sender, subject
        for k, v in headers.items():
            m[k.replace("_", "-")] = v
        m.set_content("Thanks, let's talk.")
        return m

    def test_reply_matched_by_thread(self):
        hit = mail.match_message(self._msg("x@other.com", In_Reply_To="<abc@gmail.com>"),
                                 {"<abc@gmail.com>": 7}, {}, {})
        self.assertEqual(hit, (7, "replied", "thread"))

    def test_auto_reply_detected(self):
        hit = mail.match_message(self._msg("priya@acme.com", "Out of office", Auto_Submitted="auto-replied"),
                                 {}, {"priya@acme.com": 3}, {})
        self.assertEqual(hit[1], "auto_reply")

    def test_bounce_matched_by_address(self):
        m = self._msg("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery failed")
        m.set_content("Your message to careers@guess.com couldn't be delivered.")
        self.assertEqual(mail.match_message(m, {}, {"careers@guess.com": 5}, {}), (5, "bounced", "address"))

    def test_free_mail_domain_is_not_matched(self):
        self.assertIsNone(mail.match_message(self._msg("random@gmail.com"), {}, {}, {"gmail.com": 1}))

    def test_outcomes_update_lead_and_contact(self):
        lead_id, _, _ = self.company_with_evidence()
        self.mark_sent(lead_id)
        mail.record_outcome(self.conn, lead_id, "auto_reply", "test")
        mail.record_outcome(self.conn, lead_id, "replied", "test", sentiment="negative")
        lead = db.lead_view(self.conn, lead_id)
        self.assertEqual(lead["status"], "replied")
        self.assertEqual(lead["do_not_contact"], 1)

    def test_preview_send_records_nothing(self):
        lead_id, _, ev = self.company_with_evidence()
        fake = FakeModel({"subject": "s", "body": body(), "claims": [{"evidence_id": ev[0]}]})
        draft_id = drafting.draft_lead(self.conn, lead_id, fake, PROFILE)["draft_id"]
        self.assertEqual(drafting.decide(self.conn, draft_id, "approved"), [])
        import os
        os.environ.setdefault("GMAIL_ADDRESS", "me@gmail.com")
        lines = []
        self.assertEqual(mail.send_approved(self.conn, PROFILE, live=False, printer=lines.append), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_already_emailed_address_is_skipped(self):
        lead_id, _, ev = self.company_with_evidence()
        self.conn.execute("INSERT INTO messages (lead_id, to_email, sent_at, legacy) VALUES (?,?,?,1)",
                          (lead_id, "priya@acme.com", db.now()))
        other = db.add_lead(self.conn, "Acme", "AI Intern", "priya@acme.com")
        fake = FakeModel({"subject": "s", "body": body(), "claims": [{"evidence_id": ev[0]}]})
        draft_id = drafting.draft_lead(self.conn, other, fake, PROFILE)["draft_id"]
        drafting.decide(self.conn, draft_id, "approved")
        skips = [skip for _, _, skip in mail.sendable(self.conn)]
        self.assertEqual(skips, ["already emailed this address"])

    def test_send_window(self):
        tue_10am = datetime(2026, 9, 29, 10, 0, tzinfo=mail.IST)
        sat_10am = datetime(2026, 10, 3, 10, 0, tzinfo=mail.IST)
        self.assertTrue(mail.in_send_window(tue_10am))
        self.assertFalse(mail.in_send_window(sat_10am))


class DiscoverParsingTests(unittest.TestCase):
    CARD = """<div class="individual_internship">
      <h3 class="job-internship-name"><a href="/internship/detail/x1">Backend Developer</a></h3>
      <p class="company-name">Acme Pvt. Ltd. (Pune)</p>
      <span class="stipend">₹ 25,000 - 45,000 /month</span>
      <a class="location_link">Bangalore</a></div>"""

    def test_listing_card_is_parsed(self):
        [card] = discover.parse_cards(self.CARD)
        self.assertEqual(card["company"], "Acme")
        self.assertEqual(card["detail_url"], "https://internshala.com/internship/detail/x1")
        self.assertEqual(discover.max_stipend(card["stipend"]), 45000)

    def test_company_names_are_cleaned(self):
        self.assertEqual(discover.clean_company_name("Bar Inc."), "Bar")
        self.assertEqual(discover.clean_company_name("Incubate Labs LLP"), "Incubate Labs")

    def test_roles_are_filtered(self):
        self.assertTrue(discover.is_engineering_role("Machine Learning Intern"))
        self.assertFalse(discover.is_engineering_role("Sales Intern"))

    def test_emails_skip_noise_and_prefer_careers(self):
        page = ('<body><a href="mailto:Careers@acme.com?subject=hi">mail</a> '
                'hr@acme.com support@acme.com logo@2x.png jobs@internshala.com</body>')
        emails = discover.emails_in(page)
        self.assertEqual(emails, ["careers@acme.com", "hr@acme.com"])
        self.assertEqual(min(emails, key=discover.rank_email), "careers@acme.com")


@unittest.skipUnless((config.LEGACY_DIR / "sent_log.csv").exists(),
                     "data/legacy/ is git-ignored and only exists on the original machine")
class LegacyImportTests(Base):
    def test_real_files_import_once(self):
        saved = config.LEGACY_DIR
        shutil.copytree(config.LEGACY_DIR, self.tmp / "legacy")
        config.LEGACY_DIR = self.tmp / "legacy"
        try:
            first = legacy.import_all(self.conn)
            second = legacy.import_all(self.conn)
        finally:
            config.LEGACY_DIR = saved
        sends = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(first["sends"], 12)
        self.assertEqual(set(second.values()), {0})
        self.assertEqual(sends, 12)
        # The two recruiters.csv rows with real addresses were already emailed.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status = 'ready'").fetchone()[0], 0)
        # AI summaries are not evidence: Velozity's scrape was empty.
        velozity = db.find_company(self.conn, "Velozity Global Solutions")
        self.assertEqual(db.evidence_for(self.conn, velozity["id"]), [])


if __name__ == "__main__":
    unittest.main()
