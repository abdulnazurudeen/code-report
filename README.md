# PR & Code-Review Audit

A self-contained web app. An admin picks repos and a period in the UI; the app pulls PRs, commits, reviews and review
threads from GitHub into **SQLite (`audit.db`)** with plain code, then scores every PR with an LLM following **our own
scoring skill** (`skills/pr-scoring/`, editable, with per-case model routing), and shows everything in a role-based
**dashboard** (admin / viewer / self). Viewing and rule-based scoring need only Python 3.9+; LLM scoring needs the
`anthropic` package and an API key.

---

## Run the dashboard (Windows)

**Easiest:** double-click **`run_dashboard.bat`** in this folder.
It starts the dashboard and opens **http://127.0.0.1:8765** in your browser. Keep the black window open while you use it; close it to stop.

**From a terminal** (Command Prompt or PowerShell):

```bat
cd C:\Users\Nazurudeen\projects\pr-review
py audit.py serve --open
```

(If `py` isn't found, use `python` instead. Use `--port 9000` if 8765 is busy.)

**First time only — install Python:** download from https://www.python.org/downloads/ and tick
**“Add python.exe to PATH”** during setup.

### Using the dashboard

| Where | What you get |
|---|---|
| **Top bar** | Period (1 / 3 / 6 / 12 months, all, custom dates) and project filter. All numbers update instantly. |
| **Left list** | Every person found in the repos, with PR count and overall score. Search by name. |
| **Team overview** | One row per person — scores per area, reworked PRs, ignored review comments, direct pushes, reviews given. Click a column to sort, a row to open the person. |
| **Person page** | Overall score, score per area, monthly trend, projects worked on, and tabs for **Pull requests**, **As reviewer**, **Direct pushes**. |
| **Click a PR** | 1:1 talking points, commit messages, code issues found (and whether the reviewer caught them), full review conversation with each comment marked *valid / nitpick / invalid* and *fixed / explained / dismissed / open*, reviewer assessment, and links to later fixes or reverts. |
| **Standards** | The rubric every PR is scored against. |

Score colours: **green ≥ 4**, **amber 3–4**, **red < 3** (scale 1–5).
The dashboard only reads `audit.db`; it is safe to open while Claude is updating data (refresh the page to see new results).

---

## Updating the data

Ask Claude (with the *pr-review-audit* skill): **“Run the code review audit for org/repo-a and org/repo-b, last 3 months.”**
Claude runs the steps below, scores every new or changed PR, and the dashboard picks it up.
The skill lives in `C:\Users\Nazurudeen\.claude\skills\pr-review-audit\SKILL.md`, so it works from any Claude Code session.

Manual equivalent:

```bat
py audit.py collect --repos org/repo-a org/repo-b --months 3     REM or --since 2026-05-01 --until 2026-11-03
py audit.py pending --months 3        REM writes bundles\ for PRs not yet scored
REM   -> Claude evaluates each bundle and writes evals\<same name>.json
py audit.py ingest                    REM loads the scores into audit.db
py audit.py status                    REM counts and what is still pending
```

**Which PRs are included:** every PR with activity in the period — **open, merged, and closed without merging** —
including PRs opened earlier but merged, closed or given new commits inside the period, with **all** their commits
(large PRs are paged; GitHub itself caps a PR's commit list at 250). Dates in the dashboard use each PR's
*last activity*. Branch promotions (e.g. `dev-uae → uat`) are listed but not scored; bot PRs are skipped.

Re-running any period is safe: PRs, commits and comments are stored once (by their GitHub IDs), and a PR is
only re-scored when it gets new commits or the rubric version changes.

---

## GitHub access (read-only)

The token lives in **`.github_token`** in this folder (one line, nothing else). Never share or commit it.
Fine-grained token settings:

- **Resource owner:** your organization · **Repository access:** all or selected repos
- **Repository permissions (all Read-only):** `Contents`, `Pull requests`, `Metadata`, optionally `Commit statuses`
- If your org requires approval, an owner approves it under *Organization → Settings → Personal access tokens → Pending requests*.

Check the token works: `py audit.py collect --repos org/some-repo --months 1`.
“Bad credentials” = token wrong/expired/revoked → generate a new one and replace the file contents.

---

## People

Users are discovered automatically. Unique key = GitHub numeric user ID (`gh:12345`, survives username changes).
Commit emails are linked by GitHub; personal emails used inside someone’s PR are linked when they clearly match
the author. Fix anything left over with `py audit.py merge-users email:x@y.com gh:12345`. Bots are excluded.
`py audit.py people` lists everyone with their aliases and flags unlinked commit emails or duplicate names.

**Shared PRs.** When several people commit to one PR (or a commit has `Co-authored-by:`), each person answers
for their own commits: every committer is scored on the commit messages and code quality of their own commits,
and the PR owner also answers for validation and review response. Merge commits that only bring a base branch
in are ignored. The person page shows PRs they own and other people's PRs they committed to, plus the code
issues traced to their commits. See skills/pr-scoring/rubric.md §6c.

**Whole organisation.** `py audit.py repos --org myorg --months 3` lists repos pushed in the period;
`py audit.py collect --org myorg --months 3` collects all of them.

## Deploy it (a web app for the team)

One container (or one Python process) runs the dashboard, the admin screens and a background worker that pulls from
GitHub and scores. Keys are **environment variables only**; they are never typed into the UI or shown in it.

**1. Put the code in git.** `.gitignore` keeps the token, `.env`, the database and working files out.
Never commit `audit.db`: it holds people's scores and security findings.

**2. (Optional) ship your existing analysis.** Make a consistent copy and put it on the server as `data/audit.db`:

```bash
py -3 audit.py backup --out data/audit.db
```

Starting empty also works: use **Run** in the app to collect and score.

**3. Create the GitHub OAuth app** for sign-in: step by step in [docs/github-oauth-setup.md](docs/github-oauth-setup.md).

**4. Configure and start:**

```bash
cp .env.example .env     # fill in the keys, org, OAuth app, ADMIN_USERS, VIEWER_PASSWORD
docker compose up -d --build   # app on http://<server>:8765, database in ./data/audit.db
```

Without Docker: `pip install -r requirements-llm.txt`, export the same variables, then `python3 audit.py serve --host 0.0.0.0`.
Put **HTTPS** in front (nginx/Caddy or the cloud load balancer); `GET /healthz` is the health check.
The server **refuses to listen on a network address without a login method** configured.

### Who can do what

| Role | Sees | Can do | How someone gets it |
|---|---|---|---|
| **Admin** | everything | run / cancel jobs, pick and track repos, change model routing, edit the skill and agents, try on a PR, manage people and access | GitHub login in `ADMIN_USERS`, or a row in **Access** with role admin |
| **Viewer** | every project and person, read-only | nothing | the shared `VIEWER_PASSWORD` (office TV), GitHub login in `VIEWER_USERS`, or a row with role viewer |
| **Self** | only their own PRs, scores, reviews and talking points | nothing | sign in with GitHub as a person found in the repos: matched by GitHub id or a verified email; org members (`GITHUB_ORG`) match automatically, anyone else needs a row in **Access** |

Self is enforced on the server (other people's pages, PRs and scores are refused, not just hidden). On a shared PR a
person sees the other committers' names but not their scores. A person who only *reviewed* a PR sees just their own
reviewer assessment of it. Someone who is not matched and has no role gets an "access denied" page; an admin can switch
anyone off in **Access** and their session ends on the next request.

**Office TV:** open `/login`, enter the viewer password and keep "TV mode" ticked, or go to `/?kiosk=1` (add
`&rotate=20` to cycle through people every 20 seconds). The screen refreshes itself every 5 minutes and the session lasts
30 days. The password is shared, so change it when someone leaves.

**Local use:** with no login configured the app only listens on `127.0.0.1` and everyone is admin
(`run_dashboard.bat` does this).

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | read-only token (Contents, Pull requests, Metadata, Commit statuses) used to collect |
| `GITHUB_ORG` | default organisation for the repo picker, and the org check for sign-in |
| `ANTHROPIC_API_KEY` | needed for LLM scoring (`pip install -r requirements-llm.txt`) |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `PUBLIC_URL` | GitHub sign-in (`PUBLIC_URL` = the address people open) |
| `AUDIT_SESSION_SECRET` | signs login cookies; set a long random value, or logins end at every restart |
| `ADMIN_USERS`, `VIEWER_USERS` | GitHub logins, comma separated |
| `VIEWER_PASSWORD` | shared password for read-only screens (throttled: 5 wrong tries, 10 minutes) |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD`, `DASHBOARD_USERS` | HTTP Basic logins for scripts / local use; they are admins |
| `HOST`, `PORT`, `AUDIT_DB` | where to listen, which database (container: `0.0.0.0:8765`, `/data/audit.db`) |
| `AUDIT_CONCURRENCY` | scoring calls in flight at once (default 3, max 8) |
| `AUDIT_READONLY=1` | viewing-only server: no jobs, no admin changes; the database must already be migrated |
| `AUDIT_FALLBACKS=0` | don't let the API retry a refused request on a fallback model |

## Running audits from the app (admins)

Top bar (admins only): **Run · Jobs · Repos · Skill & models · Access**.

1. **Run**: pick repositories (listed from your org; filter, "only pushed in the period"), pick the period, choose what to
   do (collect from GitHub, then score with the LLM skill / rule-based / not at all), set optional limits (stop after N PRs,
   stop at a spend cap), press **Estimate cost**, then **Start run**. Collecting is plain code with no LLM; only scoring
   calls the model. "Batch mode" uses the Message Batches API: half price, results usually within an hour.
2. **Jobs**: live progress, tokens and cost, a log, **Cancel**. One job runs at a time; the queue survives a restart and
   a batch is picked up again instead of being paid for twice.
3. **Repos**: tracked repos (the default for runs and for the schedule), pause / remove, and an optional **scheduled
   refresh** (every N hours, with a spend cap).
4. **Skill & models**: see below.
5. **Access**: server configuration status (set / not set; never the values) and the people table: GitHub login / id /
   email / name, role, and which collected person they are, active on/off.

### Which model scores which PR

**Skill & models → Models & routing** is an ordered list of rules; the first match wins and the last rule is the default.
A rule can look at lines changed, files, shared PR, truncated diff, repo (globs), changed paths (regex), state and how
it was scored before, and chooses an **agent**, a **model** and an **effort**. Defaults: security-sensitive paths go to
the investigating agent on Opus; 800+ line, shared and truncated PRs to Opus; PRs of 120 lines or less to Haiku; the rest
to Sonnet. Prices used for the estimate are in `skills/pr-scoring/models.json`; the **Estimate** shows the cost per model.

### The scoring skill (fine-tune it)

The instructions the model follows live in `skills/pr-scoring/`:

| File | What it is |
|---|---|
| `SKILL.md` | the procedure and the rules the importer depends on |
| `rubric.md` | the standards and weights (the old `STANDARDS.md` now points here) |
| `agents/scorer.md` | default agent: one call per PR (`mode: single`) |
| `agents/deep-scorer.md` | investigating agent (`mode: agentic`): reads the diff file by file with tools, then scores |
| `models.json` | model prices and the default routing rules |

Edit them in **Skill & models → Skill & agents** (saved as a new *version* with a short hash; reset any time to the repo
files) and add your own agents (front matter: `mode`, `effort`, `max_turns`, `max_diff_chars`). Every score records the
model, agent and skill version that made it. **Editing the skill does not re-score anything by itself**; tick "re-score PRs
scored with an older skill version" on **Run** when you want that. **Try on a PR** scores one collected PR with the
editor's unsaved text, shows it next to the active version and the stored score, and saves nothing.
(The JSON the importer reads is defined in code, `scoring.EVAL_SCHEMA`, not in the editable skill.)

## Scoring engines

| Engine | Needs | Quality | Where |
|---|---|---|---|
| **LLM with the skill** (default in the app) | `ANTHROPIC_API_KEY`, `pip install -r requirements-llm.txt` | senior-reviewer read of the diff; routing picks the model | **Run**, or `python audit.py score --engine llm` |
| **Claude Code agents** (how the first data was made) | a Claude Code session | same review | the *pr-review-audit* skill (`pending` → evals → `ingest`) |
| **Rule-based** | nothing: no LLM, no token, free | objective checks only; does **not** read the logic, so code quality is capped at 4 and marked as an estimate | **Run**, or `python audit.py score --engine heuristic` |

An LLM run also upgrades PRs that only have a rule-based score. Rule-based scores are not picked up by `pending`, so if
you want Claude Code agents to score some PRs, run them before the rule-based pass.

## Command line

```bash
py audit.py score --engine llm --collect --repos org/a org/b --months 3 --max-usd 5   # same pipeline as the Run page
py audit.py score --engine llm --estimate --repos org/a                                # what it would cost
py audit.py score --engine llm --batch --repos org/a                                   # half price, asynchronous
py audit.py collect|pending|ingest|status|people|backup|merge-users ...                # see `py audit.py --help`
```

## Files

| File / folder | Purpose |
|---|---|
| `run_dashboard.bat` | Double-click to open the dashboard locally |
| `audit.py` | Command line |
| `server.py` | Web server: pages, JSON API, login, admin endpoints, role checks |
| `auth.py` | Sessions, GitHub OAuth, roles, viewer password, CSRF |
| `jobs.py` | Background worker: collect → score, estimate, batch, cancel, resume, try-on-PR, schedule |
| `scoring.py` | Rule-based engine and the Anthropic-API engine (single call, investigating agent, batch) |
| `routing.py` | Model / effort / agent rules, model prices, cost estimate |
| `skill.py` | Loads, validates and versions the scoring skill |
| `control.py` | Settings, tracked repos, people table, job records |
| `github.py` | Read-only GitHub client |
| `store.py` | Database writes (no duplicates) and dashboard queries, including the "self" redaction |
| `schema.sql` | Database structure |
| `skills/pr-scoring/` | The scoring skill: `SKILL.md`, `rubric.md`, `agents/`, `models.json` |
| `STANDARDS.md` | Pointer to `skills/pr-scoring/rubric.md` |
| `dashboard/` | `index.html` (dashboard), `admin.js` (admin screens), `login.html` |
| `docs/github-oauth-setup.md` | Creating the GitHub OAuth app |
| `Dockerfile`, `docker-compose.yml`, `.env.example`, `requirements-llm.txt` | Hosting |
| `tests/e2e_test.py`, `tests/app_test.py` | Offline self-tests (fake GitHub, fake LLM, fake OAuth): `py tests\app_test.py` runs both |
| `data/` | Database copy for the hosted app (not in git) |
| `audit.db`, `bundles/`, `evals/`, `.github_token` | Your data and working files (not in git) |
