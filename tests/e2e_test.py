"""End-to-end test with a fake GitHub (no network): collect 3 months -> re-run -> collect 6 months
(after a PR got new commits) -> bundles -> simulated Claude evals -> ingest -> dashboard API checks."""
import json, sys, tempfile, shutil, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp())
for f in ["audit.py", "store.py", "github.py", "scoring.py", "schema.sql", "STANDARDS.md"]:
    shutil.copy(SRC / f, WORK / f)
shutil.copytree(SRC / "dashboard", WORK / "dashboard")
sys.path.insert(0, str(WORK))
import github, store, audit  # noqa: E402

EXTRA_COMMITS = {}
U = {"asha": 101, "ravi": 102, "meera": 103, "kiran": 104}
def actor(login): return {"login": login, "databaseId": U.get(login), "name": login.title(), "__typename": "Bot" if login.endswith("[bot]") else "User"}
def cm(oid, msg, date, login=None, email=None, co=None):
    main = {"email": email or f"{login}@example.com", "name": (login or email).title(),
            "user": {"login": login, "databaseId": U[login]} if login else None}
    authors = [main] + ([{"email": f"{co}@example.com", "name": co.title(), "user": {"login": co, "databaseId": U[co]}}] if co else [])
    return {"commit": {"oid": oid, "message": msg, "authoredDate": date, "additions": 10, "deletions": 2,
                       "author": main, "authors": {"nodes": authors}, "statusCheckRollup": {"state": "SUCCESS"}}}

def pr(n, title, author, created, state="MERGED", merged=None, commits=(), reviews=(), threads=(), files=("src/app.py",), head=None, ci="SUCCESS", head_ref=None, base="main"):
    cs = [cm(*c) for c in commits]
    if cs: cs[-1]["commit"]["statusCheckRollup"] = {"state": ci}
    EXTRA_COMMITS[n] = cs[2:]                      # page size 2 -> exercises commit pagination
    return {"number": n, "title": title, "body": f"Implements {title}. Tested locally.", "url": f"https://github.com/gem3s/x/pull/{n}",
            "state": state, "createdAt": created + "T09:00:00Z", "updatedAt": (merged or max([created] + [c[2][:10] for c in commits])) + "T13:00:00Z", "mergedAt": (merged + "T12:00:00Z") if merged else None,
            "closedAt": (merged + "T12:00:00Z") if merged else None, "additions": 120, "deletions": 30, "changedFiles": len(files),
            "baseRefName": base, "headRefName": head_ref or f"feature/{n}", "headRefOid": head or f"head{n}", "author": actor(author),
            "files": {"nodes": [{"path": f} for f in files]},
            "commits": {"totalCount": len(cs), "nodes": cs[:2], "pageInfo": {"hasNextPage": len(cs) > 2, "endCursor": "2"}},
            "reviews": {"nodes": [{"databaseId": n * 100 + i, "state": s, "body": "", "submittedAt": d + "T10:00:00Z", "author": actor(r)} for i, (r, s, d) in enumerate(reviews)]},
            "reviewThreads": {"nodes": [{"id": f"T{n}_{i}", "isResolved": res, "isOutdated": False, "path": files[0], "line": 40 + i,
                                          "resolvedBy": actor(author) if res else None,
                                          "comments": {"nodes": [{"databaseId": n * 1000 + i * 10 + j, "body": b, "createdAt": d + "T10:00:00Z", "author": actor(a)}
                                                                 for j, (a, b, d) in enumerate(cmts)]}} for i, (res, cmts) in enumerate(threads)]},
            "comments": {"nodes": []}}

