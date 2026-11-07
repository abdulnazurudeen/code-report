#!/usr/bin/env python3
"""PR & code-review audit - command line.

  python audit.py collect  --repos org/a org/b --months 3      # pull GitHub data into audit.db (safe to re-run)
  python audit.py pending  --months 3 [--repos ...]            # write review bundles for PRs not yet evaluated
  python audit.py diff     org/a 123                           # print a PR diff (used by the evaluator)
  python audit.py ingest                                       # load evals/*.json produced by Claude
  python audit.py serve    [--port 8765] [--open]              # run the dashboard
  python audit.py status                                       # counts + what still needs evaluation
  python audit.py merge-users email:x@y.com gh:12345           # manual identity fix
  python audit.py repos    --org myorg --months 3              # repos with pushes in the period
  python audit.py collect  --org myorg --months 3              # collect every repo pushed in the period
  python audit.py people                                       # identity check: who is who, unlinked emails
  python audit.py score    --engine heuristic --months 3       # score pending PRs, rule-based, no LLM / token
  python audit.py score    --engine anthropic --months 3       # score pending PRs via ANTHROPIC_API_KEY
  python audit.py backup   --out audit-backup.db               # consistent copy of the database
Environment: AUDIT_DB, GITHUB_TOKEN, HOST, PORT, DASHBOARD_PASSWORD / DASHBOARD_USERS, AUDIT_READONLY=1,
             ANTHROPIC_API_KEY, AUDIT_MODEL, AUDIT_EFFORT (see README.md).
"""
import argparse, datetime, json, os, sys, http.server, urllib.parse, threading, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True   # keep the folder free of __pycache__
sys.path.insert(0, str(HERE))
import store  # noqa: E402

MAX_DIFF_CHARS = 100_000


def window(args):
    until = args.until or datetime.date.today().isoformat()
    if args.since:
        return args.since, until
    d = datetime.date.fromisoformat(until)
    m = d.month - args.months
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return datetime.date(y, m, day).isoformat(), until


def parse_repo(s):
    s = s.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "github.com" in s:
        s = s.split("github.com", 1)[1].lstrip(":/")
    return s


