"""Database layer: idempotent writes (collector / evaluator) and read queries (dashboard)."""
import sqlite3, json, datetime
from pathlib import Path

RUBRIC_VERSION = "v1"
WEIGHTS = {"commit_msg": 0.15, "code_quality": 0.40, "validation": 0.20, "review_response": 0.25}
SCHEMA = Path(__file__).with_name("schema.sql")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def connect(path, readonly=False, migrate=True):
    """migrate=False: plain connection for per-request use once the server has migrated at start-up
    (skips the schema script, which would otherwise take a write lock on every request)."""
    if readonly:                                  # hosted dashboard: never writes, never migrates
        db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    # PERSIST journal: never deletes files, so it also works on synced/mounted folders. It is a per-connection
    # setting, so every connection (not only the migrating one) must ask for it.
    db.execute("PRAGMA journal_mode = PERSIST")
    if not migrate:
        return db
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    cols = {r[1] for r in db.execute("PRAGMA table_info(pull_requests)")}
    for col, ddl in (("head_branch", "TEXT"), ("kind", "TEXT DEFAULT 'change'"), ("merged_by_uid", "TEXT"), ("activity_at", "TEXT")):
        if col not in cols:                      # upgrade databases created by an older version
            db.execute(f"ALTER TABLE pull_requests ADD COLUMN {col} {ddl}")
    if "engine" not in {r[1] for r in db.execute("PRAGMA table_info(pr_evaluations)")}:
        # who produced the score: claude-code (agent session), anthropic-api, or heuristic (no LLM)
        db.execute("ALTER TABLE pr_evaluations ADD COLUMN engine TEXT DEFAULT 'claude-code'")
    ev_cols = {r[1] for r in db.execute("PRAGMA table_info(pr_evaluations)")}
    for col, ddl in (("model", "TEXT"), ("agent", "TEXT"), ("skill_hash", "TEXT"),
                     ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"), ("cost_usd", "REAL")):
        if col not in ev_cols:                   # which model/agent/skill version produced a score, and what it cost
            db.execute(f"ALTER TABLE pr_evaluations ADD COLUMN {col} {ddl}")
    db.execute("DROP VIEW IF EXISTS v_pr")
    db.executescript(SCHEMA.read_text(encoding="utf-8").split("-- Latest evaluation for the PR")[1].split("\n", 1)[1])
    db.execute("INSERT INTO meta VALUES ('rubric_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (RUBRIC_VERSION,))
    db.commit()
    return db


# ---------------------------------------------------------------- users
USER_REF_COLUMNS = [("pull_requests", "author_uid"), ("commits", "author_uid"), ("reviews", "reviewer_uid"),
                    ("comments", "author_uid"), ("review_threads", "resolved_by_uid"),
                    ("reviewer_evaluations", "reviewer_uid"), ("user_aliases", "uid"),
                    ("commit_coauthors", "uid"), ("pr_contributions", "uid")]


def merge_users(db, from_uid, into_uid):
    """Fold one user into another (e.g. an unlinked commit email that belongs to a GitHub user)."""
    if from_uid == into_uid:
        return
    for table, col in USER_REF_COLUMNS:
        db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (into_uid, from_uid))
    db.execute("DELETE FROM reviewer_evaluations WHERE reviewer_uid=?", (from_uid,))
    db.execute("DELETE FROM commit_coauthors WHERE uid=?", (from_uid,))
    db.execute("DELETE FROM pr_contributions WHERE uid=?", (from_uid,))   # both had a row on the same PR: keep into's
    db.execute("DELETE FROM users WHERE uid=?", (from_uid,))


AI_AGENT_LOGINS = {"claude", "cursoragent", "copilot", "copilot-swe-agent", "devin-ai-integration", "codex"}
AI_AGENT_DOMAINS = {"anthropic.com", "cursor.com", "cursor.sh", "openai.com"}


def resolve_user(db, gh_id=None, login=None, name=None, email=None, is_bot=False):
    """Return the uid for a GitHub actor or commit author, creating/merging as needed.
    Placeholder users ('email:'/'login:') are folded into the real GitHub user as soon as we see the link."""
    if login and login.endswith("[bot]"):
        is_bot = True
    # AI coding assistants show up as commit co-authors; the human committer answers for that code
    if (login or "").lower() in AI_AGENT_LOGINS or (email or "").lower().split("@")[-1] in AI_AGENT_DOMAINS:
        is_bot = True
    known = [r["uid"] for a in (login, email) if a
             for r in db.execute("SELECT uid FROM user_aliases WHERE alias=?", (a,))]
    if gh_id is not None:
        uid = f"gh:{gh_id}"
    elif known:
        uid = next((k for k in known if k.startswith("gh:")), known[0])
    elif login:
        uid = f"login:{login}"
    elif email:
        uid = f"email:{email.lower()}"
    else:
        db.execute("INSERT OR IGNORE INTO users(uid, name) VALUES ('unknown', 'Unknown / deleted user')")
        return "unknown"
    db.execute("INSERT INTO users(uid, login, name, is_bot) VALUES (?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
               "login=COALESCE(excluded.login, users.login), name=COALESCE(users.name, excluded.name), "
               "is_bot=max(users.is_bot, excluded.is_bot)", (uid, login, name or (None if login else email), int(is_bot)))
    for prev in set(known):
        if prev != uid and not prev.startswith("gh:"):
            merge_users(db, prev, uid)          # we just learned who this email/login really is
    for alias in (login, email):
        if alias:
            db.execute("INSERT INTO user_aliases(alias, uid) VALUES (?,?) ON CONFLICT(alias) DO UPDATE SET uid=excluded.uid "
                       "WHERE user_aliases.uid NOT LIKE 'gh:%' OR excluded.uid = user_aliases.uid", (alias, uid))
    return uid


def link_email_to_author(db, email_uid, author_uid, commit_name=None):
    """A commit inside X's PR was made from an email GitHub doesn't link to any account (e.g. a personal
    address). Fold it into X only when the email or commit name clearly matches X's login/name."""
    a = db.execute("SELECT login, name FROM users WHERE uid=?", (author_uid,)).fetchone()
    if not a:
        return email_uid
    email = email_uid[6:]
    hay = f"{email.split('@')[0]} {commit_name or ''}".lower().replace(".", " ").replace("_", " ").replace("-", " ")
    keys = {k.lower() for k in (a["login"], a["name"]) if k and len(k) >= 3}
    keys |= {w.lower() for w in (a["name"] or "").split() if len(w) >= 3}
    if any(k.replace("-", " ") in hay for k in keys):
        merge_users(db, email_uid, author_uid)
        db.execute("INSERT INTO user_aliases(alias, uid) VALUES (?,?) ON CONFLICT(alias) DO UPDATE SET uid=excluded.uid",
                   (email, author_uid))
        return author_uid
    return email_uid


def actor_uid(db, actor):
    """actor = GraphQL author object {login, databaseId?, __typename?} or None."""
    if not actor:
        return resolve_user(db)
    return resolve_user(db, gh_id=actor.get("databaseId"), login=actor.get("login"),
                        is_bot=actor.get("__typename") == "Bot")


# ---------------------------------------------------------------- writes (collector)
def upsert_repo(db, full_name, url=None, default_branch=None):
    db.execute("INSERT INTO repos(full_name, url, default_branch) VALUES (?,?,?) ON CONFLICT(full_name) DO UPDATE SET "
               "url=COALESCE(excluded.url, repos.url), default_branch=COALESCE(excluded.default_branch, repos.default_branch)",
               (full_name, url, default_branch))
    return db.execute("SELECT id FROM repos WHERE full_name=?", (full_name,)).fetchone()[0]


PR_COLS = ["title", "body", "author_uid", "state", "base_branch", "head_branch", "head_sha", "created_at", "merged_at", "merged_by_uid", "closed_at", "activity_at",
           "additions", "deletions", "changed_files", "ci_status", "files", "url"]


def upsert_pr(db, repo_id, number, pr):
    prev = db.execute("SELECT id, head_sha, state FROM pull_requests WHERE repo_id=? AND number=?",
                      (repo_id, number)).fetchone()
    db.execute(f"INSERT INTO pull_requests(repo_id, number, {', '.join(PR_COLS)}, last_synced) "
               f"VALUES (?,?,{','.join('?' * len(PR_COLS))},?) ON CONFLICT(repo_id, number) DO UPDATE SET "
               + ", ".join(f"{c}=excluded.{c}" for c in PR_COLS) + ", last_synced=excluded.last_synced",
               [repo_id, number, *[pr.get(c) for c in PR_COLS], now()])
    pr_id = db.execute("SELECT id FROM pull_requests WHERE repo_id=? AND number=?", (repo_id, number)).fetchone()[0]
    if prev is None:
        return pr_id, "new"
    return pr_id, "updated" if (prev["head_sha"], prev["state"]) != (pr.get("head_sha"), pr.get("state")) else "unchanged"


def upsert_commit(db, repo_id, c):
    db.execute("INSERT INTO commits(repo_id, sha, author_uid, authored_at, message, additions, deletions, pr_id, direct_push) "
               "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(repo_id, sha) DO UPDATE SET "
               "pr_id=COALESCE(excluded.pr_id, commits.pr_id), author_uid=excluded.author_uid, "
               "direct_push=CASE WHEN excluded.direct_push=1 THEN 1 WHEN excluded.pr_id IS NOT NULL THEN 0 ELSE commits.direct_push END",
               (repo_id, c["sha"], c["author_uid"], c["authored_at"], c.get("message"), c.get("additions"),
                c.get("deletions"), c.get("pr_id"), int(c.get("direct_push", 0))))


def set_coauthors(db, repo_id, sha, uids, main_uid):
    for uid in set(uids) - {main_uid}:
        db.execute("INSERT OR IGNORE INTO commit_coauthors VALUES (?,?,?)", (repo_id, sha, uid))


def pr_people(db, pr_id):
    """Everyone who answers for a PR: the owner (PR author) plus every human whose commits, or co-authored
    commits, are in it. Merge commits (bringing a base branch in) are not counted as authored work."""
    owner = db.execute("SELECT author_uid FROM pull_requests WHERE id=?", (pr_id,)).fetchone()["author_uid"]
    people = {}
    for c in db.execute("SELECT repo_id, sha, author_uid, additions, deletions, message FROM commits WHERE pr_id=? "
                        "ORDER BY authored_at", (pr_id,)).fetchall():
        if (c["message"] or "").startswith("Merge "):
            continue
        uids = {c["author_uid"]} | {r[0] for r in db.execute(
            "SELECT uid FROM commit_coauthors WHERE repo_id=? AND sha=?", (c["repo_id"], c["sha"]))}
        for uid in uids:
            d = people.setdefault(uid, {"uid": uid, "commits": 0, "additions": 0, "deletions": 0, "shas": []})
            d["commits"] += 1
            d["additions"] += c["additions"] or 0
            d["deletions"] += c["deletions"] or 0
            d["shas"].append(c["sha"])
    people.setdefault(owner, {"uid": owner, "commits": 0, "additions": 0, "deletions": 0, "shas": []})
    out = []
    for uid, d in people.items():
        u = db.execute("SELECT login, name, is_bot FROM users WHERE uid=?", (uid,)).fetchone()
        if u and u["is_bot"] and uid != owner:
            continue
        out.append({**d, "login": (u["login"] or u["name"]) if u else uid, "is_owner": uid == owner})
    return sorted(out, key=lambda d: (not d["is_owner"], -d["commits"]))


def _overall(s):
    have = {k: s[k] for k in WEIGHTS if s.get(k) is not None}
    return round(sum(WEIGHTS[k] * v for k, v in have.items()) / sum(WEIGHTS[k] for k in have), 2) if have else None


def contribution_rows(db, pr_id, ev):
    """Per-person rows for a PR evaluation. Returns (rows, missing_logins). A shared PR (commits from more
    than one person) needs a `contributors` entry for every committer, the owner included."""
    people = pr_people(db, pr_id)
    committers = [d for d in people if d["commits"]]
    shared = any(not d["is_owner"] for d in committers)
    given = {str(c.get("login", "")).lower(): c for c in ev.get("contributors") or []}
    s, rows_, missing = ev["scores"], [], []
    for d in people:
        c = given.get(str(d["login"]).lower())
        if d["commits"] and shared and c is None:
            missing.append(d["login"])
            continue
        if c is not None:
            cm, cq, note = c.get("commit_msg"), c.get("code_quality"), c.get("notes")
        elif d["commits"]:
            cm, cq, note = s["commit_msg"], s["code_quality"], None      # single-author PR: the PR's own scores
        else:
            cm = cq = note = None                                          # owner with no commits of their own
        own = {"commit_msg": cm, "code_quality": cq,
               "validation": s["validation"] if d["is_owner"] else None,
               "review_response": s["review_response"] if d["is_owner"] else None}
        rows_.append({**d, **own, "overall": _overall(own), "notes": note})
    return rows_, missing


def save_contributions(db, pr_id, sha, rows_):
    db.execute("DELETE FROM pr_contributions WHERE pr_id=? AND head_sha=? AND rubric_version=?", (pr_id, sha, RUBRIC_VERSION))
    for r in rows_:
        db.execute("INSERT INTO pr_contributions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (pr_id, sha, RUBRIC_VERSION, r["uid"], int(r["is_owner"]), r["commits"], r["additions"], r["deletions"],
                    r["commit_msg"], r["code_quality"], r["validation"], r["review_response"], r["overall"], r["notes"]))