REPOS = {
  "gem3s/payments": [
    pr(31, "Add refund API", "asha", "2026-06-02", merged="2026-06-04", commits=[("p31a", "feat(refund): add refund endpoint", "2026-06-02T09:00:00Z", "asha")],
       reviews=[("ravi", "APPROVED", "2026-06-03")], files=("api/refund.py",)),
    pr(40, "Retry on gateway timeout", "asha", "2026-09-10", merged="2026-09-12",
       commits=[("p40a", "fix stuff", "2026-09-10T09:00:00Z", "asha"), ("p40b", "add test", "2026-09-11T09:00:00Z", "asha")],
       reviews=[("ravi", "CHANGES_REQUESTED", "2026-09-11"), ("ravi", "APPROVED", "2026-09-12")],
       threads=[(True, [("ravi", "Retries are unbounded - add a max attempts cap", "2026-09-11"), ("asha", "done", "2026-09-11")]),
                (True, [("ravi", "Please add a test for the timeout path", "2026-09-11")])], files=("gateway/retry.py",)),
    pr(44, "Cap retries to stop retry storm", "asha", "2026-09-20", merged="2026-09-21",
       commits=[("p44a", "fix(gateway): cap retries at 3 (follow-up #40)", "2026-09-20T09:00:00Z", "asha")],
       reviews=[("ravi", "APPROVED", "2026-09-21")], files=("gateway/retry.py",)),
    pr(47, "Ledger CSV export", "ravi", "2026-10-15", state="OPEN", ci="FAILURE",
       commits=[("p47a", "feat(ledger): csv export", "2026-10-15T09:00:00Z", "ravi"), ("p47b", "wip", "2026-10-16T09:00:00Z", None, "ravi.personal@gmail.com")],
       reviews=[("meera", "COMMENTED", "2026-10-16")],
       threads=[(False, [("meera", "This loads the whole ledger in memory; stream rows instead", "2026-10-16")])], files=("ledger/export.py",)),
    pr(25, "Experimental payout batching", "ravi", "2026-07-10", state="CLOSED", merged="2026-09-01",
       commits=[("p25a", "feat(payout): batch payouts", "2026-07-10T09:00:00Z", "ravi")], files=("payout/batch.py",)),
    pr(20, "Big refactor of fees", "meera", "2026-07-01", state="OPEN",
       commits=[("p20a", "refactor(fees): split module", "2026-07-01T09:00:00Z", "meera"), ("p20b", "refactor(fees): rename", "2026-07-02T09:00:00Z", "meera"),
                ("p20c", "refactor(fees): move tests", "2026-07-03T09:00:00Z", "meera"), ("p20d", "refactor(fees): finish split", "2026-10-30T09:00:00Z", "meera")], files=("fees/core.py",)),
    pr(49, "Bump requests", "dependabot[bot]", "2026-10-20", merged="2026-10-21", commits=[("p49a", "build(deps): bump requests", "2026-10-20T09:00:00Z", None, "bot@github.com")], files=("requirements.txt",)),
  ],
  "gem3s/orders": [
    pr(12, "Order status webhook", "asha", "2026-10-01", merged="2026-10-01", commits=[("o12a", "feat(webhook): order status events", "2026-10-01T09:00:00Z", "asha")],
       reviews=[("meera", "APPROVED", "2026-10-01")], files=("orders/webhook.py",)),
    pr(15, "Validate order payload", "ravi", "2026-08-25", merged="2026-08-27",
       commits=[("o15a", "feat(orders): validate payload schema", "2026-08-25T09:00:00Z", "ravi"), ("o15b", "test(orders): payload validation cases", "2026-08-26T09:00:00Z", None, "ravi.personal@gmail.com")],
       reviews=[("meera", "APPROVED", "2026-08-27")],
       threads=[(True, [("meera", "Nit: rename `d` to `payload`", "2026-08-26"), ("ravi", "Renamed in o15b", "2026-08-26")])], files=("orders/validate.py",)),
    pr(18, "Order search API", "meera", "2026-10-22", merged="2026-10-24", commits=[("o18a", "feat(search): add order search with pagination", "2026-10-22T09:00:00Z", "meera")],
       reviews=[("kiran", "APPROVED", "2026-10-23")], files=("orders/search.py",)),
    # shared PR: owner asha, a commit by meera, a commit co-authored by kiran, and a merge commit by ravi (not authored work)
    pr(22, "Invoice PDF", "asha", "2026-10-05", merged="2026-10-08",
       commits=[("o22a", "feat(invoice): pdf layout", "2026-10-05T09:00:00Z", "asha"),
                ("o22b", "fix", "2026-10-06T09:00:00Z", "meera"),
                ("o22c", "feat(invoice): totals and tax lines", "2026-10-07T09:00:00Z", "asha", None, "kiran"),
                ("o22d", "Merge branch 'main' into feature/22", "2026-10-07T12:00:00Z", "ravi")],
       reviews=[("ravi", "APPROVED", "2026-10-08")], files=("orders/invoice.py",)),
    pr(19, "Merge main into prod", "meera", "2026-10-25", merged="2026-10-25", commits=[("o12a", "feat(webhook): order status events", "2026-10-01T09:00:00Z", "asha")],
       reviews=[("kiran", "APPROVED", "2026-10-25")], files=("orders/webhook.py", "orders/search.py"), head_ref="main", base="prod"),
  ],
}
DIRECT = {"gem3s/orders": [{"oid": "d001", "message": "hotfix: disable flaky cron", "authoredDate": "2026-09-05T08:00:00Z", "additions": 1, "deletions": 1,
                            "author": {"email": "kiran@example.com", "name": "Kiran", "user": {"login": "kiran", "databaseId": 104}}, "associatedPullRequests": {"totalCount": 0}}]}