# ------------------------------------------------------------------ collect
def collect(db, gh, full_name, since, until):
    import github
    stats = {"new": 0, "updated": 0, "unchanged": 0, "direct_pushes": 0}
    repo_id = None
    for repo, n in github.iter_prs(gh, full_name, since):
        if repo_id is None:
            repo_id = store.upsert_repo(db, full_name, repo["url"], (repo["defaultBranchRef"] or {}).get("name"))
        commits = github.all_pr_commits(gh, full_name, n)
        # any activity inside the window counts: opened, merged, closed, or commits pushed
        dates = [d for d in [n["createdAt"], n["mergedAt"], n["closedAt"], *[c["authoredDate"] for c in commits]] if d]
        if not any(since <= d[:10] <= until for d in dates):
            continue
        activity_at = max(d for d in dates if d[:10] <= until)
        last = commits[-1] if commits else None
        pr = {"title": n["title"], "body": n["body"], "author_uid": store.actor_uid(db, n["author"]),
              "state": n["state"].lower(), "base_branch": n["baseRefName"], "head_branch": n.get("headRefName"),
              "head_sha": n["headRefOid"],
              "created_at": n["createdAt"], "merged_at": n["mergedAt"], "closed_at": n["closedAt"],
              "merged_by_uid": store.actor_uid(db, n["mergedBy"]) if n.get("mergedBy") else None,
              "activity_at": activity_at,
              "additions": n["additions"], "deletions": n["deletions"], "changed_files": n["changedFiles"],
              "ci_status": ((last or {}).get("statusCheckRollup") or {}).get("state", "unknown"),
              "files": json.dumps([f["path"] for f in (n.get("files") or {}).get("nodes", [])]), "url": n["url"]}
        pr_id, status = store.upsert_pr(db, repo_id, n["number"], pr)
        stats[status] += 1
        for c in commits:
            row = commit_row(db, c)
            if row["author_uid"].startswith("email:") and pr["author_uid"].startswith("gh:"):
                row["author_uid"] = store.link_email_to_author(db, row["author_uid"], pr["author_uid"], (c.get("author") or {}).get("name"))
            store.upsert_commit(db, repo_id, {**row, "pr_id": pr_id})
            store.set_coauthors(db, repo_id, row["sha"], row["coauthors"], row["author_uid"])
        for r in n["reviews"]["nodes"]:
            store.upsert_review(db, pr_id, {"host_id": f"rv{r['databaseId']}", "reviewer_uid": store.actor_uid(db, r["author"]),
                                            "state": r["state"], "body": r["body"], "submitted_at": r["submittedAt"]})
        for t in n["reviewThreads"]["nodes"]:
            store.upsert_thread(db, pr_id, {"thread_id": t["id"], "path": t["path"], "line": t["line"],
                                            "is_resolved": t["isResolved"], "is_outdated": t["isOutdated"],
                                            "resolved_by_uid": store.actor_uid(db, t["resolvedBy"]) if t["resolvedBy"] else None})
            for c in t["comments"]["nodes"]:
                store.upsert_comment(db, pr_id, {"host_id": f"c{c['databaseId']}", "thread_id": t["id"], "kind": "review",
                                                 "author_uid": store.actor_uid(db, c["author"]), "body": c["body"],
                                                 "created_at": c["createdAt"]})
        for c in n["comments"]["nodes"]:
            store.upsert_comment(db, pr_id, {"host_id": f"ic{c['databaseId']}", "kind": "conversation",
                                             "author_uid": store.actor_uid(db, c["author"]), "body": c["body"],
                                             "created_at": c["createdAt"]})
    if repo_id is None:
        repo_id = store.upsert_repo(db, full_name)
    default_branch = db.execute("SELECT default_branch FROM repos WHERE id=?", (repo_id,)).fetchone()[0]
    for c in github.iter_default_branch_commits(gh, full_name, since, until):
        prs = (c["associatedPullRequests"].get("nodes") or [])
        # only PRs *from* a feature branch count; a promotion PR (dev -> uat/prod) doesn't make it reviewed
        via_feature_pr = any(p.get("headRefName") != default_branch for p in prs) if prs else c["associatedPullRequests"]["totalCount"] > 0
        if not via_feature_pr and not c["message"].startswith("Merge "):
            row = commit_row(db, c)
            store.upsert_commit(db, repo_id, {**row, "direct_push": 1})
            store.set_coauthors(db, repo_id, row["sha"], row["coauthors"], row["author_uid"])
            stats["direct_pushes"] += 1
    store.classify_promotions(db, repo_id)
    stats["promotions"] = db.execute("SELECT count(*) FROM pull_requests WHERE repo_id=? AND kind='promotion'", (repo_id,)).fetchone()[0]
    stats["rework_candidates"] = store.propose_rework(db, repo_id)
    stats["shared_prs"] = db.execute("SELECT count(DISTINCT c.pr_id) FROM commits c JOIN pull_requests p ON p.id=c.pr_id "
                                     "WHERE p.repo_id=? AND c.message NOT LIKE 'Merge %' AND c.author_uid != p.author_uid",
                                     (repo_id,)).fetchone()[0]
    db.commit()
    return stats


def commit_row(db, c):
    a = c.get("author") or {}
    u = a.get("user") or {}
    uid = store.resolve_user(db, gh_id=u.get("databaseId"), login=u.get("login"), name=a.get("name"), email=a.get("email"))
    co = []
    for x in ((c.get("authors") or {}).get("nodes") or [])[1:]:     # first entry is the main author
        xu = x.get("user") or {}
        co.append(store.resolve_user(db, gh_id=xu.get("databaseId"), login=xu.get("login"), name=x.get("name"), email=x.get("email")))
    return {"sha": c["oid"], "author_uid": uid, "authored_at": c["authoredDate"], "message": c["message"],
            "additions": c.get("additions"), "deletions": c.get("deletions"), "coauthors": co}


# ------------------------------------------------------------------ evaluation bundles
def bundle_name(full_name, number):
    return f"{full_name.replace('/', '__')}__{number}.json"


