# PR & Code-Review Audit

A web app that answers: **how good are our pull requests and code reviews, per person and per project?**

1. It **collects** pull requests, commits and review comments from GitHub (plain code, no AI, read-only).
2. It **scores** each pull request (PR) with an AI model that follows written instructions you can edit.
3. It **shows** everything on a dashboard: scores per person, per project, issues found, 1:1 talking points.

Who sees what: **admins** run and configure everything, **viewers** (e.g. the office TV) see everyone read-only, and
**each developer** can sign in with GitHub and see only their own results.

> **Never been here before?** Read sections 1 to 3. Sections 4 onwards are for later.

---

## 1. Start it on your computer (10 minutes)

1. **Install Python 3.9 or newer** from https://www.python.org/downloads/ (tick *Add python.exe to PATH*).
2. **Only if you want AI scoring:** open a terminal in this folder and run `pip install -r requirements-llm.txt`.
3. **Create your settings file.** Copy `.env.example` to `.env` and fill in the lines marked in section 2.
   (`.env` stays on your computer; it is never committed.)
4. **Start it:** double-click **`run_dashboard.bat`**, or run `py audit.py serve --open`.
   Your browser opens **http://127.0.0.1:8765**. Keep the black window open; close it to stop.
5. **Load data:** click **Run** in the top bar and follow section 3.

On your own computer there is no login: the app only answers on `127.0.0.1`, and you are an admin.

## 2. The settings file (`.env`)

Only these matter to begin with:

| Line | What it is | Where to get it |
|---|---|---|
| `GITHUB_ORG` | Your organisation name, the `xyz` in `github.com/xyz` | your GitHub URL |
| `GITHUB_TOKEN` | A **read-only** key so the app can read your PRs. Locally you can instead put the token alone in a file named `.github_token` | GitHub → Settings → Developer settings → Fine-grained tokens. Resource owner: your org. Repository permissions, all *Read-only*: Contents, Pull requests, Metadata (and Commit statuses). If the org asks for approval, an owner approves it under Organization → Settings → Personal access tokens |
| `ANTHROPIC_API_KEY` | The key for AI scoring (**costs money per PR**). Not needed to collect data or for the free rule-based scoring | https://console.anthropic.com → API keys |
| `AUTO_REFRESH_HOURS` | Optional: keep the data up to date by itself (section 4) | e.g. `6` |

The app never shows these keys; the **Access** tab only says *set* or *not set*. The full list, with comments, is in
`.env.example`. Sign-in settings (GitHub OAuth, admins, TV password) are only needed on a shared server (section 5).

## 3. A tour of the screens

### Dashboard (what everyone sees)

| Where | What you get |
|---|---|
| **Top bar** | Period (1 / 3 / 6 / 12 months, all, custom) and project filter. Numbers update instantly. |
| **Left list** | Every person found in the repos, with PR count and overall score. |
| **Overview** | The home page. A team score ring and ten headline numbers (PRs, blockers, tests, rework, PR size, direct pushes...) each compared with the previous period; quality over time; score by area (radar); how scores are spread; issues by severity; what happened to review comments; projects and PR size against score; who is doing well and who could use coaching; reviewer quality; the PRs that need attention; and a colour-coded scorecard of every person. Click a project, person or PR to drill in. |
| **Person page** | Score ring, rank, comparison with the team average, radar and bars by area (the line marks the team), monthly trend, projects, PR outcomes, issues by severity, and tabs for **Pull requests**, **As reviewer**, **Direct pushes**. |
| **Click a PR** | 1:1 talking points, commit messages, code issues (and whether the reviewer caught them), the review conversation with each comment marked valid / nitpick / invalid and fixed / dismissed / open, and which model scored it. |
| **Standards** | The rules every PR is scored against, shown as a readable page. |

Scores are 1 to 5: **green 4 or more**, **amber 3 to 4**, **red below 3**.

### Run (admins): load or update data

1. **Pick repositories** from your organisation (use the filter box), or tick *use every repo of the organisation*.
2. **Choose the period** (how far back) and **what to do**:
   - *Pull from GitHub*: collects PRs, commits and reviews. Free, no AI.
   - *Scoring*: **LLM with our skill** (costs money), **Rule-based** (free, rough), or **Don't score** (collect only).