def backfill_contributions(db):
    """Give every current single-author evaluation its owner row. Shared PRs evaluated before per-person
    scoring existed stay without rows, so `pending` sends them back for a contributor-aware evaluation."""
    done = 0
    for e in db.execute("SELECT e.* FROM pr_evaluations e JOIN pull_requests p ON p.id=e.pr_id AND p.head_sha=e.head_sha "
                        "WHERE e.rubric_version=? AND NOT EXISTS (SELECT 1 FROM pr_contributions pc WHERE pc.pr_id=e.pr_id "
                        "AND pc.head_sha=e.head_sha AND pc.rubric_version=e.rubric_version)", (RUBRIC_VERSION,)).fetchall():
        rows_, missing = contribution_rows(db, e["pr_id"], {"scores": {k: e[k] for k in WEIGHTS}})
        if not missing:
            save_contributions(db, e["pr_id"], e["head_sha"], rows_)
            done += 1
    return done


def upsert_review(db, pr_id, r):
    db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET state=excluded.state, body=excluded.body",
               (r["host_id"], pr_id, r["reviewer_uid"], r["state"], r.get("body"), r.get("submitted_at")))


def upsert_thread(db, pr_id, t):
    db.execute("INSERT INTO review_threads VALUES (?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
               "is_resolved=excluded.is_resolved, is_outdated=excluded.is_outdated, resolved_by_uid=excluded.resolved_by_uid",
               (t["thread_id"], pr_id, t.get("path"), t.get("line"), int(bool(t.get("is_resolved"))),
                int(bool(t.get("is_outdated"))), t.get("resolved_by_uid")))