def make_bundle(db, pr_id, diff_text, gh=None):
    p = store.pr_detail(db, pr_id)
    people = store.pr_people(db, pr_id)
    shared = sum(1 for d in people if d["commits"]) > 1 or any(d["commits"] and not d["is_owner"] for d in people)
    commits = p["commits"]
    if shared and gh is not None:            # who changed what: needed to hold each committer to their own code
        for c in commits:
            if not (c["message"] or "").startswith("Merge "):
                c["files"] = gh.commit_files(p["repo"], c["sha"])
    cands = [dict(r) for r in db.execute(
        "SELECT o.number original_pr, o.title, COALESCE(u.login,u.name) original_author, rl.kind kind_guess, rl.reason "
        "FROM rework_links rl JOIN pull_requests o ON o.id=rl.original_pr LEFT JOIN users u ON u.uid=o.author_uid "
        "WHERE rl.pr_id=?", (pr_id,))]
    truncated = len(diff_text) > MAX_DIFF_CHARS
    return {
        "instructions": "Evaluate against STANDARDS.md. Write the result JSON to evals/<same file name>.",
        "repo": p["repo"], "number": p["number"], "head_sha": p["head_sha"], "url": p["url"],
        "title": p["title"], "body": p["body"], "author": p["author_login"], "state": p["state"],
        "base_branch": p["base_branch"], "head_branch": p.get("head_branch"),
        "created_at": p["created_at"], "merged_at": p["merged_at"], "closed_at": p["closed_at"], "merged_by": p["merged_by"], "ci_status": p["ci_status"],
        "size": {"additions": p["additions"], "deletions": p["deletions"], "files": p["changed_files"]},
        "commits": commits,
        "people": [{"login": d["login"], "role": "owner" if d["is_owner"] else "contributor", "commits": d["commits"],
                    "additions": d["additions"], "deletions": d["deletions"], "shas": d["shas"]} for d in people],
        "shared_pr": shared,
        "reviews": p["reviews"],
        "threads": [{"comment_id": t["comments"][0]["host_id"] if t["comments"] else None, "path": t["path"],
                     "line": t["line"], "is_resolved": bool(t["is_resolved"]), "is_outdated": bool(t["is_outdated"]),
                     "comments": [{k: c[k] for k in ("author", "created_at", "body")} for c in t["comments"]]}
                    for t in p["threads"]],
        "conversation": p["conversation"],
        "rework_candidates": cands,
        "direct_commits_after_merge": [dict(r) for r in db.execute(
            "SELECT c.sha, c.authored_at, COALESCE(u.login,u.name) author, c.message FROM commits c "
            "LEFT JOIN users u ON u.uid=c.author_uid WHERE c.repo_id=(SELECT repo_id FROM pull_requests WHERE id=?) "
            "AND c.direct_push=1 AND ? IS NOT NULL AND c.authored_at >= ? AND julianday(c.authored_at) - julianday(?) <= 14 "
            "ORDER BY c.authored_at", (pr_id, p["merged_at"], p["merged_at"] or "", p["merged_at"] or ""))],
        "diff_truncated": truncated,
        "diff": diff_text[:MAX_DIFF_CHARS],
    }


def cmd_score(db, args):
    """Score pending PRs without a Claude Code session: rule-based (free) or via the Anthropic API (API key)."""
    import github, scoring
    store.backfill_contributions(db)
    db.commit()
    since, until = window(args)
    llm = args.engine == "anthropic"
    todo = store.prs_needing_evaluation(db, since, until, [parse_repo(r) for r in args.repos] if args.repos else None,
                                        replace_heuristic=llm)
    gh = github.GitHub(github.load_token())
    report = {"engine": args.engine, "window": [since, until], "pending": len(todo), "saved": 0, "failed": [],
              "tokens": {"input": 0, "output": 0, "cache_read": 0}}
    for row in todo[: args.limit or None]:
        diff = gh.pr_diff(row["full_name"], row["number"])
        bundle = make_bundle(db, row["id"], diff, gh)
        label = f"{row['full_name']}#{row['number']}"
        try:
            ev = scoring.llm_eval(bundle, diff) if llm else scoring.heuristic_eval(bundle, diff)
        except Exception as e:                       # keep going; one bad PR must not stop a scheduled run
            report["failed"].append({"pr": label, "error": f"{type(e).__name__}: {e}"[:300]})
            continue
        for k, v in (ev.pop("_usage", None) or {}).items():
            report["tokens"][k] += v
        ok, msg = store.save_evaluation(db, ev)
        if ok:
            report["saved"] += 1
            db.commit()
        else:
            report["failed"].append({"pr": label, "error": msg})
        print(f"{label}: {'saved' if ok else msg}", flush=True)
    store.log_run(db, f"score:{args.engine}", [], since, until, report)
    db.commit()
    print(json.dumps(report, indent=1))