3. Press **Estimate cost**. It shows how many PRs would be scored and roughly what that costs per model.
   Press **Start run**. Set *Stop when spend reaches* if you want a safety limit.

The green/red chips at the top show what is configured. If you only want fresh data, pick *Don't score, collect only*.

### Jobs (admins): watch a run

A progress bar, the money spent, a log of what happened, and a **Cancel** button. One job runs at a time; waiting jobs
start by themselves, and a restart picks up where it stopped.

### Repos (admins): tracked repos and automatic refresh

*Tracked repos* are the ones kept up to date. **Automatic refresh** is explained in section 4.

### Skill & models (admins): how scoring works, in plain words

**You do not have to touch this.** The defaults work. Open it when you want to spend less, be stricter, or check
what the AI is told. It has three tabs, each with a help box at the top:

- **Models & routing: which AI model scores which PR.** Think of three reviewers: **Opus** (most careful, most
  expensive), **Sonnet** (balanced) and **Haiku** (fastest, cheapest). The list of rules decides who gets which PR.
  The rules are read **from the top; the first one that fits wins**, and the last rule is the catch-all.
  The default: tiny PRs (up to 120 lines) go to Haiku, big (800+ lines), shared or security-related PRs go to Opus,
  everything else to Sonnet. Examples:
  - *Spend less*: change the last rule ("Everything else") from Sonnet to Haiku, then **Save routing**.
  - *Be more careful on one project*: **Add rule**, put its repo name in *Repos*, choose Opus, and move it to the top.
  - *Not sure what a PR would get?* Use **Test** at the bottom: enter a size and press it.
  - **agent** = the style of review. `scorer` reads the PR in one go; `deep-scorer` reads large or sensitive PRs
    file by file (slower, costs more, more thorough).
  - **effort** = how hard the model thinks (low is cheaper and faster, high is more thorough).
- **Skill & agents: the written instructions the AI follows.** `SKILL.md` is how to work through a PR, `rubric.md` is
  what "good" means (the standards and weights), and `agents/…` define the reviewers. Click a file to edit it, use
  **Preview** to read it formatted, add a note, and **Save as new version**. Every score remembers which version made it,
  you can load or re-activate any old version, and **Back to repo version** returns to the shipped text.
  **Saving never re-scores old PRs by itself.** When you want the new instructions applied to old PRs, tick
  *re-score PRs scored with an older skill version* on the Run page.
- **Try on a PR: test before you trust it.** Choose a project and PR number (already collected) and press **Score it**.
  You see the AI's scores next to the stored ones. **Nothing is saved**; it costs a few cents. Tick the box to try your
  unsaved edit against the version in use.

### Access (admins): who can sign in

Shows what the server is configured with (set / not set) and a table of **people**: a GitHub login, id or email, a role
(admin / viewer / self) and which collected person's data they see. Add a row to let someone outside the org in, to
link a person who uses a different email, or to switch someone off.

## 4. Keep the data up to date automatically

If the settings are complete, you never need to press Run: the app refreshes by itself.

1. In `.env` set `GITHUB_TOKEN`, `GITHUB_ORG` and **`AUTO_REFRESH_HOURS=6`** (any number of hours), then restart.
2. About a minute after it starts, and then every 6 hours, it asks GitHub for everything that changed in the last
   `AUTO_REFRESH_MONTHS` (default 1) and stores it. With no tracked repos it uses **every active repo of the
   organisation**; if you track repos on the **Repos** tab, only those.
3. `AUTO_SCORE` says what happens to the new PRs: `none` (default: just fresh data, no cost, they show as
   "awaiting evaluation"), `heuristic` (free rough score) or `llm` (full AI score, needs `ANTHROPIC_API_KEY`;
   `AUTO_MAX_USD`, default 5, is the cap per run).

You can see it, change it or switch it off on **Repos → Automatic refresh** (settings saved there win over `.env`;
**Go back to the .env setting** undoes that). The status shows when it last ran and when it runs next.
It never fails because of a missing key: if `llm` is chosen without a key it only collects.