def upsert_comment(db, pr_id, c):
    db.execute("INSERT INTO comments VALUES (?,?,?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET body=excluded.body",
               (c["host_id"], pr_id, c.get("thread_id"), c["kind"], c["author_uid"], c.get("body"), c.get("created_at")))


def classify_promotions(db, repo_id):
    """A PR whose head is a long-lived branch (the default branch, or any branch other PRs merge into) is a
    promotion such as 'Merge dev into prod': it carries other people's already-reviewed work, so it is
    listed but not scored and not used for rework detection."""
    db.execute("""UPDATE pull_requests SET kind = CASE WHEN head_branch IS NOT NULL AND (
                    head_branch = (SELECT default_branch FROM repos WHERE id=:r)
                    OR head_branch IN (SELECT DISTINCT base_branch FROM pull_requests WHERE repo_id=:r))
                  THEN 'promotion' ELSE 'change' END WHERE repo_id=:r""", {"r": repo_id})
    db.execute("""DELETE FROM rework_links WHERE confirmed IS NULL AND (
                    pr_id IN (SELECT id FROM pull_requests WHERE kind='promotion') OR
                    original_pr IN (SELECT id FROM pull_requests WHERE kind='promotion'))""")


REWORK_WORDS = ("fix", "hotfix", "bug", "revert", "follow-up", "followup", "follow up", "regression", "patch", "broken")


