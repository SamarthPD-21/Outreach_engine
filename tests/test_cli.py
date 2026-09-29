"""End-to-end tests of pipeline.py through Typer's test runner. No network, fake model."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

import pipeline
from outreach import config, db, discover, drafting, llm

runner = CliRunner()


class FakeModel:
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    def json(self, system, user):
        return FakeModel.responses.pop(0)


def good_draft(evidence_id):
    return {"subject": "Backend intern", "body": " ".join(["word"] * 80),
            "claims": [{"text": "search API", "evidence_id": evidence_id}]}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (config.DB_PATH, config.LESSONS_FILE, config.PACKS_DIR)
        config.DB_PATH = self.tmp / "test.db"
        config.LESSONS_FILE = self.tmp / "lessons.md"
        config.PACKS_DIR = self.tmp / "packs"
        self.model_patch = mock.patch.object(llm, "OpenAIModel", FakeModel)
        self.model_patch.start()

    def tearDown(self):
        db.close_all()
        self.model_patch.stop()
        config.DB_PATH, config.LESSONS_FILE, config.PACKS_DIR = self._saved
        shutil.rmtree(self.tmp)

    def cli(self, *args, input=None):
        return runner.invoke(pipeline.app, list(args), input=input)

    def seed(self):
        conn = db.shared()
        lead_id = db.add_lead(conn, "Acme", "Backend Intern", "priya@acme.com", "Priya Rao")
        company_id = db.lead_view(conn, lead_id)["company_id"]
        ids = [db.add_evidence(conn, company_id, "homepage", f"https://acme.com/{i}",
                               f"Acme builds a low-latency search API for retailers, page {i}.")
               for i in range(2)]
        conn.commit()
        return lead_id, ids

    def test_dashboard_on_empty_database(self):
        result = self.cli()
        self.assertEqual(result.exit_code, 0, result.output)
        for stage in ("Discover", "Research", "Draft", "Review", "Send", "Sync"):
            self.assertIn(stage, result.output)
        self.assertIn("Next: pipeline.py discover", result.output)

    def test_lead_add_and_list(self):
        self.assertEqual(self.cli("lead", "add", "Acme", "SDE Intern", "--email", "info@acme.com").exit_code, 0)
        out = self.cli("lead", "add", "Beta", "AI Intern").output
        self.assertIn("manual list", out)
        listing = self.cli("lead", "list").output
        self.assertIn("Acme", listing)
        self.assertIn("2 lead(s)", listing)

    def test_run_drafts_then_leaves_review_for_later(self):
        _, ids = self.seed()
        FakeModel.responses = [good_draft(ids[0])]
        result = self.cli("run", "--from", "draft", "--to", "send", "-y")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("1 draft(s), 0 need fixes", result.output)
        self.assertIn("wait for you", result.output)
        self.assertIn("No approved drafts", result.output)

    def test_interactive_review_then_send_preview(self):
        lead_id, ids = self.seed()
        FakeModel.responses = [good_draft(ids[0])]
        self.cli("draft")
        result = self.cli("review", input="a\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Approved", result.output)
        self.assertIn("Acme builds a low-latency", result.output)  # evidence shown beside the claim
        preview = self.cli("send")
        self.assertIn("priya@acme.com", preview.output)
        self.assertIn("Add --send", preview.output)
        conn = db.shared()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(db.lead_view(conn, lead_id)["status"], "approved")

    def test_blocked_draft_cannot_be_approved_in_review(self):
        _, ids = self.seed()
        bad = {"subject": "x", "body": "too short", "claims": []}
        FakeModel.responses = [bad, bad]
        self.cli("draft")
        result = self.cli("review", input="a\nq\n")
        self.assertIn("Fix the ✗ issues", result.output)

    def test_failed_stage_stops_the_run(self):
        self.seed()

        class Broken(FakeModel):
            def json(self, system, user):
                raise RuntimeError("model is down")

        with mock.patch.object(llm, "OpenAIModel", Broken):
            result = self.cli("run", "--only", "draft", "--only", "review", "-y")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("model is down", result.output)
        self.assertIn("Stopped at Draft", result.output)
        self.assertNotIn("Nothing to review", result.output)

    def test_stage_without_credentials_is_skipped(self):
        conn = db.shared()
        lead_id = db.add_lead(conn, "Acme", "Intern", "a@acme.com")
        conn.execute("INSERT INTO messages (lead_id, to_email, sent_at) VALUES (?, ?, ?)",
                     (lead_id, "a@acme.com", db.now()))
        conn.commit()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GMAIL_")}
        with mock.patch.dict(os.environ, env, clear=True):
            result = self.cli("run", "--only", "sync")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Set GMAIL_ADDRESS, GMAIL_APP_PASSWORD", result.output)

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(SystemExit):
            pipeline.run(from_="nope", to="sync", only=None, skip=None, limit=5, send=False,
                         yes=True, anytime=False, classify=False, keep_going=False)

    def test_experiment_commands(self):
        self.assertEqual(self.cli("exp", "new", "--preset", "length").exit_code, 0)
        status = self.cli("exp", "status").output
        self.assertIn("short", status)
        self.assertIn("Too early", status)
        self.assertIn("Experiment · length", self.cli().output)

    def test_discover_adds_leads_without_guessing(self):
        cards = [{"company": "Gamma", "title": "Backend Developer", "stipend": "₹ 40,000 /month",
                  "location": "Remote", "detail_url": "https://internshala.com/x"},
                 {"company": "Delta", "title": "Sales Intern", "stipend": "₹ 50,000 /month",
                  "location": "Remote", "detail_url": "https://internshala.com/y"},
                 {"company": "Eps", "title": "ML Intern", "stipend": "₹ 10,000 /month",
                  "location": "Remote", "detail_url": "https://internshala.com/z"}]
        about = "Gamma runs payment reconciliation for 400 Indian marketplaces."
        with mock.patch.object(discover, "fetch_cards", return_value=cards), \
                mock.patch.object(discover, "CATEGORY_URLS", ["https://internshala.com/internships/a/b"]), \
                mock.patch.object(discover.time, "sleep"), \
                mock.patch.object(discover, "listing_details", return_value=(about, "https://gamma.in")), \
                mock.patch.object(discover, "found_address", return_value=""):
            result = self.cli("discover")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("1 new lead(s), 0 with a published address", result.output)
        conn = db.shared()
        lead = conn.execute("SELECT * FROM leads").fetchone()
        self.assertEqual(lead["status"], "manual")
        self.assertEqual(len(db.evidence_for(conn, lead["company_id"])), 1)

    def test_doctor_runs(self):
        result = self.cli("doctor")
        self.assertIn("profile.toml loads", result.output)


if __name__ == "__main__":
    unittest.main()
