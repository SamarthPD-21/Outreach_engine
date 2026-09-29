# Cold Outreach Engine

A command-line tool for job-hunt cold email that finds internship leads, researches
each company, writes drafts backed by real facts, lets you approve every email,
sends through Gmail, and reads the replies back so you can see what works.

It's built around three ideas:

1. **Nothing is made up.** Every statement a draft makes about a company must cite a
   fact saved from a real page. No evidence means no draft.
2. **You approve every email.** The tool drafts and checks. You decide what's sent.
3. **Measure, then change.** Replies are matched to the exact email that caused them,
   so experiments and proof-of-work pitches are judged on data rather than hunches.

---

## Contents

- [Quick start](#quick-start)
- [Setup](#setup)
- [How the pipeline works](#how-the-pipeline-works)
- [The dashboard](#the-dashboard)
- [Running the pipeline](#running-the-pipeline)
- [The six stages](#the-six-stages)
- [Leads and companies](#leads-and-companies)
- [Experiment loop](#experiment-loop)
- [Proof of work](#proof-of-work)
- [Reports](#reports)
- [Rules the tool enforces](#rules-the-tool-enforces)
- [Configuration reference](#configuration-reference)
- [Data model](#data-model)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env              # add your OpenAI key and Gmail app password
python pipeline.py init           # create outreach.db and import the old data
python pipeline.py doctor         # check that everything is set up
python pipeline.py                # see the dashboard
python pipeline.py run            # run every stage (sending stays a preview)
```

A typical week:

```bash
python pipeline.py sync                          # what happened to last week's emails?
python pipeline.py discover                      # find new listings
python pipeline.py lead add "Acme" "Backend Intern" --email priya@acme.com --name "Priya Rao" --website acme.com
python pipeline.py run --from research --to review
python pipeline.py send --send                   # Tuesday to Thursday morning
python pipeline.py exp status                    # is the current experiment telling you anything?
```

---

## Setup

### Requirements

Python 3.12 or newer, then:

```bash
pip install -r requirements.txt
```

| Package | Used for |
|---|---|
| `requests`, `beautifulsoup4` | Fetching and parsing company sites and listings |
| `openai` | Writing drafts, build ideas and reply labels |
| `typer`, `rich` | The command line and its tables, panels and prompts |

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored and is the only
place secrets live.

| Variable | Needed for | Notes |
|---|---|---|
| `OPENAI_API_KEY` | draft, `pow pack`, `sync --classify` | |
| `OPENAI_MODEL` | | Defaults to `gpt-5.6-luna` |
| `GMAIL_ADDRESS` | send, sync | The account you send from |
| `GMAIL_APP_PASSWORD` | send, sync | A 16-character App Password, not your normal password |
| `GITHUB_TOKEN` | optional | Raises GitHub's API limit from 60 to 5,000 requests an hour |

To get a Gmail App Password:

1. Turn on 2-Step Verification for the Google account.
2. Create one at <https://myaccount.google.com/apppasswords>.
3. In Gmail settings, under **Forwarding and POP/IMAP**, enable IMAP. `sync` needs it
   to read replies.

A stage that needs a missing variable is skipped with a message saying what to add.

### Your profile (`profile.toml`)

Everything the drafts are allowed to say about you comes from `profile.toml`:

- `[candidate]`: name, email, resume path, links, a short summary and the signature
  added to every email.
- `[proof_points]`: your projects and experience, one line each. The keys (`typeahead`,
  `rag`, and so on) are how drafts and build ideas refer to them.
- `[role_proof_points]`: which proof points lead for each role type. `sde` is for
  backend and full-stack roles, `ai` for AI and ML roles, and `ai_sde` for roles that
  mix the two.
- `[style]`: extra banned phrases, on top of built-in ones like "passionate" and
  "I hope this email finds you well".

Keep it accurate. The model is told to use only what's here.

### First run

```bash
python pipeline.py init
python pipeline.py doctor
```

`init` creates `outreach.db` and imports the old scripts' data from `data/legacy/`:
past sends, leads and scraped company text. Running it again imports nothing twice.

`doctor` checks the keys, profile, resume file and database. It also checks that no
API key is sitting in a `.py` file and that `.env` is git-ignored.

---

## How the pipeline works

```
 ┌──────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐   ┌─────────┐   ┌─────────┐
 │ discover │ → │ research │ → │  draft  │ → │  review  │ → │  send   │ → │  sync   │
 └──────────┘   └──────────┘   └─────────┘   └──────────┘   └─────────┘   └─────────┘
  new leads      evidence       drafts with    approved      emails with   replies,
  + listing      with source    cited claims   drafts        Message-IDs   bounces
  text           URLs                                                          │
       ▲                             ▲                                         │
       │                             └── experiment arm + proof-of-work pitch  │
       └──────────── reply rates by source decide which leads go first ◄───────┘
```

`pipeline.py` is the entry point. The stages don't call each other. Each one reads
what the previous stage left in `outreach.db` and writes its own results there. So
you can run them all at once, one at a time, or on different days, and nothing is
lost in between.

Every lead has exactly one status, and only these moves are allowed:

```
new ──► ready ──► drafted ──► approved ──► sent ──► replied ──► closed
  │       ▲          │                       │
  └──► manual        └── rejected: back       └──► bounced ──► closed
      (no address)       to ready
```

A lead without a usable address goes to `manual`. It stays there until you add a
contact with `lead add`.

---

## The dashboard

```bash
python pipeline.py            # or: python pipeline.py status
```

The dashboard shows:

- Totals: leads, emails sent, replies, positive replies and reply rate.
- For each stage, how much work is waiting, with notes such as "4 have no website",
  "2 need fixes" or "3/15 today, window open".
- The running experiment, with replies per arm and a plain-language reading.
- The proof-of-work board: builds in progress, ideas, and how many shipped this month.
- A suggested next step.

---

## Running the pipeline

```bash
python pipeline.py run [options]
```

| Option | What it does |
|---|---|
| `--from STAGE`, `--to STAGE` | Run a slice, for example `--from research --to review` |
| `--only STAGE` | Run just this stage. Repeat it for more than one. |
| `--skip STAGE` | Leave a stage out. Repeatable. |
| `-n`, `--limit N` | Most items per stage (default 5) |
| `--send` | Really send. Without it, the send stage only previews. |
| `-y`, `--yes` | No prompts. Review is left for later, and sending skips the confirmation. |
| `--anytime` | Ignore the Tuesday to Thursday morning send window |
| `--classify` | Label replies as positive, neutral or negative during sync |
| `--keep-going` | Continue after a stage fails |

How a run behaves:

- A stage with nothing to do is skipped.
- A stage missing a key in `.env` is skipped with a note.
- If a stage fails, the run stops and prints the command to resume from that stage.
- The run ends with a summary table: each stage's result, time and outcome.

Every stage can also run on its own, as described next.

---

## The six stages

### 1. Discover

```bash
python pipeline.py discover [-n 10] [--min-stipend 30000]
```

- Scans 10 Internshala searches: AI, ML, software, backend, CS and full-stack, in
  Bengaluru and remote, at ₹30,000+ a month.
- Keeps engineering roles only, and drops sales, marketing, HR, content and similar.
- Skips companies already in the database.
- For each new listing, saves the listing's "about the company" text as evidence and
  the company website if the listing names one.
- Looks for a published contact address on the listing, then on the company's
  homepage, `/contact`, `/careers`, `/about` and `/team`. On the company's own pages
  it only accepts addresses on the company's domain. `careers@` and `hr@` rank above
  `info@`. **No address is ever guessed.** If none is found, the lead goes to `manual`.

### 2. Research

```bash
python pipeline.py research                     # every company with open leads and < 2 facts
python pipeline.py research "Future AGI"        # one company
python pipeline.py research 13 --job-url https://... --no-github
```

For each company, research fetches and saves as evidence:

| Kind | Pages tried |
|---|---|
| `homepage` | `/` |
| `about` | `/about`, `/about-us`, `/company` |
| `careers` | `/careers`, `/jobs` |
| `product` | `/product`, `/products`, `/platform`, `/features` |
| `engineering` | `/blog`, `/engineering`, `/changelog` |
| `developers` | `/docs`, `/developers`, `/api` |
| `github_repo` | Up to 6 active public repos from the GitHub org linked on the site |
| `github_issue` | Open `good first issue` and `help wanted` issues in the top 3 repos |
| `job_post` | Any `--job-url` you pass |

Each evidence item stores its URL, the fetch time and up to 1,500 characters of text.
Redirects back to the homepage aren't saved twice. Companies without a website are
listed with the command to set one.

### 3. Draft

```bash
python pipeline.py draft            # the best ready leads, up to --limit
python pipeline.py draft 12 14      # specific leads
```

- Leads are taken Tier A first, then by how well their source has produced replies.
- A lead is only drafted if its company has at least 2 evidence items.
- The model receives the evidence as `[E<id>]` lines, your profile, the proof points
  for the role, the style instructions, and any shipped proof-of-work build.
- It must return a subject, a body and a list of claims, each citing an evidence id.
- Each draft is checked. Problems marked ✗ block approval; problems marked ! are
  warnings:

  | Check | |
  |---|---|
  | Every claim cites evidence that exists for this company | ✗ |
  | At least one claim about the company | ✗ |
  | Body is 50–180 words | ✗ |
  | No banned phrases | ✗ |
  | No links except your profile links and your shipped build | ✗ |
  | Subject is 1–10 words | ! |
  | Two questions or fewer | ! |
  | Body doesn't start with its own greeting | ! |

- A draft with ✗ problems is regenerated once with the problems listed. If it still
  fails, it's saved and marked "needs fixes".
- The greeting is added by the tool: "Hi Priya," for a named person, "Hi there," for
  "Hiring Team" style names. The signature comes from `profile.toml`.
- Each draft records its experiment arm and prompt version.

### 4. Review

```bash
python pipeline.py review
```

For each draft you see:

- the full email, as it will be sent,
- a table of every claim beside the saved text it cites, so you can check it,
- any ✗ or ! problems, the experiment arm, and whether it's a proof-of-work pitch.

Then press **a** to approve, **e** to edit in `$EDITOR`, **r** to reject with a reason,
**s** to skip or **q** to quit. Edits are checked again. A draft with ✗ problems can't
be approved until you fix them. Rejecting sends the lead back to `ready`.

### 5. Send

```bash
python pipeline.py send                 # preview: who gets what
python pipeline.py send --send          # send, after confirming
python pipeline.py send --send -y --anytime -n 5
```

- Sends through Gmail SMTP with your resume attached.
- Stores each email's Message-ID, so a reply can be matched to that exact email.
- Only sends Tuesday to Thursday, 9:30–11:30 IST, unless you pass `--anytime`.
- At most 15 a day (`OUTREACH_DAILY_CAP`), with 30–90 seconds between emails.
- Skips any address that was already emailed, bounced, or asked not to be contacted.
- If one email fails, it's logged and the rest still go out.

### 6. Sync

```bash
python pipeline.py sync [--classify]
```

- Reads your Gmail inbox over IMAP (read-only, so nothing is marked as read).
- Replies are matched to a lead by, in order: the thread's Message-ID, the sender's
  address, or the company's domain (never for free providers like gmail.com).
- Bounces from mailer-daemon are matched by Message-ID or address. The address is
  then marked invalid.
- Out-of-office and other auto-replies are recorded but don't count as replies.
- With `--classify`, the model labels each reply positive, neutral or negative. A
  negative reply marks that person do-not-contact.
- You can also record an outcome by hand:

  ```bash
  python pipeline.py outcome 7 replied --sentiment positive --note "call on Friday"
  ```

---

## Leads and companies

```bash
python pipeline.py lead add COMPANY ROLE [--email] [--name] [--title] [--website] [--job-url] [--source]
python pipeline.py lead import leads.csv        # columns: Company, Role, Email, Name, Title, Website
python pipeline.py lead list [--status ready] [--company Acme]
python pipeline.py lead show 7                  # contact, drafts, sends, outcome and full history

python pipeline.py company show "Future AGI"    # details and every saved evidence item
python pipeline.py company set "Future AGI" --tier A --website futureagi.com --github future-agi
```

- Adding a lead that already exists returns the existing one.
- Shared inboxes (`support@`, `info@`, `contact@`…) are accepted but flagged, because
  a named person replies far more often.
- Tiers: **A** means you want this company most and it gets proof-of-work
  treatment, **B** means good, **C** is the default.

---

## Experiment loop

The experiment loop tests one change to your emails at a time and keeps what works.

```bash
python pipeline.py exp presets
python pipeline.py exp new --preset subject-style
python pipeline.py exp status
python pipeline.py exp conclude subject-style --winner casual --note "lowercase subjects won"
python pipeline.py exp list
```

### Presets

| Preset | Changes | Arms |
|---|---|---|
| `subject-style` | subject | `role-fit` (names the role and one fit) vs `casual` (2–4 lowercase words) |
| `opening` | opening | `observation` (a fact about them) vs `question` (a technical question) |
| `proof-lead` | proof | `production` (freelance production work first) vs `project` (best project first) |
| `length` | length | `short` (60–90 words) vs `standard` (120–160 words) |
| `ask` | ask | `consider` (consider me for the role) vs `walkthrough` (a 15-minute project walkthrough) |

Your own experiment:

```bash
python pipeline.py exp new --name tone --variable tone \
    --hypothesis "Warmer emails get more replies" \
    --arm "plain=Plain and direct, no small talk." \
    --arm "warm=Warm and a little informal, one friendly line."
```

### How it works

- **One experiment at a time.** Starting a second one is refused until the first is
  concluded, so each result has one cause.
- **Even split.** Each new draft is assigned to the arm with the fewest leads, and a
  lead keeps its arm if it's drafted again.
- **The arm's instruction goes into the prompt**, and the draft is tagged with it.
  Review shows the tag.
- **A send only counts as "no reply" after 7 days** (`OUTREACH_MATURE_DAYS`), so recent
  emails don't pull a rate down.
- **Winners are only named with enough data.** `status` says "too early" until every
  arm has 20 sends old enough to count. After that, it compares the top two arms with
  Fisher's exact test and only names a leader when p < 0.05.
- **You're told how much your sample can detect.** From an 8% reply rate, 25 sends
  per arm can only detect a jump to about 41%, and 50 per arm to about 30%. Test big
  changes, not small tweaks.
- **Bounces above 3%** trigger a warning to fix addresses first.
- **Concluding** makes the winning instruction the default for every later draft,
  whenever no experiment is running. It also appends the result table to `lessons.md`.
- **Lead order learns too.** Reply rates by lead source are smoothed (starting from
  a 10% guess, so one lucky reply doesn't dominate) and decide which ready leads are
  drafted first.

---

## Proof of work

For the companies you want most, build something small for them and lead the email
with it.

```bash
python pipeline.py company set "Future AGI" --tier A
python pipeline.py pow candidates                 # Tier A first, with evidence and GitHub
python pipeline.py pow pack "Future AGI"          # research + 3–5 build ideas
python pipeline.py pow start 3
python pipeline.py pow ship 3 --summary "Fixed trace-to-graph linking in traceAI" \
    --repo https://github.com/future-agi/future-agi/pull/1234 --hours 3.5
python pipeline.py draft                          # the next draft for them leads with the build
python pipeline.py pow list                       # board, and shipped this month vs a target of 5
python pipeline.py pow drop 4 --reason "needs a paid API key"
```

### Packs

`pow pack` researches the company first if it has fewer than 3 evidence items, or
always with `--refresh`. It then asks the model for ideas, each with a title, a kind,
what it is, why they'd care, the evidence it's based on, which of your projects it
builds on, estimated hours, steps, the deliverable and a one-line email opener.

Allowed kinds are `demo`, `oss_fix`, `teardown`, `eval` and `integration`. An idea is
dropped (and listed as dropped) if it:

- cites evidence that doesn't exist,
- is estimated at more than 4 hours,
- is an `oss_fix` that doesn't point at a real open issue, or
- has an unknown kind.

The model is also told not to propose anything that needs private access, sends heavy
traffic to their systems, probes their security or scrapes personal data.

The pack is written to `packs/<company>.md`, with every idea and its steps plus the
full evidence list with sources. Ideas are saved on the board, so repeating a title
doesn't duplicate it.

### Shipping

- `pow ship` needs a public `--repo` or `--demo` link.
- It moves the company to Tier A.
- The company's next draft opens with the build: what it is, the link and what it
  shows. The build's links are allowed in the email.
- `report` compares proof-of-work pitches with standard ones.

Note: an address that was already emailed isn't emailed again. To pitch a build at a
company you've written to before, add a different person there with `lead add`.

---

## Reports

```bash
python pipeline.py report
```

Shows sent, bounced, replied, positive and reply rate with a 95% confidence interval,
broken down by:

- **source** (internshala, manual, legacy…),
- **tier** (A/B/C),
- **pitch** (proof of work, standard, or the old script),
- **prompt version**.

The intervals are Wilson intervals, which stay honest at small numbers. With 12 sends
and no replies, the true rate could still be anywhere from 0% to 24%.

---

## Rules the tool enforces

- Every claim about a company cites a saved source, and a company with fewer than two
  sources isn't drafted.
- Addresses are only taken from pages that publish them. None are guessed.
- You approve every email, and drafts with blocking problems can't be approved.
- An address is never emailed twice. Bounced and do-not-contact addresses are never
  emailed again.
- At most 15 new emails a day, only on Tuesday to Thursday mornings (both can be
  overridden deliberately).
- Negative replies mark the person do-not-contact.
- Lead status only moves along the allowed paths.
- Secrets live in `.env`, never in code. `doctor` checks this.

---

## Configuration reference

All settings are environment variables and can go in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | – | OpenAI key |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Model for drafts, ideas and reply labels |
| `GMAIL_ADDRESS` | – | Sending account |
| `GMAIL_APP_PASSWORD` | – | Gmail App Password (SMTP and IMAP) |
| `GITHUB_TOKEN` | – | Optional, for a higher GitHub API limit |
| `OUTREACH_DAILY_CAP` | `15` | Most new emails per day |
| `OUTREACH_MATURE_DAYS` | `7` | Days before a send counts as "no reply" |
| `OUTREACH_DB` | `outreach.db` | Database path |
| `OUTREACH_HOME` | project folder | Where `.env`, `profile.toml`, `packs/` and `lessons.md` live |

---

## Data model

Everything is in one SQLite file, `outreach.db`.

| Table | Holds |
|---|---|
| `companies` | Name, website, GitHub org, tier |
| `contacts` | People and addresses, with address status and do-not-contact |
| `leads` | A company, contact and role, with its status and source |
| `evidence` | Facts with their URL, kind and fetch time |
| `drafts` | Subject, body, cited claims, experiment arm, check results, your decision |
| `messages` | What was sent, when, and its Message-ID |
| `outcomes` | Reply, bounce or auto-reply for each lead, with sentiment |
| `experiments`, `arms`, `assignments` | The experiment loop |
| `style_defaults` | Winning instructions from concluded experiments |
| `projects` | Proof-of-work ideas and builds |
| `events` | A history of every action, shown by `lead show` |

To look inside it directly: `sqlite3 outreach.db`. Back it up like any file you care
about. It's git-ignored because it holds contact data.

### What's kept out of git

`.gitignore` keeps secrets and personal data out of the repository:

- `.env`: API keys and the Gmail App Password (`.env.example` is committed instead)
- `outreach.db` and `data/legacy/`: leads, contacts, sent emails and replies
- `*.pdf`: your resume. Remove that line if the repository is private and you want it tracked.
- `packs/` and `lessons.md`: generated from your own outreach
- Python caches, virtual environments, and editor and OS files

---

## Project layout

```
.
├── pipeline.py            Entry point: the six stages, `run`, the dashboard and `doctor`
├── profile.toml           Your facts, proof points and signature
├── requirements.txt
├── .env.example           Template for .env (secrets, git-ignored)
├── Samarth_Deshpande_Resume.pdf
├── outreach/
│   ├── commands.py        lead, company, exp, pow, outcome, report and init commands
│   ├── config.py          Settings, .env loading, profile loading
│   ├── db.py              Schema, lead status rules, shared connection
│   ├── discover.py        Internshala scanning and contact discovery
│   ├── research.py        Site and GitHub research into evidence
│   ├── drafting.py        Grounded drafting and the draft checks
│   ├── review.py          The interactive review screen
│   ├── mail.py            Gmail sending, inbox sync and outcome matching
│   ├── experiments.py     The experiment loop and breakdowns
│   ├── proof.py           Proof-of-work packs, ideas and the build board
│   ├── stats.py           Wilson intervals, Fisher's exact test, sample sizes
│   ├── llm.py             The OpenAI wrapper
│   ├── legacy.py          Import of the old scripts' data
│   └── ui.py              Terminal styling
├── data/legacy/           The old scripts' CSV/JSON, imported by `init` (git-ignored)
├── tests/
│   ├── test_outreach.py   Module tests
│   └── test_cli.py        End-to-end tests of pipeline.py
├── outreach.db            Created by `init` (git-ignored)
├── packs/                 Created by `pow pack`
└── lessons.md             Created by `exp conclude`
```

`python -m outreach` runs the same CLI as `pipeline.py`. For a shorter command:

```bash
alias outreach="python $PWD/pipeline.py"
outreach --install-completion        # tab completion for commands and options
```

---

## Testing

```bash
python -m unittest discover tests
```

The tests use a fake model and a temporary database, and never touch the network,
OpenAI or Gmail. They cover:

- the statistics,
- lead status rules,
- experiment assignment and verdicts,
- draft checks and proof-of-work pitches,
- idea validation,
- reply and bounce matching,
- discovery parsing,
- the legacy import,
- the CLI end to end: the dashboard, runs, interactive review, failures and missing keys.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `OPENAI_API_KEY is not set` | Add it to `.env`. `doctor` lists everything missing. |
| `SMTPAuthenticationError` or IMAP login fails | Use a Gmail App Password, not your account password, and enable IMAP in Gmail settings. |
| "Outside the send window" | Wait until Tuesday to Thursday, 9:30–11:30 IST, or pass `--anytime`. |
| "has 0 evidence item(s)" | Set the website (`company set ID --website URL`), then `research ID`, or add a `--job-url`. |
| A company "has no website" | `company set ID --website example.com` |
| A draft "needs fixes" | Open `review`, press **e**, fix the ✗ items and save. |
| "already emailed this address" | By design. Add a different person at that company. |
| GitHub returns nothing | You've probably hit the 60-requests-an-hour limit. Set `GITHUB_TOKEN`. |
| The experiment says "too early" | Each arm needs 20 sends that are at least 7 days old. Keep sending. |
| `Unknown stage` | Stages are `discover`, `research`, `draft`, `review`, `send` and `sync`. |