def propose_rework(db, repo_id, window_days=30):
    """Heuristic candidates: later PR touches the same files within N days of an earlier merged PR and
    looks like a fix (title/commits) or references it (#N). Claude confirms/rejects during evaluation."""
    prs = db.execute("SELECT id, number, title, body, author_uid, created_at, merged_at, files FROM pull_requests "
                     "WHERE repo_id=? AND COALESCE(kind,'change')='change' ORDER BY created_at", (repo_id,)).fetchall()
    added = 0
    for later in prs:
        text = (later["title"] or "").lower()
        msgs = " ".join(r[0].lower() for r in db.execute("SELECT message FROM commits WHERE pr_id=?", (later["id"],)) if r[0])
        fixy = any(w in text or w in msgs for w in REWORK_WORDS)
        lfiles = set(json.loads(later["files"] or "[]"))
        for orig in prs:
            if orig["id"] == later["id"] or not orig["merged_at"] or orig["merged_at"] > later["created_at"]:
                continue
            days = (datetime.datetime.fromisoformat(later["created_at"][:19]) -
                    datetime.datetime.fromisoformat(orig["merged_at"][:19])).days
            if days > window_days:
                continue
            ref = f"#{orig['number']}" in f"{later['title']} {later['body'] or ''} {msgs}"
            overlap = lfiles & set(json.loads(orig["files"] or "[]"))
            is_revert = text.startswith("revert") and (orig["title"] or "").lower() in text
            if not (is_revert or (overlap and (fixy or ref)) or (ref and fixy)):
                continue
            kind = "revert" if is_revert else ("self_fix" if later["author_uid"] == orig["author_uid"] else "fixed_by_other")
            reason = ", ".join(filter(None, [
                "reverts it" if is_revert else None, f"references #{orig['number']}" if ref else None,
                f"same files: {', '.join(sorted(overlap)[:3])}" if overlap else None, f"{days}d after merge"]))
            cur = db.execute("INSERT INTO rework_links(pr_id, original_pr, kind, reason) VALUES (?,?,?,?) "
                             "ON CONFLICT(pr_id, original_pr) DO UPDATE SET reason=excluded.reason "
                             "WHERE rework_links.confirmed IS NULL", (later["id"], orig["id"], kind, reason))
            added += cur.rowcount
    return added


def log_run(db, kind, repos, since, until, stats):
    db.execute("INSERT INTO runs(kind, started_at, repos, since, until, stats) VALUES (?,?,?,?,?,?)",
               (kind, now(), ",".join(repos), since, until, json.dumps(stats)))


# ---------------------------------------------------------------- evaluation
ENGINES = ("claude-code", "anthropic-api", "heuristic")


def prs_needing_evaluation(db, since, until, repos=None, replace_heuristic=False, skill_hash=None):
    """PRs with no evaluation for their current code. replace_heuristic=True also returns PRs that only
    have a rule-based (no-LLM) score, so an LLM run can upgrade them. skill_hash=<current> also returns PRs
    the API engine scored with a different version of the scoring skill (an explicit "re-score" request)."""
    q = ("SELECT p.id, p.number, p.head_sha, r.full_name, p.additions, p.deletions, p.changed_files "
         "FROM pull_requests p JOIN repos r ON r.id=p.repo_id "
         "JOIN users u ON u.uid=p.author_uid "
         "LEFT JOIN pr_evaluations e ON e.pr_id=p.id AND e.head_sha=p.head_sha AND e.rubric_version=? "
         "WHERE (e.pr_id IS NULL OR NOT EXISTS (SELECT 1 FROM pr_contributions pc WHERE pc.pr_id=p.id "
         "  AND pc.head_sha=p.head_sha AND pc.rubric_version=e.rubric_version)"
         + (" OR e.engine='heuristic'" if replace_heuristic else "")
         + (" OR (e.engine='anthropic-api' AND COALESCE(e.skill_hash,'') != ?)" if skill_hash else "") +
         ") AND u.is_bot=0 AND COALESCE(p.kind,'change')='change' AND substr(COALESCE(p.activity_at, p.created_at),1,10) BETWEEN ? AND ?")
    args = [RUBRIC_VERSION, *([skill_hash] if skill_hash else []), since, until]
    if repos:
        q += f" AND r.full_name IN ({','.join('?' * len(repos))})"
        args += list(repos)
    return db.execute(q + " ORDER BY r.full_name, p.number", args).fetchall()


def overall(scores):
    return round(sum(WEIGHTS[k] * scores[k] for k in WEIGHTS), 2)


def save_evaluation(db, ev):
    """ev follows the JSON contract in STANDARDS.md. Returns (ok, message)."""
    row = db.execute("SELECT p.id, p.head_sha FROM pull_requests p JOIN repos r ON r.id=p.repo_id "
                     "WHERE r.full_name=? AND p.number=?", (ev["repo"], ev["number"])).fetchone()
    if not row:
        return False, "unknown PR"
    if row["head_sha"] != ev["head_sha"]:
        return False, "stale: PR has new commits since the bundle was made"
    s = ev["scores"]
    for k in WEIGHTS:
        if not isinstance(s.get(k), (int, float)) or not 1 <= s[k] <= 5:
            return False, f"score {k} missing or out of range"
    for c in ev.get("contributors") or []:
        for k in ("commit_msg", "code_quality"):
            if not isinstance(c.get(k), (int, float)) or not 1 <= c[k] <= 5:
                return False, f"contributor {c.get('login')}: {k} missing or out of range"
    pr_id, sha = row["id"], row["head_sha"]
    contrib, missing = contribution_rows(db, pr_id, ev)
    if missing:
        return False, f"shared PR: add a `contributors` entry for {', '.join(missing)} (every committer, owner included)"
    engine = ev.get("engine") or "claude-code"
    if engine not in ENGINES:
        return False, f"unknown engine {engine!r}"
    db.execute("INSERT INTO pr_evaluations (pr_id, head_sha, rubric_version, commit_msg, code_quality, validation, "
               "review_response, overall, tests_added, commit_notes, validation_notes, code_issues, summary, talking_points, "
               "evaluated_at, engine, model, agent, skill_hash, input_tokens, output_tokens, cost_usd) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(pr_id, head_sha, rubric_version) DO UPDATE SET commit_msg=excluded.commit_msg, "
               "code_quality=excluded.code_quality, validation=excluded.validation, review_response=excluded.review_response, "
               "overall=excluded.overall, tests_added=excluded.tests_added, commit_notes=excluded.commit_notes, "
               "validation_notes=excluded.validation_notes, code_issues=excluded.code_issues, summary=excluded.summary, "
               "talking_points=excluded.talking_points, evaluated_at=excluded.evaluated_at, engine=excluded.engine, "
               "model=excluded.model, agent=excluded.agent, skill_hash=excluded.skill_hash, "
               "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, cost_usd=excluded.cost_usd",
               (pr_id, sha, RUBRIC_VERSION, s["commit_msg"], s["code_quality"], s["validation"], s["review_response"],
                overall(s), int(bool(ev.get("tests_added"))), ev.get("commit_notes"), ev.get("validation_notes"),
                json.dumps(ev.get("code_issues", [])), ev.get("summary"), json.dumps(ev.get("talking_points", [])), now(),
                engine, ev.get("model"), ev.get("agent"), ev.get("skill_hash"),
                (ev.get("usage") or {}).get("input"), (ev.get("usage") or {}).get("output"), ev.get("cost_usd")))
    save_contributions(db, pr_id, sha, contrib)
    for t in ev.get("threads", []):
        db.execute("INSERT INTO thread_evaluations VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                   "validity=excluded.validity, resolution=excluded.resolution, fix_sha=excluded.fix_sha, note=excluded.note",
                   (str(t["comment_id"]), pr_id, sha, RUBRIC_VERSION, t["validity"], t["resolution"], t.get("fix_sha"), t.get("note")))
    for r in ev.get("reviewers", []):
        a = db.execute("SELECT uid FROM user_aliases WHERE alias=?", (r["login"],)).fetchone()
        if a:
            db.execute("INSERT INTO reviewer_evaluations VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                       "score=excluded.score, missed=excluded.missed, notes=excluded.notes",
                       (pr_id, sha, RUBRIC_VERSION, a["uid"], r["score"], json.dumps(r.get("missed", [])), r.get("notes")))
    for rw in ev.get("rework", []):
        orig = db.execute("SELECT id FROM pull_requests WHERE repo_id=(SELECT repo_id FROM pull_requests WHERE id=?) AND number=?",
                          (pr_id, rw["original_pr"])).fetchone()
        if orig:
            db.execute("INSERT INTO rework_links(pr_id, original_pr, kind, confirmed, evidence) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(pr_id, original_pr) DO UPDATE SET kind=excluded.kind, confirmed=excluded.confirmed, "
                       "evidence=excluded.evidence", (pr_id, orig["id"], rw["kind"], int(bool(rw.get("confirmed"))), rw.get("evidence")))
    return True, "saved"