def cmd_backup(db_path, out):
    """Consistent copy of the database (safe while the dashboard or a scoring run is using it)."""
    import sqlite3
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    dst.close(); src.close()
    print(f"backup written to {out} ({Path(out).stat().st_size // 1024} KB)")


def cmd_pending(db, args):
    import github
    store.backfill_contributions(db)
    db.commit()
    since, until = window(args)
    todo = store.prs_needing_evaluation(db, since, until, [parse_repo(r) for r in args.repos] if args.repos else None)
    out = HERE / "bundles"
    out.mkdir(exist_ok=True)
    gh = None
    written = []
    for row in todo[: args.limit or None]:
        f = out / bundle_name(row["full_name"], row["number"])
        if f.exists() and json.loads(f.read_text(encoding="utf-8")).get("head_sha") == row["head_sha"]:
            written.append(f.name)
            continue
        gh = gh or github.GitHub(github.load_token())
        f.write_text(json.dumps(make_bundle(db, row["id"], gh.pr_diff(row["full_name"], row["number"]), gh), indent=1), encoding="utf-8")
        written.append(f.name)
    print(json.dumps({"window": [since, until], "pending": len(todo), "bundles": written}, indent=1))


def cmd_ingest(db, args):
    src = HERE / "evals"
    done = src / "done"
    done.mkdir(parents=True, exist_ok=True)
    report = {"saved": 0, "rejected": []}
    report["owner_rows_backfilled"] = store.backfill_contributions(db)
    for f in sorted(src.glob(getattr(args, "match", "*") + ".json" if not getattr(args, "match", "*").endswith(".json") else args.match)):
        try:
            ok, msg = store.save_evaluation(db, json.loads(f.read_text(encoding="utf-8")))
        except (KeyError, ValueError, TypeError) as e:
            ok, msg = False, f"bad file: {e}"
        if ok:
            report["saved"] += 1
            f.replace(done / f.name)
            (HERE / "bundles" / f.name).unlink(missing_ok=True)
        else:
            report["rejected"].append({"file": f.name, "reason": msg})
    store.log_run(db, "ingest", [], "", "", report)
    db.commit()
    print(json.dumps(report, indent=1))


def active_repos(gh, org, since, verbose=False):
    """Non-archived repos of the org with a push on or after `since`."""
    out = []
    for r in gh.org_repos(org):
        if r["archived"] or (r["pushed_at"] or "")[:10] < since:
            continue
        if verbose:
            print(f"# pushed {r['pushed_at'][:10]}  default branch {r['default_branch']}{'  (fork)' if r['fork'] else ''}", file=sys.stderr)
        out.append(r["full_name"])
    return out


def cmd_people(db):
    """Identity check: every person with their aliases and activity, and identities that look unlinked
    (commit emails GitHub doesn't map to an account, or two records sharing a name)."""
    people = [dict(r) for r in db.execute("""SELECT u.uid, u.login, u.name, u.is_bot,
        (SELECT count(*) FROM commits c WHERE c.author_uid=u.uid AND c.message NOT LIKE 'Merge %') commits,
        (SELECT count(*) FROM commit_coauthors ca WHERE ca.uid=u.uid) coauthored,
        (SELECT count(*) FROM pull_requests p WHERE p.author_uid=u.uid) prs,
        (SELECT group_concat(alias, ', ') FROM user_aliases a WHERE a.uid=u.uid) aliases
        FROM users u ORDER BY u.is_bot, commits DESC""")]
    names = {}
    for p in people:
        for k in {x.lower() for x in (p["login"], p["name"]) if x}:
            names.setdefault(k, set()).add(p["uid"])
    for p in people:
        p["flags"] = [f for f in [
            "unlinked email - merge into the right person" if p["uid"].startswith("email:") and not p["is_bot"] else None,
            "no GitHub id" if p["uid"].startswith("login:") and not p["is_bot"] else None,
            "same name as " + ", ".join(sorted(o for k in {x.lower() for x in (p["login"], p["name"]) if x} for o in names[k] if o != p["uid"]))
            if any(len(names[k]) > 1 for k in {x.lower() for x in (p["login"], p["name"]) if x}) else None] if f]
    print(json.dumps({"people": people,
                      "needs_attention": [p for p in people if p["flags"] and not p["is_bot"]]}, indent=1))