class FakeGH:
    calls = 0
    def __init__(self, token=None): pass
    def graphql(self, query, v):
        FakeGH.calls += 1
        full = f"{v['owner']}/{v['name']}"
        if "pullRequest(number" in query:
            return {"repository": {"pullRequest": {"commits": {"nodes": EXTRA_COMMITS[v["number"]], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if "pullRequests" in query:
            nodes = sorted(REPOS[full], key=lambda p: p["updatedAt"], reverse=True)
            start = int(v["cursor"] or 0)
            page = nodes[start:start + 3]                    # small pages to exercise pagination
            return {"repository": {"url": f"https://github.com/{full}", "defaultBranchRef": {"name": "main"},
                    "pullRequests": {"nodes": page, "pageInfo": {"hasNextPage": start + 3 < len(nodes), "endCursor": str(start + 3)}}}}
        hist = [c for c in DIRECT.get(full, []) if v["since"] <= c["authoredDate"] <= v["until"]]
        return {"repository": {"defaultBranchRef": {"target": {"history": {"nodes": hist, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}
    def commit_files(self, full, sha):
        return [{"file": "orders/invoice.py", "additions": 10, "deletions": 2}]
    def pr_diff(self, full, n):
        return f"diff --git a/x b/x\n+ change for {full}#{n}\n"

github.GitHub = FakeGH
github.load_token = lambda *a: "fake"

TODAY = "2026-11-03"
def months_back(m):
    class A: pass
    a = A(); a.since = None; a.until = TODAY; a.months = m
    return audit.window(a)

db = store.connect(str(WORK / "audit.db"))
def collect(m):
    s, u = months_back(m)
    return {r: audit.collect(db, FakeGH(), r, s, u) for r in REPOS}
def count(t): return db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]

# ---- Run 1: last 3 months
r1 = collect(3); c1 = {t: count(t) for t in ["pull_requests", "commits", "reviews", "comments", "users", "rework_links"]}
print("run1", r1, c1)
assert c1["pull_requests"] == 11, c1                 # PR #31 (June) is outside 3 months
# ---- identical re-run => nothing changes
collect(3); assert {t: count(t) for t in c1} == c1, "re-run duplicated rows"
# ---- PR #47 gets a new commit, then 6-month run
REPOS["gem3s/payments"][3]["headRefOid"] = "head47-v2"
REPOS["gem3s/payments"][3]["updatedAt"] = "2026-10-20T10:00:00Z"
r2 = collect(6); c2 = {t: count(t) for t in c1}
print("run2", r2, c2)
assert c2["pull_requests"] == 12 and r2["gem3s/payments"]["new"] == 1 and r2["gem3s/payments"]["updated"] == 1
assert db.execute("SELECT count(*) FROM (SELECT repo_id, sha FROM commits GROUP BY 1,2 HAVING count(*)>1)").fetchone()[0] == 0

# ---- identity: personal email (unlinked) merged into ravi; bot flagged
ravi = db.execute("SELECT uid FROM user_aliases WHERE alias='ravi.personal@gmail.com'").fetchone()[0]
print("ravi.personal@gmail.com ->", ravi)
assert ravi == "gh:102", ravi
assert db.execute("SELECT count(*) FROM users WHERE uid LIKE 'email:ravi%'").fetchone()[0] == 0
assert db.execute("SELECT is_bot FROM users WHERE login='dependabot[bot]'").fetchone()[0] == 1

# ---- rework candidate #44 -> #40 proposed
rw = [dict(r) for r in db.execute("SELECT l.number later, o.number orig, rl.kind, rl.reason FROM rework_links rl JOIN pull_requests l ON l.id=rl.pr_id JOIN pull_requests o ON o.id=rl.original_pr")]
print("rework candidates", rw)
assert any(r["later"] == 44 and r["orig"] == 40 and r["kind"] == "self_fix" for r in rw)

assert db.execute("SELECT kind FROM pull_requests WHERE number=19").fetchone()[0] == "promotion"
assert not any(19 in (r["later"], r["orig"]) for r in rw)
# ---- bundles for pending PRs (bots excluded)
class P: pass
p = P(); p.since = None; p.until = TODAY; p.months = 6; p.repos = None; p.limit = 0
audit.cmd_pending(db, p)
bundles = sorted((WORK / "bundles").glob("*.json"))
assert len(bundles) == 10, [b.name for b in bundles]   # 12 PRs minus dependabot and the promotion PR
b22 = json.loads((WORK / "bundles" / "gem3s__orders__22.json").read_text())
assert b22["shared_pr"] and {(x["login"], x["role"]) for x in b22["people"]} == {("asha", "owner"), ("meera", "contributor"), ("kiran", "contributor")}, b22["people"]
assert all("files" in c for c in b22["commits"] if not c["message"].startswith("Merge ")), "per-commit files for shared PRs"
assert db.execute("SELECT count(*) FROM commit_coauthors").fetchone()[0] == 1
assert db.execute("SELECT count(*) FROM commits c JOIN pull_requests p ON p.id=c.pr_id WHERE p.number=20").fetchone()[0] == 4, "commit pagination"
assert db.execute("SELECT state FROM pull_requests WHERE number=25").fetchone()[0] == "closed"

# ---- simulated Claude evaluations (shape defined in STANDARDS.md)
(WORK / "evals").mkdir(exist_ok=True)
def ev(b, scores, **kw):
    return {"repo": b["repo"], "number": b["number"], "head_sha": b["head_sha"],
            "scores": dict(zip(["commit_msg", "code_quality", "validation", "review_response"], scores)),
            "tests_added": True, "commit_notes": "Conventional, clear.", "code_issues": [], "threads": [], "reviewers": [],
            "rework": [], "summary": "Clean, focused change; review was quick and appropriate.", "talking_points": ["Good scoping and naming."], **kw}
for f in bundles:
    b = json.loads(f.read_text())
    n = b["number"]
    if n == 40:
        out = ev(b, [1, 2, 4, 2], commit_notes="'fix stuff' and 'add test' are not descriptive; no conventional type.",
                 code_issues=[{"severity": "major", "file": "gateway/retry.py", "line": 41, "issue": "Retry loop has no max attempts; can cause retry storm under gateway outage", "caught_by_reviewer": True},
                              {"severity": "minor", "file": "gateway/retry.py", "line": 55, "issue": "Fixed sleep instead of exponential backoff with jitter", "caught_by_reviewer": False}],
                 validation_notes="Test added for timeout path after review; no test for repeated failures.",
                 threads=[{"comment_id": b["threads"][0]["comment_id"], "validity": "valid", "resolution": "dismissed", "note": "Author replied 'done' and resolved, but no commit adds a cap. Fixed later in #44."},
                          {"comment_id": b["threads"][1]["comment_id"], "validity": "valid", "resolution": "fixed_in_code", "fix_sha": "p40b", "note": "Test added in p40b."}],
                 reviewers=[{"login": "ravi", "score": 3, "missed": ["Did not verify the 'done' reply actually capped retries before approving", "Fixed sleep vs backoff not raised"], "notes": "Raised the right blocker but approved without checking the fix."}],
                 summary="Reviewer correctly flagged unbounded retries, but the thread was resolved with 'done' and no code change; the issue shipped and needed PR #44.",
                 talking_points=["Resolve a review thread only after pushing the fix, and reference the commit.", "Use descriptive Conventional Commit messages instead of 'fix stuff'.", "Good: added the requested timeout test quickly."])
    elif n == 44:
        out = ev(b, [5, 4, 3, 5], tests_added=False, validation_notes="No regression test for the cap.",
                 rework=[{"original_pr": 40, "kind": "self_fix", "confirmed": True, "evidence": "Adds the max-attempts cap requested in #40 review thread (gateway/retry.py:41)."}],
                 reviewers=[{"login": "ravi", "score": 3, "missed": ["Regression test for the cap not requested"], "notes": "Approved small fix quickly."}],
                 summary="Follow-up fix for #40's unbounded retries; message references the original PR.",
                 talking_points=["Fix is correct; add a regression test so the storm can't come back."])
    elif n == 47:
        out = ev(b, [2, 3, 2, 3], commit_notes="'wip' commit left in history.",
                 code_issues=[{"severity": "major", "file": "ledger/export.py", "line": 40, "issue": "Loads entire ledger into memory", "caught_by_reviewer": True}],
                 validation_notes="CI failing; no tests.",
                 threads=[{"comment_id": b["threads"][0]["comment_id"], "validity": "valid", "resolution": "open", "note": "Not yet addressed."}],
                 reviewers=[{"login": "meera", "score": 5, "missed": [], "notes": "Caught the memory issue."}],
                 summary="Open PR with failing CI and an unaddressed valid comment.", talking_points=["Fix CI before requesting review.", "Squash the 'wip' commit."])
    elif n == 15:
        out = ev(b, [5, 4, 5, 5], threads=[{"comment_id": b["threads"][0]["comment_id"], "validity": "nitpick", "resolution": "fixed_in_code", "fix_sha": "o15b", "note": "Renamed."}],
                 reviewers=[{"login": "meera", "score": 4, "missed": [], "notes": "Reasonable review."}])
    elif n == 12:
        out = ev(b, [4, 3, 2, 4], tests_added=False, validation_notes="No tests for webhook retries; merged same day.",
                 code_issues=[{"severity": "major", "file": "orders/webhook.py", "line": 22, "issue": "Webhook failures are swallowed with bare except", "caught_by_reviewer": False}],
                 reviewers=[{"login": "meera", "score": 2, "missed": ["Swallowed exceptions in webhook sender"], "notes": "Approved with no comments."}],
                 summary="Feature works but errors are swallowed; approved without comments.", talking_points=["Never swallow exceptions; log and surface failures.", "Add tests for webhook failure paths."])
    elif n == 22:     # first attempt forgets the contributors -> must be rejected
        out = ev(b, [3, 3, 4, 4], code_issues=[{"severity": "major", "file": "orders/invoice.py", "line": 12, "issue": "Tax rounding per line",
                                                  "author": "meera", "commit_sha": "o22b", "caught_by_reviewer": False}])
    else:
        out = ev(b, [4, 4, 4, 4])
    (WORK / "evals" / f.name).write_text(json.dumps(out))
# one stale eval (wrong head) must be rejected
(WORK / "evals" / "stale.json").write_text(json.dumps({**ev({"repo": "gem3s/orders", "number": 18, "head_sha": "old"}, [1, 1, 1, 1])}))
audit.cmd_ingest(db, None)
assert count("pr_evaluations") == 9, count("pr_evaluations")
assert (WORK / "evals" / "stale.json").exists()
f22 = WORK / "evals" / "gem3s__orders__22.json"
assert f22.exists(), "shared PR without contributors must be rejected"
e22 = json.loads(f22.read_text())
e22["contributors"] = [{"login": "asha", "commit_msg": 5, "code_quality": 4, "notes": "Clear conventional commits."},
                       {"login": "Meera", "commit_msg": 1, "code_quality": 2, "notes": "'fix' message; introduced the rounding bug."},
                       {"login": "kiran", "commit_msg": 5, "code_quality": 4, "notes": "Co-authored the totals commit."}]
f22.write_text(json.dumps(e22))
audit.cmd_ingest(db, None)
assert count("pr_evaluations") == 10 and not f22.exists()
pc = {r["login"]: dict(r) for r in db.execute("SELECT u.login, pc.* FROM pr_contributions pc JOIN users u ON u.uid=pc.uid "
                                              "JOIN pull_requests p ON p.id=pc.pr_id WHERE p.number=22")}
assert set(pc) == {"asha", "meera", "kiran"}, pc                 # ravi's merge commit is not authored work
assert pc["asha"]["is_owner"] == 1 and pc["asha"]["validation"] == 4 and pc["meera"]["validation"] is None
assert pc["meera"]["overall"] == round((0.15 * 1 + 0.40 * 2) / 0.55, 2), pc["meera"]
assert count("pr_contributions") == 10 + 2                         # one owner row per PR + two contributors on #22
# re-ingesting same results: no duplicates, nothing pending
audit.cmd_pending(db, p)
assert len(store.prs_needing_evaluation(db, "2026-05-03", TODAY)) == 0

# ---- dashboard API
team = store.team(db, "2026-05-03", TODAY)
print("\nTEAM"); [print({k: t.get(k) for k in ("login", "prs", "repos", "overall", "reworked", "ignored_comments", "direct_pushes", "reviews_given")}) for t in team]
asha = next(t for t in team if t["login"] == "asha")
assert next(t for t in team if t["login"] == "ravi")["prs"] == 3   # includes the closed PR
assert asha["prs"] == 5 and asha["repos"] == 2 and asha["reworked"] == 1 and asha["ignored_comments"] == 1
assert not any(t["login"] == "dependabot[bot]" for t in team)
d = store.user_detail(db, asha["uid"], "2026-05-03", TODAY)
assert {p["repo"] for p in d["projects"]} == {"gem3s/payments", "gem3s/orders"}
pr40 = next(x for x in d["prs"] if x["number"] == 40)
det = store.pr_detail(db, pr40["id"])
assert det["fixed_by"][0]["number"] == 44 and det["threads"][0]["eval"]["resolution"] == "dismissed"
kiran = next(t for t in team if t["login"] == "kiran")
assert kiran["direct_pushes"] == 1 and kiran["contributed"] == 1 and kiran["commits"] == 2   # co-authored + direct push
meera = next(t for t in team if t["login"] == "meera")
assert meera["contributed"] == 1
dm = store.user_detail(db, meera["uid"], "2026-05-03", TODAY)
p22 = next(x for x in dm["prs"] if x["number"] == 22)
assert p22["role"] == "contributor" and p22["code_quality"] == 2 and p22["validation"] is None
assert [i["number"] for i in dm["my_issues"]] == [22], dm["my_issues"]
assert not any(i["number"] == 22 for i in store.user_detail(db, asha["uid"], "2026-05-03", TODAY)["my_issues"])
det22 = store.pr_detail(db, p22["id"])
assert len(det22["people"]) == 3 and all(x["scored"] for x in det22["people"])

# ---- rule-based engine (no LLM): shape accepted by the importer, shared PR gets per-committer rows
import scoring
b22 = json.loads((WORK / "bundles" / "gem3s__orders__22.json").read_text()) if (WORK / "bundles" / "gem3s__orders__22.json").exists() \
    else audit.make_bundle(db, p22["id"], "diff --git a/x.py b/x.py\n+++ b/x.py\n+print('debug')\n", FakeGH())
h = scoring.heuristic_eval(b22, "diff --git a/orders/invoice.py b/orders/invoice.py\n+++ b/orders/invoice.py\n"
                                "+API_KEY = 'abcd1234efgh5678ijkl'\n+x = 1\n")
assert h["engine"] == "heuristic" and h["scores"]["code_quality"] == 1, h["scores"]      # hard-coded secret -> blocker
assert {c["login"].lower() for c in h["contributors"]} >= {"asha", "meera"}, h["contributors"]
ok, msg = store.save_evaluation(db, h)
assert ok, msg
assert store.pr_detail(db, p22["id"])["engine"] == "heuristic"
assert any(r["number"] == 22 for r in store.prs_needing_evaluation(db, "2026-05-03", TODAY, replace_heuristic=True))
assert not any(r["number"] == 22 for r in store.prs_needing_evaluation(db, "2026-05-03", TODAY))
assert store.meta(db)["engines"].get("heuristic") == 1
db.commit()

# ---- hosted dashboard: login required, health check open, read-only database
import os, socket, threading, time, urllib.request, urllib.error, base64
with socket.socket() as s_:
    s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]
os.environ["DASHBOARD_PASSWORD"] = "pw-test"
threading.Thread(target=audit.cmd_serve, args=(str(WORK / "audit.db"), port), kwargs={"readonly": True}, daemon=True).start()
time.sleep(0.5)
base = f"http://127.0.0.1:{port}"
assert urllib.request.urlopen(base + "/healthz").status == 200
try:
    urllib.request.urlopen(base + "/api/meta"); raise AssertionError("no login must give 401")
except urllib.error.HTTPError as e:
    assert e.code == 401 and "Basic" in e.headers.get("WWW-Authenticate", "")
req = urllib.request.Request(base + "/api/meta", headers={"Authorization": "Basic " + base64.b64encode(b"admin:pw-test").decode()})
assert json.loads(urllib.request.urlopen(req).read())["engines"]["heuristic"] == 1
bad = urllib.request.Request(base + "/api/team", headers={"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()})
try:
    urllib.request.urlopen(bad); raise AssertionError("wrong password must give 401")
except urllib.error.HTTPError as e:
    assert e.code == 401
os.environ.pop("DASHBOARD_PASSWORD")
try:
    audit.cmd_serve(str(WORK / "audit.db"), port + 1, host="0.0.0.0"); raise AssertionError("network host without login must refuse")
except SystemExit as e:
    assert "login" in str(e)
audit.cmd_backup(str(WORK / "audit.db"), str(WORK / "copy.db"))
assert store.connect(str(WORK / "copy.db"), readonly=True).execute("SELECT count(*) FROM pr_evaluations").fetchone()[0] == count("pr_evaluations")
print("\nALL CHECKS PASSED. DB:", WORK / "audit.db")