# ---------------------------------------------------------------- reads (dashboard)
def _window(since, until, repo, col="COALESCE(p.activity_at, p.created_at)", repo_col="r.full_name"):
    sql, args = f" AND substr({col},1,10) BETWEEN ? AND ?", [since, until]
    if repo:
        sql += f" AND {repo_col}=?"
        args.append(repo)
    return sql, args


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def meta(db):
    return {"rubric_version": RUBRIC_VERSION,
            "repos": [r[0] for r in db.execute("SELECT full_name FROM repos ORDER BY full_name")],
            "first": db.execute("SELECT min(substr(COALESCE(activity_at, created_at),1,10)) FROM pull_requests").fetchone()[0],
            "last": db.execute("SELECT max(substr(COALESCE(activity_at, created_at),1,10)) FROM pull_requests").fetchone()[0],
            "last_run": db.execute("SELECT max(started_at) FROM runs").fetchone()[0],
            "engines": {r[0] or "claude-code": r[1] for r in db.execute(
                "SELECT engine, count(*) FROM v_pr WHERE overall IS NOT NULL GROUP BY engine")},
            "pending_eval": db.execute("SELECT count(*) FROM v_pr p JOIN users u ON u.uid=p.author_uid "
                                        "WHERE u.is_bot=0 AND COALESCE(p.kind,'change')='change' AND (p.overall IS NULL OR NOT EXISTS "
                                        "(SELECT 1 FROM pr_contributions pc WHERE pc.pr_id=p.id AND pc.head_sha=p.head_sha "
                                        "AND pc.rubric_version=p.rubric_version))").fetchone()[0]}