## 5. Put it on a server for the team

One container runs everything: dashboard, admin screens and the background worker.

1. **Put the code in git** (`.gitignore` already keeps `.env`, the token and the database out). Never commit `audit.db`.
2. **Optional: bring your existing results.** `py audit.py backup --out data/audit.db`, then copy that file to the
   server's `data/` folder over a private channel. Starting empty also works.
3. **Create the GitHub OAuth app** (lets people sign in with GitHub): [docs/github-oauth-setup.md](docs/github-oauth-setup.md).
4. **Fill in `.env`** on the server (see `.env.example`, block 4): `PUBLIC_URL`, the OAuth id and secret,
   `AUDIT_SESSION_SECRET` (a long random string), `ADMIN_USERS`, optionally `VIEWER_PASSWORD`.
5. **Start:** `docker compose up -d --build`. Open `PUBLIC_URL`. Put **HTTPS** in front (nginx/Caddy or the
   cloud's load balancer). `GET /healthz` is the health check. Without Docker: `pip install -r requirements-llm.txt`,
   set the same variables, `python3 audit.py serve --host 0.0.0.0`.

The server refuses to listen on a network address if no sign-in method is configured.

| Role | Sees | Can do | How someone gets it |
|---|---|---|---|
| **Admin** | everything | run jobs, choose repos, change models, edit the skill, manage people | GitHub login in `ADMIN_USERS`, or a row in **Access** |
| **Viewer** | every project and person, read-only | nothing | the shared `VIEWER_PASSWORD`, a login in `VIEWER_USERS`, or a row in **Access** |
| **Self** | only their own PRs, scores and reviews | nothing | signing in with GitHub as a person found in the repos (matched by GitHub id or a verified email; org members match automatically; others need a row in **Access**) |

"Self" is enforced on the server: other people's pages are refused, not just hidden. On a shared PR a person sees the
other committers' names but not their scores. Someone who is not matched gets an "access denied" page.

**Office TV:** open the server's `/login`, enter the viewer password and keep *TV mode* ticked (or open `/?kiosk=1`;
add `&rotate=20` to cycle the overview and each person every 20 seconds). It refreshes itself every 5 minutes and stays signed in for
30 days. Everyone who knows the password sees all scores, so change it when someone leaves.

## 6. When something goes wrong

| What you see | Why / what to do |
|---|---|
| Run page: "set GITHUB_ORG on the server" | Add `GITHUB_ORG=<your org>` to `.env` and restart. You can still type repos as `owner/name`. |
| Chip "✗ GitHub token" | `GITHUB_TOKEN` is missing or the `.github_token` file is empty. "Bad credentials" in a job log means the token is wrong or expired: make a new one. |
| Job failed: "ANTHROPIC_API_KEY is not set" | Add the key to `.env` and restart, or choose rule-based / collect only. |
| `.env` changes do nothing | Restart the app: it reads `.env` when it starts. Variables set in the real environment win over `.env`. |
| Estimate says 0 PRs | Nothing to score yet: collect first, or everything in that period is already scored. |
| Sign-in: "redirect_uri is not associated…" | The OAuth app's callback URL must be exactly `PUBLIC_URL` + `/auth/callback`. |
| Everyone is signed out after a restart | Set `AUDIT_SESSION_SECRET` to a fixed value. |
| "access denied" for a developer | They are not matched. Add a row in **Access** (their GitHub login and which person they are). |
| Server won't start on a network address | Configure a sign-in method (OAuth or `VIEWER_PASSWORD`). |
| Read-only server says the database predates the app | Start once without `AUDIT_READONLY=1` so it can upgrade the database. |

## 7. Reference

### How a PR gets its score

Four areas, each 1 to 5, weighted: commit messages 15%, code quality 40%, validation (CI, tests) 20%, handling review
feedback 25%. Everything is spelled out in `skills/pr-scoring/rubric.md` (also under **Standards** in the app).
Scores come from one of three **engines**; the dashboard shows which:

| Engine | Needs | Quality |
|---|---|---|
| **LLM with the skill** | `ANTHROPIC_API_KEY` | a senior-reviewer read of the diff; routing picks the model |
| **Claude Code agents** (how the first data was made) | a Claude Code session | same review |
| **Rule-based** | nothing, free | objective checks only (commit format, CI, tests, description, size, self-merge, hard-coded secrets, risky APIs). It does not read the logic, so code quality is capped at 4 and marked as an estimate |

Batch mode (a Run option) sends the PRs to the Message Batches API: **half price**, results usually within an hour.

**Which PRs count:** every PR with activity in the period (open, merged, closed unmerged), with all its commits.
Branch promotions (e.g. `dev → uat`) are listed but not scored; bot PRs are skipped. Re-running a period is safe:
nothing is stored twice, and a PR is only re-scored when it gets new commits (or you ask for a re-score).

**Shared PRs:** when several people commit to one PR (or use `Co-authored-by:`), each answers for their own commits;
the PR owner also answers for validation and review response. AI co-authors (Claude, Copilot, Cursor) are not scored.

**People:** found automatically; the key is the GitHub id. Fix duplicates with
`py audit.py merge-users email:x@y.com gh:12345`; list everyone with `py audit.py people`.

### Command line (same engine as the Run page)

```bash
py audit.py score --engine llm --collect --repos org/a org/b --months 3 --max-usd 5
py audit.py score --engine llm --estimate --repos org/a          # what it would cost
py audit.py score --engine llm --batch --repos org/a             # half price, slower
py audit.py score --engine heuristic --months 3                  # free rule-based
py audit.py collect --org myorg --months 3                       # collect only
py audit.py backup --out audit-backup.db                         # consistent copy of the database
py audit.py status | people | pending | ingest | merge-users | --help
```

### All settings

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN`, `GITHUB_ORG` | collecting; repo picker; org check at sign-in |
| `ANTHROPIC_API_KEY`, `AUDIT_CONCURRENCY` | LLM scoring; calls in flight at once (1 to 8, default 3) |
| `AUTO_REFRESH_HOURS`, `AUTO_REFRESH_MONTHS`, `AUTO_SCORE`, `AUTO_MAX_USD` | automatic refresh (section 4) |
| `PUBLIC_URL`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `AUDIT_SESSION_SECRET` | GitHub sign-in |
| `ADMIN_USERS`, `VIEWER_USERS`, `VIEWER_PASSWORD` | roles and the TV password |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD`, `DASHBOARD_USERS` | HTTP Basic logins for scripts (they are admins) |
| `HOST`, `PORT`, `AUDIT_DB`, `AUDIT_READONLY=1` | where it listens, which database, viewing-only mode |
| `AUDIT_FALLBACKS=0` | don't let the API retry a refused request on a fallback model |

### Files

| File / folder | Purpose |
|---|---|
| `run_dashboard.bat` | double-click to start locally |
| `.env`, `.env.example` | your settings (not committed), and the annotated template |
| `audit.py`, `envfile.py` | command line; `.env` reader |
| `server.py`, `auth.py` | web server, API, roles; sign-in, sessions, CSRF |
| `jobs.py` | background worker: collect, score, estimate, batch, cancel, resume, automatic refresh |
| `scoring.py`, `routing.py`, `skill.py` | scoring engines; model rules and prices; the skill loader |
| `control.py`, `store.py`, `schema.sql`, `github.py` | settings and jobs; database and dashboard queries; table layout; read-only GitHub client |
| `skills/pr-scoring/` | the scoring skill: `SKILL.md`, `rubric.md`, `agents/`, `models.json` (model prices, default rules) |
| `STANDARDS.md` | pointer to `skills/pr-scoring/rubric.md` |
| `dashboard/` | `index.html` (pages), `charts.js` (SVG chart library), `admin.js` (admin screens), `login.html` |
| `docs/github-oauth-setup.md` | creating the GitHub OAuth app |
| `Dockerfile`, `docker-compose.yml`, `requirements-llm.txt` | hosting |
| `tests/` | offline self-tests with fake GitHub, AI and sign-in: `py tests\app_test.py` |
| `audit.db`, `data/`, `backups/`, `bundles/`, `evals/`, `.github_token` | your data and working files (not committed) |