def cmd_status(db, args):
    print(json.dumps({**store.meta(db),
                      "users": db.execute("SELECT count(*) FROM users WHERE is_bot=0").fetchone()[0],
                      "prs": db.execute("SELECT count(*) FROM pull_requests").fetchone()[0],
                      "commits": db.execute("SELECT count(*) FROM commits").fetchone()[0]}, indent=1))


# ------------------------------------------------------------------ dashboard server
def load_users():
    """Dashboard logins from DASHBOARD_USERS="alice:pw1,bob:pw2" or DASHBOARD_PASSWORD (user "admin")."""
    users = {}
    for pair in filter(None, (os.environ.get("DASHBOARD_USERS") or "").split(",")):
        name, _, pw = pair.strip().partition(":")
        if name and pw:
            users[name] = pw
    if os.environ.get("DASHBOARD_PASSWORD"):
        users.setdefault(os.environ.get("DASHBOARD_USER", "admin"), os.environ["DASHBOARD_PASSWORD"])
    return users


def cmd_serve(db_path, port, open_browser=False, host="127.0.0.1", readonly=False, public=False):
    import base64, hmac
    users = load_users()
    local = host in ("127.0.0.1", "localhost", "::1")
    if not users and not local and not public:
        raise SystemExit("Refusing to serve on a network address without a login: the dashboard shows people's scores "
                         "and security findings. Set DASHBOARD_PASSWORD (or DASHBOARD_USERS), or pass --public.")
    if not Path(db_path).exists():
        raise SystemExit(f"Database not found: {db_path}")

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "pr-audit"
        sys_version = ""

        def log_message(self, fmt, *a):
            if not local:                                # keep an access log when hosted
                sys.stderr.write(f"{self.address_string()} {fmt % a}\n")

        def send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def authorised(self):
            if not users:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    name, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except ValueError:
                    return False
                return name in users and hmac.compare_digest(users[name].encode(), pw.encode())
            return False

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/healthz":                     # for load balancers / container health checks
                return self.send(200, {"ok": True})
            if not self.authorised():
                return self.send(401, {"error": "login required"}, extra={"WWW-Authenticate": 'Basic realm="PR audit"'})
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            since, until, repo = q.get("since", "0000-00-00"), q.get("until", "9999-12-31"), q.get("repo") or None
            db = store.connect(db_path, readonly=readonly)
            try:
                if u.path in ("/", "/index.html"):
                    return self.send(200, (HERE / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/meta":
                    return self.send(200, store.meta(db))
                if u.path == "/api/team":
                    return self.send(200, store.team(db, since, until, repo))
                if u.path.startswith("/api/user/"):
                    d = store.user_detail(db, urllib.parse.unquote(u.path[10:]), since, until, repo)
                    return self.send(200 if d else 404, d or {"error": "not found"})
                if u.path.startswith("/api/pr/"):
                    d = store.pr_detail(db, int(u.path[8:]))
                    return self.send(200 if d else 404, d or {"error": "not found"})
                if u.path == "/api/standards":
                    return self.send(200, (HERE / "STANDARDS.md").read_bytes(), "text/plain; charset=utf-8")
                self.send(404, {"error": "not found"})
            except (ValueError, KeyError):
                self.send(400, {"error": "bad request"})
            finally:
                db.close()

    srv = http.server.ThreadingHTTPServer((host, port), H)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"Dashboard running at {url}  (listening on {host}; login {'on, ' + str(len(users)) + ' user(s)' if users else 'off'}"
          f"{'; read-only' if readonly else ''})  - Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("AUDIT_DB") or str(HERE / "audit.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "pending"):
        p = sub.add_parser(name)
        p.add_argument("--repos", nargs="+", help="org/repo or GitHub URLs")
        if name == "collect":
            p.add_argument("--org", help="collect every repo of this org pushed in the period")
        p.add_argument("--months", type=int, default=3)
        p.add_argument("--since")
        p.add_argument("--until")
        if name == "pending":
            p.add_argument("--limit", type=int, default=0)
    d = sub.add_parser("diff"); d.add_argument("repo"); d.add_argument("number", type=int)
    ig = sub.add_parser("ingest"); ig.add_argument("--match", default="*", help="only eval files matching this glob, e.g. *instapract-api*")
    sub.add_parser("status"); sub.add_parser("people")
    rp = sub.add_parser("repos"); rp.add_argument("--org", required=True); rp.add_argument("--months", type=int, default=3)
    rp.add_argument("--since"); rp.add_argument("--until")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="0.0.0.0 to accept connections from other machines")
    s.add_argument("--open", action="store_true")
    s.add_argument("--readonly", action="store_true", default=os.environ.get("AUDIT_READONLY") == "1",
                   help="open the database read-only (hosted dashboard)")
    s.add_argument("--public", action="store_true", help="allow a network address without a login (not recommended)")
    sc = sub.add_parser("score", help="score pending PRs without Claude Code")
    sc.add_argument("--engine", choices=["heuristic", "anthropic"], default="heuristic",
                    help="heuristic = rule-based, free, no LLM; anthropic = LLM review via ANTHROPIC_API_KEY")
    sc.add_argument("--repos", nargs="+"); sc.add_argument("--months", type=int, default=3)
    sc.add_argument("--since"); sc.add_argument("--until"); sc.add_argument("--limit", type=int, default=0)
    b = sub.add_parser("backup", help="consistent copy of the database"); b.add_argument("--out", required=True)
    m = sub.add_parser("merge-users"); m.add_argument("from_uid"); m.add_argument("into_uid")
    args = ap.parse_args()

    if args.cmd == "serve":
        if not args.readonly:
            store.connect(args.db).close()          # create / migrate the schema
        return cmd_serve(args.db, args.port, args.open, args.host, args.readonly, args.public)
    if args.cmd == "backup":
        return cmd_backup(args.db, args.out)
    db = store.connect(args.db)
    if args.cmd == "collect":
        import github
        gh = github.GitHub(github.load_token())
        since, until = window(args)
        if not args.repos and not args.org:
            raise SystemExit("collect needs --repos or --org")
        targets = list(args.repos or []) + (active_repos(gh, args.org, since) if args.org else [])
        all_stats = {}
        for r in dict.fromkeys(targets):
            name = parse_repo(r)
            all_stats[name] = collect(db, gh, name, since, until)
            print(f"{name}: {all_stats[name]}", flush=True)
        store.log_run(db, "collect", list(all_stats), since, until, all_stats)
        db.commit()
    elif args.cmd == "repos":
        import github
        since, _ = window(args)
        for r in active_repos(github.GitHub(github.load_token()), args.org, since, verbose=True):
            print(r)
    elif args.cmd == "people":
        cmd_people(db)
    elif args.cmd == "pending":
        cmd_pending(db, args)
    elif args.cmd == "score":
        cmd_score(db, args)
    elif args.cmd == "diff":
        import github
        # full diff (bundles hold the capped copy); raw UTF-8 so Windows consoles don't choke on non-ASCII
        sys.stdout.buffer.write(github.GitHub(github.load_token()).pr_diff(parse_repo(args.repo), args.number).encode("utf-8"))
    elif args.cmd == "ingest":
        cmd_ingest(db, args)
    elif args.cmd == "status":
        cmd_status(db, args)
    elif args.cmd == "merge-users":
        store.merge_users(db, args.from_uid, args.into_uid)
        db.commit()
        print("merged")


if __name__ == "__main__":
    main()