def team(db, since, until, repo=None):
    w, a = _window(since, until, repo, "COALESCE(p.activity_at, p.created_at)", "p.repo")
    users = rows(db.execute(f"""
      SELECT u.uid, u.login, u.name,
        count(p.id) prs, count(DISTINCT p.repo) repos, count(p.overall) evaluated, sum(p.state='merged') merged
      FROM users u JOIN v_pr p ON p.author_uid=u.uid
      WHERE u.is_bot=0 AND COALESCE(p.kind,'change')='change' {w} GROUP BY u.uid""", a))
    by_uid = {u["uid"]: u for u in users}
    w2, a2 = _window(since, until, repo)
    # scores: each person's own share of every PR they own or committed to (see pr_contributions)
    for r in db.execute(f"""SELECT pc.uid, count(DISTINCT p.repo_id) repos, sum(1 - pc.is_owner) contributed,
          round(avg(pc.overall),2) overall, round(avg(pc.commit_msg),2) commit_msg, round(avg(pc.code_quality),2) code_quality,
          round(avg(pc.validation),2) validation, round(avg(pc.review_response),2) review_response
        FROM pr_contributions pc JOIN pull_requests p ON p.id=pc.pr_id AND p.head_sha=pc.head_sha
        JOIN repos r ON r.id=p.repo_id JOIN users u ON u.uid=pc.uid
        WHERE pc.rubric_version='{RUBRIC_VERSION}' AND u.is_bot=0 AND COALESCE(p.kind,'change')='change' {w2}
        GROUP BY pc.uid""", a2):
        if r["uid"] not in by_uid:
            u = db.execute("SELECT uid, login, name FROM users WHERE uid=?", (r["uid"],)).fetchone()
            by_uid[r["uid"]] = {**dict(u), "prs": 0, "evaluated": 0}
        by_uid[r["uid"]].update({k: r[k] for k in r.keys() if k != "uid"})
    for r in db.execute(f"""SELECT rv.reviewer_uid uid, count(DISTINCT rv.pr_id) n FROM reviews rv
          JOIN pull_requests p ON p.id=rv.pr_id JOIN repos r ON r.id=p.repo_id JOIN users u ON u.uid=rv.reviewer_uid
          WHERE u.is_bot=0 AND rv.reviewer_uid != p.author_uid AND COALESCE(p.kind,'change')='change' {w2} GROUP BY rv.reviewer_uid""", a2):
        if r["uid"] not in by_uid:
            u = db.execute("SELECT uid, login, name FROM users WHERE uid=?", (r["uid"],)).fetchone()
            by_uid[r["uid"]] = {**dict(u), "prs": 0, "repos": 0, "evaluated": 0, "overall": None}
        by_uid[r["uid"]]["reviews_given"] = r["n"]
    for r in db.execute(f"""SELECT o.author_uid uid, count(DISTINCT o.id) n FROM rework_links rl
          JOIN pull_requests o ON o.id=rl.original_pr JOIN repos r ON r.id=o.repo_id
          WHERE rl.confirmed=1 {_window(since, until, repo, 'COALESCE(o.activity_at, o.created_at)')[0]} GROUP BY o.author_uid""", a2):
        if r["uid"] in by_uid:
            by_uid[r["uid"]]["reworked"] = r["n"]
    for r in db.execute(f"""SELECT p.author_uid uid, count(*) n FROM thread_evaluations te
          JOIN pull_requests p ON p.id=te.pr_id AND p.head_sha=te.head_sha JOIN repos r ON r.id=p.repo_id
          WHERE te.rubric_version='{RUBRIC_VERSION}' AND te.validity='valid' AND te.resolution IN ('dismissed','open') {w2}
          GROUP BY p.author_uid""", a2):
        if r["uid"] in by_uid:
            by_uid[r["uid"]]["ignored_comments"] = r["n"]
    for r in db.execute(f"""SELECT c.author_uid uid, count(*) n FROM commits c JOIN repos r ON r.id=c.repo_id
          WHERE c.direct_push=1 {_window(since, until, repo, 'c.authored_at')[0]} GROUP BY c.author_uid""", a2):
        if r["uid"] not in by_uid:
            u = db.execute("SELECT uid, login, name, is_bot FROM users WHERE uid=?", (r["uid"],)).fetchone()
            if not u or u["is_bot"]:
                continue
            by_uid[r["uid"]] = {"uid": u["uid"], "login": u["login"], "name": u["name"], "prs": 0, "repos": 0, "evaluated": 0, "overall": None}
        by_uid[r["uid"]]["direct_pushes"] = r["n"]
    for r in db.execute(f"""SELECT x.uid, count(DISTINCT x.sha) n FROM (
          SELECT c.author_uid uid, c.repo_id, c.sha, c.authored_at FROM commits c WHERE c.message NOT LIKE 'Merge %'
          UNION SELECT ca.uid, c.repo_id, c.sha, c.authored_at FROM commit_coauthors ca
                JOIN commits c ON c.repo_id=ca.repo_id AND c.sha=ca.sha) x
        JOIN repos r ON r.id=x.repo_id {_window(since, until, repo, 'x.authored_at')[0].replace(' AND', ' WHERE', 1)}
        GROUP BY x.uid""", a2):
        if r["uid"] not in by_uid:
            u = db.execute("SELECT uid, login, name, is_bot FROM users WHERE uid=?", (r["uid"],)).fetchone()
            if not u or u["is_bot"]:
                continue
            by_uid[r["uid"]] = {"uid": u["uid"], "login": u["login"], "name": u["name"], "prs": 0, "repos": 0, "evaluated": 0, "overall": None}
        by_uid[r["uid"]]["commits"] = r["n"]
    return sorted(by_uid.values(), key=lambda u: (-(u["prs"] or 0), (u["login"] or u["name"] or "").lower()))


