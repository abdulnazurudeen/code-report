# PR & Code-Review Audit

Collects GitHub PRs, commits, reviews and review threads for any repos and period into **SQLite (`audit.db`)**,
has Claude score every PR against **STANDARDS.md**, and shows everything in a local **dashboard**.
Needs only Python 3.9+ — no packages to install.

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
issues traced to their commits. See STANDARDS.md §6c.

**Whole organisation.** `py audit.py repos --org myorg --months 3` lists repos pushed in the period;
`py audit.py collect --org myorg --months 3` collects all of them.

## Deploy it (hosted dashboard for the team)

The app is one small Python process (standard library only) that serves the dashboard from `audit.db`.

**1. Put the code in git** — `.gitignore` already keeps the token, `.env`, the database and working files out.
Never commit `audit.db`: it holds people's scores and security findings.

**2. Ship the existing analysis** — make a consistent copy and move it to the server:

```bash
py -3 audit.py backup --out data/audit.db
```

**3. Run it** (any Linux VM / Azure App Service for Containers / ECS / Cloud Run with a persistent volume):

```bash
cp .env.example .env          # set DASHBOARD_PASSWORD (or DASHBOARD_USERS=alice:pw,bob:pw)
docker compose up -d --build  # dashboard on http://<server>:8765, database in ./data/audit.db
```

Without Docker: `HOST=0.0.0.0 DASHBOARD_PASSWORD=... AUDIT_DB=/path/audit.db python3 audit.py serve`.

- The server **refuses to listen on a network address without a login** (people's scores and security findings are
  in it). `--public` overrides that; don't use it on the internet.
- Login is HTTP Basic: put **HTTPS** in front (nginx/Caddy, or the cloud load balancer). Health check: `GET /healthz`.
- `AUDIT_READONLY=1` opens the database read-only, for a viewing-only server whose data you replace by copying a new
  `audit.db` over it.

| Variable | Purpose |
|---|---|
| `DASHBOARD_PASSWORD` / `DASHBOARD_USER` | one shared login (user defaults to `admin`) |
| `DASHBOARD_USERS` | several logins: `alice:pw1,bob:pw2` |
| `HOST`, `PORT` | where to listen (container: `0.0.0.0:8765`) |
| `AUDIT_DB` | database path (container: `/data/audit.db`) |
| `AUDIT_READONLY=1` | never write to the database |
| `GITHUB_TOKEN`, `GITHUB_ORG`, `AUDIT_MONTHS` | for refreshing data (read-only token) |
| `ANTHROPIC_API_KEY`, `AUDIT_MODEL`, `AUDIT_EFFORT` | only for `score --engine anthropic` |

## Scoring without Claude Code

Three ways to score a PR; the dashboard shows which one produced each score.

| Engine | Needs | Quality | Command |
|---|---|---|---|
| **Claude Code agents** (how the current data was made) | a Claude Code session | full senior-reviewer read of the diff | the *pr-review-audit* skill (`pending` → evals → `ingest`) |
| **Anthropic API** | `ANTHROPIC_API_KEY` (pay per use), `pip install -r requirements-llm.txt` | same review, unattended | `python audit.py score --engine anthropic --months 3` |
| **Rule-based** | nothing — no LLM, no token, free | objective checks only: commit format, CI, tests, description, size, self-merge / rubber-stamp approvals, hard-coded secrets, risky APIs, debug code. It does **not** read the logic, so code quality is capped at 4 and marked as an estimate | `python audit.py score --engine heuristic --months 3` |

Scheduled refresh on the server (e.g. nightly cron):

```bash
docker compose run --rm refresh        # collect new GitHub activity + rule-based scores (free)
docker compose run --rm refresh-llm    # collect + Anthropic API scores (needs ANTHROPIC_API_KEY)
```

An API run also upgrades PRs that only have a rule-based score. Rule-based scores are not picked up by `pending`,
so if you want Claude Code agents to score some PRs, run them before the rule-based pass.

## Files

| File / folder | Purpose |
|---|---|
| `run_dashboard.bat` | Double-click to open the dashboard |
| `audit.py` | Command line + dashboard server |
| `github.py` | Read-only GitHub client |
| `store.py` | Database writes (no duplicates) and dashboard queries |
| `scoring.py` | Rule-based and Anthropic-API scoring engines |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Hosting |
| `requirements-llm.txt` | Optional: `anthropic` SDK for API scoring |
| `data/` | Database copy for the hosted dashboard (not in git) |
| `schema.sql` | Database structure |
| `STANDARDS.md` | Scoring rubric — edit freely, then bump `RUBRIC_VERSION` in `store.py` to re-score |
| `dashboard/index.html` | Dashboard UI |
| `tests/e2e_test.py` | Offline self-test with fake GitHub data (`py tests\e2e_test.py`) |
| `audit.db` | Your data (created on first run) |
| `bundles/`, `evals/` | Working files for scoring (created automatically) |
| `.github_token` | Your token (keep private) |