def user_detail(db, uid, since, until, repo=None):
    u = db.execute("SELECT uid, login, name FROM users WHERE uid=?", (uid,)).fetchone()
    if not u:
        return None
    aliases = [r[0] for r in db.execute("SELECT alias FROM user_aliases WHERE uid=? ORDER BY alias", (uid,))]
    w, a = _window(since, until, repo, "COALESCE(p.activity_at, p.created_at)", "p.repo")
    prs = rows(db.execute(f"""SELECT p.id, p.repo, p.number, p.title, p.url, p.state, COALESCE(p.kind,'change') kind, p.base_branch, p.head_branch, p.created_at, p.merged_at, p.closed_at, COALESCE(p.activity_at, p.created_at) activity_at, p.ci_status,
          p.additions, p.deletions, p.author_login owner, CASE WHEN p.author_uid=:uid THEN 'owner' ELSE 'contributor' END role,
          p.overall pr_overall, p.code_issues,
          -- this person's own share; falls back to the PR's scores only while a shared PR awaits re-evaluation
          CASE WHEN pc.uid IS NOT NULL THEN pc.overall WHEN p.author_uid=:uid THEN p.overall END overall,
          CASE WHEN pc.uid IS NOT NULL THEN pc.commit_msg WHEN p.author_uid=:uid THEN p.commit_msg END commit_msg,
          CASE WHEN pc.uid IS NOT NULL THEN pc.code_quality WHEN p.author_uid=:uid THEN p.code_quality END code_quality,
          CASE WHEN pc.uid IS NOT NULL THEN pc.validation WHEN p.author_uid=:uid THEN p.validation END validation,
          CASE WHEN pc.uid IS NOT NULL THEN pc.review_response WHEN p.author_uid=:uid THEN p.review_response END review_response,
          pc.notes my_notes, pc.uid IS NULL AND p.overall IS NOT NULL needs_split,
          (SELECT count(*) FROM commits c WHERE c.pr_id=p.id) commits,
          (SELECT count(*) FROM commits c WHERE c.pr_id=p.id AND c.message NOT LIKE 'Merge %' AND (c.author_uid=:uid OR EXISTS
              (SELECT 1 FROM commit_coauthors ca WHERE ca.repo_id=c.repo_id AND ca.sha=c.sha AND ca.uid=:uid))) my_commits,
          (SELECT count(*) FROM review_threads t WHERE t.pr_id=p.id) threads,
          (SELECT count(*) FROM thread_evaluations te WHERE te.pr_id=p.id AND te.head_sha=p.head_sha
              AND te.validity='valid' AND te.resolution IN ('dismissed','open')) ignored,
          (SELECT count(*) FROM rework_links rl WHERE rl.original_pr=p.id AND rl.confirmed=1) reworked,
          (SELECT group_concat(DISTINCT COALESCE(ru.login, ru.name)) FROM reviews rv JOIN users ru ON ru.uid=rv.reviewer_uid
              WHERE rv.pr_id=p.id AND rv.reviewer_uid != p.author_uid) reviewers
        FROM v_pr p LEFT JOIN pr_contributions pc ON pc.pr_id=p.id AND pc.head_sha=p.head_sha
             AND pc.rubric_version='{RUBRIC_VERSION}' AND pc.uid=:uid
        WHERE (p.author_uid=:uid OR pc.uid IS NOT NULL OR EXISTS (SELECT 1 FROM commits c WHERE c.pr_id=p.id
               AND c.message NOT LIKE 'Merge %' AND (c.author_uid=:uid OR EXISTS (SELECT 1 FROM commit_coauthors ca
               WHERE ca.repo_id=c.repo_id AND ca.sha=c.sha AND ca.uid=:uid))))
          {w.replace('?', ':s', 1).replace('?', ':u', 1).replace('?', ':repo', 1)}
        ORDER BY COALESCE(p.activity_at, p.created_at) DESC""", {"uid": uid, "s": since, "u": until, "repo": repo}))
    me = {x.lower() for x in (u["login"], u["name"], *aliases) if x}
    my_issues = []
    for p in prs:
        issues = json.loads(p.pop("code_issues") or "[]")
        for i in issues:     # an issue belongs to the committer named on it; unnamed issues belong to the PR owner
            who = str(i.get("author") or "").lower()
            if (who in me) if who else p["role"] == "owner":
                my_issues.append({**i, "pr_id": p["id"], "repo": p["repo"], "number": p["number"], "title": p["title"]})
    projects, months = {}, {}
    for p in prs:
        if p["kind"] == "promotion":
            continue
        d = projects.setdefault(p["repo"], {"repo": p["repo"], "prs": 0, "contributed": 0, "merged": 0, "scores": [], "reworked": 0, "ignored": 0})
        if p["role"] == "owner":
            d["prs"] += 1; d["merged"] += p["state"] == "merged"; d["reworked"] += p["reworked"]; d["ignored"] += p["ignored"]
        else:
            d["contributed"] += 1
        m = months.setdefault(p["activity_at"][:7], {"month": p["activity_at"][:7], "prs": 0, "scores": []})
        m["prs"] += 1
        for x in (d, m):
            if p["overall"] is not None:
                x["scores"].append(p["overall"])
    for d in [*projects.values(), *months.values()]:
        d["overall"] = round(sum(d["scores"]) / len(d["scores"]), 2) if d["scores"] else None
        del d["scores"]
    trend = sorted(months.values(), key=lambda m: m["month"])
    w2, a2 = _window(since, until, repo)
    reviews = rows(db.execute(f"""SELECT p.id pr_id, r.full_name repo, p.number, p.title, p.url, p.additions + p.deletions size,
          COALESCE(au.login, au.name) author,
          (SELECT group_concat(DISTINCT rv.state) FROM reviews rv WHERE rv.pr_id=p.id AND rv.reviewer_uid=:uid) states,
          (SELECT count(*) FROM comments c WHERE c.pr_id=p.id AND c.author_uid=:uid) comments,
          re.score, re.missed, re.notes
        FROM pull_requests p JOIN repos r ON r.id=p.repo_id LEFT JOIN users au ON au.uid=p.author_uid
        LEFT JOIN reviewer_evaluations re ON re.pr_id=p.id AND re.head_sha=p.head_sha AND re.reviewer_uid=:uid
             AND re.rubric_version='{RUBRIC_VERSION}'
        WHERE p.author_uid != :uid AND COALESCE(p.kind,'change')='change' AND EXISTS (SELECT 1 FROM reviews rv WHERE rv.pr_id=p.id AND rv.reviewer_uid=:uid)
          {w2.replace('?', ':s', 1).replace('?', ':u', 1).replace('?', ':repo', 1)}
        ORDER BY COALESCE(p.activity_at, p.created_at) DESC""", {"uid": uid, "s": since, "u": until, "repo": repo}))
    direct = rows(db.execute(f"""SELECT r.full_name repo, c.sha, c.authored_at, c.message FROM commits c JOIN repos r ON r.id=c.repo_id
        WHERE c.author_uid=? AND c.direct_push=1 {_window(since, until, repo, 'c.authored_at')[0]} ORDER BY c.authored_at DESC""",
                       [uid, *a2]))
    summary = next((t for t in team(db, since, until, repo) if t["uid"] == uid), {})
    return {"user": dict(u), "aliases": aliases, "summary": summary, "projects": sorted(projects.values(), key=lambda d: -d["prs"]),
            "trend": trend, "prs": prs, "reviews": reviews, "direct_pushes": direct, "my_issues": my_issues}


def pr_detail(db, pr_id):
    p = db.execute("SELECT * FROM v_pr WHERE id=?", (pr_id,)).fetchone()
    if not p:
        return None
    p = dict(p)
    for k in ("code_issues", "talking_points", "files"):
        p[k] = json.loads(p[k]) if p.get(k) else []
    p["commits"] = rows(db.execute("SELECT c.sha, c.authored_at, c.message, c.additions, c.deletions, COALESCE(u.login,u.name) author, "
                                   "(SELECT group_concat(COALESCE(cu.login, cu.name), ', ') FROM commit_coauthors ca JOIN users cu ON cu.uid=ca.uid "
                                   " WHERE ca.repo_id=c.repo_id AND ca.sha=c.sha) coauthors FROM commits c "
                                   "LEFT JOIN users u ON u.uid=c.author_uid WHERE c.pr_id=? ORDER BY c.authored_at", (pr_id,)))
    saved = {r["uid"]: dict(r) for r in db.execute("SELECT * FROM pr_contributions WHERE pr_id=? AND head_sha=? AND rubric_version=?",
                                                   (pr_id, p["head_sha"], RUBRIC_VERSION))}
    p["people"] = [{k: v for k, v in {**d, **saved.get(d["uid"], {}), "scored": d["uid"] in saved}.items() if k != "shas"}
                   for d in pr_people(db, pr_id)]
    p["reviews"] = rows(db.execute("SELECT rv.state, rv.body, rv.submitted_at, COALESCE(u.login,u.name) reviewer FROM reviews rv "
                                   "LEFT JOIN users u ON u.uid=rv.reviewer_uid WHERE rv.pr_id=? ORDER BY rv.submitted_at", (pr_id,)))
    threads = []
    for t in db.execute("SELECT * FROM review_threads WHERE pr_id=?", (pr_id,)):
        cs = rows(db.execute("SELECT c.host_id, c.body, c.created_at, COALESCE(u.login,u.name) author FROM comments c "
                             "LEFT JOIN users u ON u.uid=c.author_uid WHERE c.thread_id=? ORDER BY c.created_at", (t["thread_id"],)))
        ev = db.execute("SELECT validity, resolution, fix_sha, note FROM thread_evaluations WHERE comment_id=? AND head_sha=? "
                        "AND rubric_version=?", (cs[0]["host_id"] if cs else "", p["head_sha"], RUBRIC_VERSION)).fetchone()
        threads.append({**dict(t), "comments": cs, "eval": dict(ev) if ev else None})
    p["threads"] = sorted(threads, key=lambda t: t["comments"][0]["created_at"] if t["comments"] else "")
    p["merged_by"] = (db.execute("SELECT COALESCE(login,name) FROM users WHERE uid=?", (p.get("merged_by_uid"),)).fetchone() or [None])[0]
    p["conversation"] = rows(db.execute("SELECT c.body, c.created_at, COALESCE(u.login,u.name) author FROM comments c "
                                        "LEFT JOIN users u ON u.uid=c.author_uid WHERE c.pr_id=? AND c.kind='conversation' AND COALESCE(u.is_bot,0)=0 "
                                        "ORDER BY c.created_at", (pr_id,)))
    p["reviewer_evals"] = rows(db.execute("SELECT COALESCE(u.login,u.name) reviewer, re.score, re.missed, re.notes "
                                          "FROM reviewer_evaluations re JOIN users u ON u.uid=re.reviewer_uid "
                                          "WHERE re.pr_id=? AND re.head_sha=? AND re.rubric_version=?",
                                          (pr_id, p["head_sha"], RUBRIC_VERSION)))
    for r in p["reviewer_evals"]:
        r["missed"] = json.loads(r["missed"] or "[]")
    p["fixed_by"] = rows(db.execute("SELECT rl.kind, rl.evidence, rl.reason, rl.confirmed, l.id, l.number, l.title, COALESCE(u.login,u.name) author "
                                    "FROM rework_links rl JOIN pull_requests l ON l.id=rl.pr_id LEFT JOIN users u ON u.uid=l.author_uid "
                                    "WHERE rl.original_pr=? AND rl.confirmed=1", (pr_id,)))
    p["fixes"] = rows(db.execute("SELECT rl.kind, rl.evidence, rl.confirmed, o.id, o.number, o.title FROM rework_links rl "
                                 "JOIN pull_requests o ON o.id=rl.original_pr WHERE rl.pr_id=? AND rl.confirmed=1", (pr_id,)))
    return p


# ---------------------------------------------------------------- "self" view: one person sees only their own data
def pr_access(db, pr_id, uid):
    """How much of a PR a person may see: 'full' (owner / committer / co-author), 'reviewer' (only reviewed it), or None."""
    if not db.execute("SELECT 1 FROM pull_requests WHERE id=?", (pr_id,)).fetchone():
        return None
    if any(d["uid"] == uid for d in pr_people(db, pr_id)):
        return "full"
    if db.execute("SELECT 1 FROM reviews WHERE pr_id=? AND reviewer_uid=?", (pr_id, uid)).fetchone():
        return "reviewer"
    return None


def redact_pr_for_self(db, p, uid, access):
    """Strip what a person may not see of other people's evaluation: their scores, notes and reviewer ratings.
    'reviewer' access (they only reviewed it) keeps just the PR header and their own reviewer assessment."""
    mine = db.execute("SELECT COALESCE(login,name) FROM users WHERE uid=?", (uid,)).fetchone()
    me = (mine[0] if mine else None)
    p = dict(p)
    p["reviewer_evals"] = [r for r in p.get("reviewer_evals", []) if r["reviewer"] == me]
    if access == "reviewer":
        keep = ("id", "repo", "number", "title", "url", "state", "kind", "author_login", "created_at", "merged_at",
                "closed_at", "additions", "deletions", "changed_files", "ci_status", "engine", "head_branch", "base_branch",
                "reviewer_evals", "merged_by", "model")
        out = {k: p.get(k) for k in keep}
        out.update({"redacted": True, "people": [], "commits": [], "threads": [], "reviews": [], "conversation": [],
                    "code_issues": [], "talking_points": [], "fixed_by": [], "fixes": [], "overall": None})
        return out
    for x in p.get("people", []):
        if x["login"] != me:                    # other committers' scores and notes are theirs, not yours
            for k in ("commit_msg", "code_quality", "validation", "review_response", "overall", "notes"):
                x[k] = None
    # issues traced to someone else's commits stay visible only without their name being scored
    p["redacted"] = False
    return p
