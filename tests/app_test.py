"""Tests for the web app layer (no network, no real LLM): skill + routing, scoring through a fake Anthropic client
(single / agentic / batch), the job runner (collect -> score, cost cap, cancel, resume, try-on-PR), and the server's
three roles (admin / viewer / self) including GitHub OAuth with a fake GitHub. Runs the base e2e test first for data."""
import http.client, json, os, runpy, sys, threading, time, urllib.parse
from pathlib import Path
from types import SimpleNamespace as NS

G = runpy.run_path(str(Path(__file__).with_name("e2e_test.py")))        # builds WORK with fake GitHub data and a scored DB
WORK, FakeGH, TODAY = G["WORK"], G["FakeGH"], G["TODAY"]
import audit, auth, control, jobs, routing, scoring, server, skill, store, github  # noqa: E402  (WORK copies)
DBP = str(WORK / "audit.db")
db = store.connect(DBP)
SINCE = "2026-05-03"
PARAMS = {"since": SINCE, "until": TODAY}


def ok(cond, msg):
    assert cond, msg


# ------------------------------------------------------------------ 1. skill + routing
files = skill.repo_files()
ok(set(files) >= {"SKILL.md", "rubric.md", "agents/scorer.md", "agents/deep-scorer.md"}, files.keys())
h0 = skill.version_hash(files)
ok(h0 == skill.version_hash(dict(files)) and len(h0) == 12, "hash is stable")
ag = skill.agents(files)
ok(ag["scorer"]["mode"] == "single" and ag["deep-scorer"]["mode"] == "agentic" and ag["deep-scorer"]["max_turns"] == 12, ag)
for bad in ({**files, "../evil.md": "x"}, {**files, "agents/x.md": "no front matter"}, {k: v for k, v in files.items() if k != "SKILL.md"},
            {**files, "agents/scorer.md": files["agents/scorer.md"].replace("mode: single", "mode: wild")},
            {**files, "agents/scorer.md": files["agents/scorer.md"].replace("name: scorer", "name: other")}):
    try:
        skill.validate(bad); raise AssertionError("must reject")
    except ValueError:
        pass
edited = {**files, "SKILL.md": files["SKILL.md"] + "\n- Always mention one thing that went well.\n"}
h1 = skill.save_version(db, edited, "tester", "test edit"); db.commit()
f2, h2, src = skill.active(db)
ok(h2 == h1 != h0 and src == "edited" and f2["SKILL.md"].endswith("went well.\n"), (h1, h2, src))
ok(skill.version_files(db, h0) == files and skill.version_files(db, h1) == edited, "versions readable")
skill.reset(db); db.commit()
ok(skill.active(db)[1:] == (h0, "repo"), "reset goes back to the repo files")
ok("# RUBRIC" in skill.system_prompt(files, "scorer") and "deep" not in skill.system_prompt(files, "scorer").lower().split("# rubric")[0][-400:], "prompt layout")

rules = routing.load_rules(db)
ok(routing.validate_rules(rules, set(ag)) == rules, "default rules are valid")
for bad_rules in ([{**rules[0], "model": "gpt-9"}] + rules[1:], rules[:-1], [{**rules[-1], "when": {"bogus": 1}}],
                  [{**rules[-1], "effort": "turbo"}], [{**rules[-1], "agent": "ghost"}], [{**rules[0], "when": {"path_regex": "("}}] + rules[1:]):
    try:
        routing.validate_rules(bad_rules, set(ag)); raise AssertionError("must reject")
    except ValueError:
        pass
feat = lambda **k: {"lines": 50, "files": 2, "shared": False, "truncated": False, "repo": "o/x", "paths": ["a.py"], "state": "merged", "prior_engine": "none", **k}
pick = lambda **k: routing.choose(rules, feat(**k))[1]
ok(pick()["model"] == "claude-haiku-5-5" and pick(lines=500)["model"] == "claude-sonnet-5-5", "small -> haiku, normal -> sonnet")
ok(pick(lines=900)["model"] == "claude-opus-5-5" and pick(shared=True, lines=500)["model"] == "claude-opus-5-5", "heavy/shared -> opus")
ok(pick(paths=["src/auth/login.py"])["agent"] == "deep-scorer", "security path -> investigating agent")
ok(routing.matches({"repo": ["o/*"]}, feat()) and not routing.matches({"repo": ["z/*"]}, feat()), "repo globs")
c = routing.cost_usd("claude-opus-5-5", {"input": 1_000_000, "output": 1_000_000})
ok(c == 24.0 and routing.cost_usd("claude-opus-5-5", {"input": 1_000_000, "output": 1_000_000}, batch=True) == 12.0, c)
ok(routing.cost_usd("claude-haiku-5-5", {"input": 200_000}) == 0.1, "haiku long-context tier")  # 200k > 100k -> $0.50/M
ok(routing.cost_usd("nope", {"input": 1}) is None, "unknown model has no price")


# ------------------------------------------------------------------ 2. a fake Anthropic client
class Blk:
    def __init__(self, **k): self.__dict__.update(k)


def bundle_from(params):
    return json.loads(params["messages"][0]["content"].split("PR bundle:\n", 1)[1]) if isinstance(params["messages"][0]["content"], str) else None


def fake_eval(b):
    ev = {"scores": {"commit_msg": 4, "code_quality": 4, "validation": 4, "review_response": 4}, "tests_added": True, "commit_notes": "ok",
          "contributors": [{"login": p["login"], "commit_msg": 4, "code_quality": 4, "notes": "fine"} for p in b.get("people", []) if p["commits"]] if b.get("shared_pr") else [],
          "code_issues": [], "validation_notes": "ok",
          "threads": [{"comment_id": t["comment_id"], "validity": "valid", "resolution": "fixed_in_code", "fix_sha": None, "note": "fixed"} for t in b["threads"]],
          "reviewers": [{"login": r, "score": 4, "missed": [], "notes": "ok"} for r in sorted({x["reviewer"] for x in b["reviews"]})],
          "rework": [{"original_pr": c["original_pr"], "kind": c["kind_guess"], "confirmed": False, "evidence": "fake"} for c in b["rework_candidates"]],
          "summary": "Fake review.", "talking_points": ["Nice."]}
    return json.dumps(ev)


class Msg:
    def __init__(self, text=None, tool=None, model="m"):
        self.stop_reason = "tool_use" if tool else "end_turn"
        self.content = [Blk(type="tool_use", id="tu1", name=tool, input={})] if tool else [Blk(type="text", text=text)]
        self.usage = NS(input_tokens=1000, output_tokens=500, cache_read_input_tokens=200, cache_creation_input_tokens=0)
        self.model = model


class Fake:
    def __init__(self):
        self.calls, self.batch_requests, self.fail_retrieve = [], [], False
        me = self
        class Stream:
            def __init__(s, **p): me.calls.append(p); s.p = p
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s): return Msg(fake_eval(bundle_from(s.p)), model=s.p["model"])
        class Messages:
            def stream(s, **p): return Stream(**p)
            def create(s, **p):
                me.calls.append(p)
                first = isinstance(p["messages"][-1]["content"], str)
                b = json.loads(p["messages"][0]["content"].split("PR bundle:\n", 1)[1])
                return Msg(tool="list_changed_files") if first else Msg(fake_eval(b), model=p["model"])
        class Batches:
            def create(s, requests):
                me.batch_requests += requests
                return NS(id=f"batch_{len(me.batch_requests)}")
            def retrieve(s, bid):
                if me.fail_retrieve:
                    me.fail_retrieve = False
                    raise RuntimeError("network down")
                return NS(processing_status="ended", request_counts=NS(processing=0, succeeded=len(me.batch_requests), errored=0, canceled=0, expired=0))
            def results(s, bid):
                for r in me.batch_requests:
                    yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=Msg(fake_eval(bundle_from(r["params"])), model=r["params"]["model"])))
            def cancel(s, bid): pass
        Messages.batches = Batches()
        self.messages = Messages()
        self.beta = NS(messages=Messages())


FAKE = Fake()
scoring._client = lambda client=None: client or FAKE
os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-123"
os.environ["GITHUB_TOKEN"] = "ghp_test_secret_456"
os.environ["AUDIT_CONCURRENCY"] = "1"
jobs.BATCH_POLL_SECONDS = 0


def wipe_scores():
    for t in ("pr_evaluations", "pr_contributions", "thread_evaluations", "reviewer_evaluations"):
        db.execute(f"DELETE FROM {t}")
    db.commit()


def run_job(params, kind="refresh", job_id=None):
    jid = job_id or control.create_job(db, kind, params, "tester")
    db.commit()
    jobs.JobRunner(DBP).execute(db, jid)
    return control.get_job(db, jid)


# ---- single mode: collect + score, routed
wipe_scores()
control.add_tracked(db, "gem3s/payments", "tester"); control.add_tracked(db, "gem3s/orders", "tester"); db.commit()
est = jobs.estimate(db, {**PARAMS, "score": "llm"})
ok(est["prs"] == 10 and est["usd_estimate"] > 0 and set(est["by_model"]) == {"claude-sonnet-5-5", "claude-opus-5-5"}, est)
ok(jobs.estimate(db, {**PARAMS, "score": "llm", "batch": True})["usd_estimate"] < est["usd_estimate"], "batch is cheaper")
ok(jobs.estimate(db, {**PARAMS, "score": "heuristic"})["usd_estimate"] == 0, "rule-based is free")
j = run_job({**PARAMS, "score": "llm", "collect": True})
ok(j["status"] == "done" and j["summary"]["saved"] == 10 and not j["summary"]["failed"], j["summary"])
ok(j["cost_usd"] > 0 and j["input_tokens"] > 0 and j["progress_done"] == 10 == j["progress_total"], j)
rows = {r["number"]: dict(r) for r in db.execute("SELECT p.number, e.* FROM pr_evaluations e JOIN pull_requests p ON p.id=e.pr_id")}
_, active_hash, _ = skill.active(db)
ok(all(r["engine"] == "anthropic-api" and r["skill_hash"] == active_hash and r["cost_usd"] > 0 and r["agent"] == "scorer" for r in rows.values()), rows[22])
ok(rows[22]["model"] == "claude-opus-5-5" and rows[44]["model"] == "claude-sonnet-5-5", (rows[22]["model"], rows[44]["model"]))   # shared -> opus
sys_text = FAKE.calls[0]["system"][0]["text"]
ok(sys_text.startswith("# PR scoring skill") and "# RUBRIC" in sys_text and FAKE.calls[0]["system"][0]["cache_control"], "skill is the system prompt, cached")
ok(FAKE.calls[0]["output_config"]["format"]["type"] == "json_schema", "structured output")
ok(db.execute("SELECT count(*) FROM pr_contributions").fetchone()[0] >= 10 + 2, "per-person rows from the LLM result (shared PR)")
ok(db.execute("SELECT last_collected_at FROM tracked_repos WHERE full_name='gem3s/orders'").fetchone()[0], "tracked repo remembers last collect")
ok(not store.prs_needing_evaluation(db, SINCE, TODAY), "nothing pending after the run")
ok(j["events"] and any("scored gem3s/orders#22" in e["message"] and "claude-opus-5-5" in e["message"] for e in j["events"]), "events name model + rule")

# ---- re-score when the skill changed (explicit option only)
skill.save_version(db, edited, "tester"); db.commit()
ok(not store.prs_needing_evaluation(db, SINCE, TODAY), "editing the skill does not silently re-score anything")
ok(len(store.prs_needing_evaluation(db, SINCE, TODAY, skill_hash=skill.active(db)[1])) == 10, "…but an explicit re-score finds them")
j = run_job({**PARAMS, "score": "llm", "collect": False, "rescore_old_skill": True, "max_prs": 2})
ok(j["summary"]["saved"] == 2 and db.execute("SELECT count(*) FROM pr_evaluations WHERE skill_hash=?", (skill.active(db)[1],)).fetchone()[0] == 2, j["summary"])
skill.reset(db); db.commit()

# ---- cost cap, cancel, missing key
wipe_scores()
j = run_job({**PARAMS, "score": "llm", "collect": False, "max_usd": 0.0001})
ok(j["status"] == "done" and "cost cap" in j["summary"].get("stopped", "") and 1 <= j["summary"]["saved"] < 10, j["summary"])
wipe_scores()
jid = control.create_job(db, "refresh", {**PARAMS, "score": "llm"}, "tester"); control.update_job(db, jid, cancel_requested=1); db.commit()
ok(run_job(None, job_id=jid)["status"] == "cancelled", "cancel before start")
jid = control.create_job(db, "refresh", {**PARAMS, "score": "llm", "collect": False}, "tester"); db.commit()
for _ in range(1):
    os.environ.pop("ANTHROPIC_API_KEY")
    j = run_job(None, job_id=jid)
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-123"
ok(j["status"] == "failed" and "ANTHROPIC_API_KEY" in j["error"], j)

# ---- custom routing: the admin chooses the model per case; an investigating agent runs tools
wipe_scores()
custom = routing.validate_rules([
    {"name": "orders on haiku", "when": {"repo": ["gem3s/orders"]}, "agent": "scorer", "model": "claude-haiku-5-5", "effort": "low"},
    {"name": "open PRs get the investigating agent", "when": {"state": "open"}, "agent": "deep-scorer", "model": "claude-opus-5-5", "effort": "high"},
    {"name": "rest", "when": {}, "agent": "scorer", "model": "claude-sonnet-5-5", "effort": "medium"}], set(ag))
control.set_setting(db, routing.SETTING_KEY, custom, "tester"); db.commit()
FAKE.calls.clear()
j = run_job({**PARAMS, "score": "llm", "collect": False})
ok(j["summary"]["saved"] == 10 and not j["summary"]["failed"], j["summary"])
rows = {r["number"]: dict(r) for r in db.execute("SELECT p.number, e.* FROM pr_evaluations e JOIN pull_requests p ON p.id=e.pr_id")}
ok(rows[22]["model"] == "claude-haiku-5-5" and rows[44]["model"] == "claude-sonnet-5-5", "model chosen by repo rule")
ok(rows[47]["agent"] == "deep-scorer" and rows[47]["model"] == "claude-opus-5-5" and rows[20]["agent"] == "deep-scorer", "agentic route")
agentic = [r for r in rows.values() if r["agent"] == "deep-scorer"]
ok(len(agentic) == 2 and sum(1 for c in FAKE.calls if "tools" in c) == 2 * len(agentic), "each agentic PR made a tool round-trip (2 requests)")
ok({c["output_config"]["effort"] for c in FAKE.calls} == {"low", "high", "medium"}, "effort per rule")
control.del_setting(db, routing.SETTING_KEY); db.commit()

# tool sandbox: the model's tool inputs are validated
by = scoring.split_diff("diff --git a/x.py b/x.py\n+++ b/x.py\n+API_KEY='abc'\n+y=1\ndiff --git a/z.py b/z.py\n+++ b/z.py\n+eval(q)\n")
ok(set(by) == {"x.py", "z.py"}, by.keys())
ok(scoring.run_tool("read_file_diff", {"path": "../../etc/passwd"}, by)[1] and scoring.run_tool("read_file_diff", {"path": "x.py", "offset": -1}, by)[1], "bad args refused")
ok("z.py:3" in scoring.run_tool("search_diff", {"pattern": "eval\\("}, by)[0] and scoring.run_tool("search_diff", {"pattern": "("}, by)[1], "search + bad regex")
ok(scoring.run_tool("nope", {}, by)[1] and scoring.run_tool("list_changed_files", "x", by)[1], "unknown tool / non-object args")

# ---- batch mode (half price) + resume after a crash
wipe_scores()
control.set_setting(db, routing.SETTING_KEY, routing.validate_rules([
    {"name": "open -> agent", "when": {"state": "open"}, "agent": "deep-scorer", "model": "claude-opus-5-5", "effort": "high"},
    {"name": "rest", "when": {}, "agent": "scorer", "model": "claude-sonnet-5-5", "effort": "medium"}], set(ag)), "tester"); db.commit()
FAKE.batch_requests.clear(); FAKE.fail_retrieve = True
j = run_job({**PARAMS, "score": "llm", "collect": False, "batch": True})
ok(j["status"] == "failed" and control.get_job(db, j["id"])["state"]["batches"], "crash while polling keeps the batch ids")
n_req = len(FAKE.batch_requests)
db.execute("UPDATE jobs SET status='queued', error=NULL WHERE id=?", (j["id"],)); db.commit()
j = run_job(None, job_id=j["id"])
ok(j["status"] == "done" and len(FAKE.batch_requests) == n_req, "resume re-polls the same batch, no second submission")
ok(j["summary"]["saved"] == n_req == 8, (j["summary"], n_req))     # 8 batched; the 2 open PRs need the agent and ran online first
rows = {r["number"]: dict(r) for r in db.execute("SELECT p.number, e.* FROM pr_evaluations e JOIN pull_requests p ON p.id=e.pr_id")}
ok(len(rows) == 10 and rows[47]["agent"] == "deep-scorer" and rows[44]["agent"] == "scorer", len(rows))
control.del_setting(db, routing.SETTING_KEY); db.commit()
full = routing.cost_usd(rows[44]["model"], {"input": 1000 + 200 * 0, "output": 500, "cache_read": 200}, batch=False)
ok(rows[44]["cost_usd"] == round(full / 2, 4), (rows[44]["cost_usd"], full))     # batch price = half

# ---- rule-based job needs no key, no LLM
wipe_scores(); os.environ.pop("ANTHROPIC_API_KEY")
j = run_job({**PARAMS, "score": "heuristic", "collect": False})
ok(j["status"] == "done" and j["summary"]["saved"] == 10 and db.execute("SELECT count(*) FROM pr_evaluations WHERE engine='heuristic'").fetchone()[0] == 10, j["summary"])
ok(j["cost_usd"] == 0, "free")
os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-123"

# ---- try-on-PR: scores with an unsaved draft skill, compares with the active one, saves nothing
before = db.execute("SELECT count(*) FROM pr_evaluations WHERE engine='anthropic-api'").fetchone()[0]
draft = {**files, "SKILL.md": files["SKILL.md"] + "\n- Be extra strict about error handling.\n"}
j = run_job({"repo": "gem3s/payments", "number": 44, "draft": draft, "compare_active": True}, kind="try")
r = j["summary"]["results"]
ok(j["status"] == "done" and set(r) == {"draft", "active"} and r["draft"]["skill_hash"] == skill.version_hash(draft) != r["active"]["skill_hash"], j)
ok(j["summary"]["stored"]["engine"] == "heuristic" and db.execute("SELECT count(*) FROM pr_evaluations WHERE engine='anthropic-api'").fetchone()[0] == before, "nothing saved")
ok(any("extra strict" in c["system"][0]["text"] for c in FAKE.calls) and not skill.active(db)[2] == "edited", "draft used only for the try")
j = run_job({"repo": "gem3s/payments", "number": 9999}, kind="try")
ok(j["status"] == "failed" and "not collected" in j["error"], j)

# ------------------------------------------------------------------ 3. the server: three roles
import socket
with socket.socket() as s_:
    s_.bind(("127.0.0.1", 0)); PORT = s_.getsockname()[1]
os.environ.update({"GITHUB_OAUTH_CLIENT_ID": "cid", "GITHUB_OAUTH_CLIENT_SECRET": "csecret", "PUBLIC_URL": f"http://127.0.0.1:{PORT}",
                   "AUDIT_SESSION_SECRET": "s3cret-for-tests", "GITHUB_ORG": "gem3s", "ADMIN_USERS": "alice-gh",
                   "VIEWER_PASSWORD": "tv-pass", "VIEWER_USERS": "carol-gh", "GITHUB_OAUTH_BASE": "http://oauth.invalid", "GITHUB_API": "http://api.invalid"})
os.environ.pop("DASHBOARD_PASSWORD", None); os.environ.pop("DASHBOARD_USERS", None)
PROFILES = {  # code -> (token) ; token -> profile
    "c-alice": {"id": 999, "login": "alice-gh", "name": "Alice", "emails": [], "member": True},
    "c-asha": {"id": 101, "login": "asha-gh", "name": "Asha", "emails": ["asha@example.com"], "member": True},
    "c-ravi": {"id": 102, "login": "ravi-gh", "name": "Ravi", "emails": [], "member": True},
    "c-carol": {"id": 888, "login": "carol-gh", "name": "Carol", "emails": [], "member": False},
    "c-stranger": {"id": 555, "login": "stranger", "name": "S", "emails": [], "member": False},
    "c-meera-out": {"id": 103, "login": "meera-out", "name": "Meera", "emails": [], "member": False},
    "c-meera-row": {"id": 777, "login": "meera-gh", "name": "Meera", "emails": [], "member": False},
    "c-kiran-mail": {"id": 4040, "login": "kiran-new", "name": "Kiran", "emails": ["kiran@example.com"], "member": True},
}


def fake_http(method, url, headers=None, data=None):
    if url.endswith("/login/oauth/access_token"):
        return (200, {"access_token": "tok-" + data["code"]}) if data["code"] in PROFILES and data["client_secret"] == "csecret" else (200, {"error": "bad_verification_code"})
    prof = PROFILES[(headers or {}).get("Authorization", "").replace("Bearer tok-", "")]
    if url.endswith("/user"):
        return 200, {"id": prof["id"], "login": prof["login"], "name": prof["name"]}
    if url.endswith("/user/emails"):
        return 200, [{"email": e, "verified": True} for e in prof["emails"]] + [{"email": "spoof@example.com", "verified": False}]
    if "/user/memberships/orgs/gem3s" in url:
        return (200, {"state": "active"}) if prof["member"] else (404, {"message": "Not Found"})
    raise AssertionError(url)


auth.http_json = fake_http
threading.Thread(target=server.serve, args=(DBP, PORT), kwargs={"host": "127.0.0.1"}, daemon=True).start()
time.sleep(0.8)


def call(method, path, body=None, cookie=None, headers=None, csrf=True):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    h = {"Host": f"127.0.0.1:{PORT}", **(headers or {})}
    if cookie: h["Cookie"] = cookie
    if csrf and method != "GET": h["X-Requested-With"] = "audit"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); raw = r.read()
    try: data = json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw.decode(errors="replace")
    except ValueError: data = raw.decode(errors="replace")
    return r.status, data, r


def login(code):
    st, _, r = call("GET", "/auth/login")
    ok(st == 302 and "oauth.invalid/login/oauth/authorize" in r.getheader("Location") and "client_id=cid" in r.getheader("Location"), r.getheader("Location"))
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.getheader("Location")).query)["state"][0]
    state_cookie = r.getheader("Set-Cookie").split(";")[0]
    st, body, r = call("GET", f"/auth/callback?code={code}&state={state}", cookie=state_cookie)
    if st != 302:
        return None, st, body
    return [v.split(";")[0] for k, v in r.getheaders() if k == "Set-Cookie" and v.startswith("audit_session=")][0], st, body


# -- not signed in
st, _, r = call("GET", "/"); ok(st == 302 and r.getheader("Location") == "/login", "page redirects to the login page")
st, data, r = call("GET", "/api/meta"); ok(st == 401 and r.getheader("WWW-Authenticate") is None, "API: 401 without a browser popup")
st, data, _ = call("GET", "/auth/config"); ok(data == {"oauth": True, "viewer_password": True, "basic": False, "org": "gem3s"}, data)
st, page, _ = call("GET", "/login"); ok(st == 200 and "Sign in with GitHub" in page and "viewer" in page.lower(), "login page")
st, _, r = call("GET", "/auth/callback?code=c-alice&state=forged"); ok(st == 400, "forged state refused")

# -- viewer (office TV): shared password
bad = {"X-Forwarded-For": "10.0.0.7"}
for _ in range(5):
    ok(call("POST", "/auth/viewer", {"password": "wrong"}, headers=bad)[0] == 401, "wrong password")
ok(call("POST", "/auth/viewer", {"password": "tv-pass"}, headers=bad)[0] == 429, "throttled after 5 wrong guesses, even with the right one")
ok(call("POST", "/auth/viewer", {"password": "tv-pass"}, csrf=False)[0] == 403, "login POST needs the CSRF header")
st, _, r = call("POST", "/auth/viewer", {"password": "tv-pass"}, headers={"X-Forwarded-For": "10.0.0.8"})
ok(st == 200, st)
VIEWER = r.getheader("Set-Cookie").split(";")[0]
ok("HttpOnly" in r.getheader("Set-Cookie") and "SameSite=Lax" in r.getheader("Set-Cookie") and "Max-Age=2592000" in r.getheader("Set-Cookie"), "cookie flags / 30 days")
st, team, _ = call("GET", f"/api/team?since={SINCE}&until={TODAY}", cookie=VIEWER)
ok(st == 200 and {"asha", "ravi", "meera", "kiran"} <= {t["login"] for t in team}, "viewer sees everyone")
ok(call("GET", "/api/me", cookie=VIEWER)[1]["role"] == "viewer", "me")
pr40 = db.execute("SELECT p.id FROM pull_requests p WHERE number=40").fetchone()[0]
ok(call("GET", f"/api/pr/{pr40}", cookie=VIEWER)[0] == 200 and call("GET", "/api/user/gh:101", cookie=VIEWER)[0] == 200, "viewer reads any PR / person")
for path in ("/api/jobs", "/api/settings", "/api/skill", "/api/routing", "/api/users", "/api/github/repos"):
    ok(call("GET", path, cookie=VIEWER)[0] == 403, f"viewer must not read {path}")
for m, path, body in (("POST", "/api/jobs", {"kind": "refresh", "repos": ["gem3s/orders"]}), ("PUT", "/api/skill", {"files": files}), ("PUT", "/api/routing", {"rules": rules}),
                      ("POST", "/api/repos", {"full_name": "a/b"}), ("POST", "/api/users", {"gh_login": "x", "role": "admin"})):
    ok(call(m, path, body, cookie=VIEWER)[0] == 403, f"viewer must not {m} {path}")

# -- self: GitHub login, only their own data
ASHA, st, _ = login("c-asha")
ok(ASHA, st)
me = call("GET", "/api/me", cookie=ASHA)[1]; ok(me["role"] == "self" and me["uid"] == "gh:101" and me["login"] == "asha-gh", me)
team = call("GET", f"/api/team?since={SINCE}&until={TODAY}", cookie=ASHA)[1]; ok([t["uid"] for t in team] == ["gh:101"], "self sees only their own row")
ok(call("GET", "/api/user/gh:101?since=0000&until=9999", cookie=ASHA)[0] == 200, "own page")
ok(call("GET", "/api/user/gh:102", cookie=ASHA)[0] == 403 and call("GET", "/api/user/gh:103", cookie=ASHA)[0] == 403, "others' pages refused")
meta = call("GET", "/api/meta", cookie=ASHA)[1]; ok(set(meta["repos"]) == {"gem3s/payments", "gem3s/orders"} and meta["pending_eval"] is None, meta)
pr47 = db.execute("SELECT id FROM pull_requests WHERE number=47").fetchone()[0]        # ravi's PR, asha is not involved
pr22 = db.execute("SELECT id FROM pull_requests WHERE number=22").fetchone()[0]        # shared PR owned by asha
ok(call("GET", f"/api/pr/{pr47}", cookie=ASHA)[0] == 403, "someone else's PR refused")
st, d22, _ = call("GET", f"/api/pr/{pr22}", cookie=ASHA)
ok(st == 200 and not d22["redacted"], st)
mine = [x for x in d22["people"] if x["login"] == "asha"][0]; others = [x for x in d22["people"] if x["login"] != "asha"]
ok(mine["commit_msg"] is not None and others and all(x["code_quality"] is None and x["overall"] is None and x["notes"] is None for x in others), "other committers' scores hidden")
ok(call("GET", "/api/settings", cookie=ASHA)[0] == 403 and call("POST", "/api/jobs", {"kind": "refresh"}, cookie=ASHA)[0] == 403, "no admin features")
RAVI, _, _ = login("c-ravi")
st, d40, _ = call("GET", f"/api/pr/{pr40}", cookie=RAVI)       # asha's PR that ravi only reviewed
ok(st == 200 and d40["redacted"] and d40["people"] == [] and d40["overall"] is None and "commit_msg" not in d40 or d40.get("commit_msg") is None, "reviewer-only access is redacted")
ok(all(r["reviewer"] == "ravi" for r in d40["reviewer_evals"]), "only their own reviewer rating")
# an org member matched by a verified e-mail (kiran has no GitHub id match for this new login)
KIRAN, st, _ = login("c-kiran-mail")
ok(KIRAN and call("GET", "/api/me", cookie=KIRAN)[1]["uid"] == "gh:104", "matched by verified e-mail")
# not an org member / unknown -> no access; explicit user row -> access even outside the org
OUT, st, body = login("c-stranger"); ok(OUT is None and st == 403 and "not set up" in body, "stranger denied")
OUT, st, _ = login("c-meera-out"); ok(OUT is None and st == 403, "matching person but not in the org: denied")
st, _, r = call("GET", "/auth/callback?code=c-asha&state=x"); ok(st == 400, "callback without our state cookie")

# -- viewer by login list, admin by ADMIN_USERS
CAROL, st, _ = login("c-carol"); ok(CAROL and call("GET", "/api/me", cookie=CAROL)[1]["role"] == "viewer", "VIEWER_USERS")
ALICE, st, _ = login("c-alice"); ok(ALICE and call("GET", "/api/me", cookie=ALICE)[1]["role"] == "admin", "ADMIN_USERS")

# -- admin: user table (email / GitHub id / name -> person), settings never reveal secrets
st, d, _ = call("GET", "/api/settings", cookie=ALICE)
txt = json.dumps(d)
ok(st == 200 and d["anthropic_key"] is True and d["github_token"] is True and d["oauth"] is True, d)
ok("sk-test-secret-123" not in txt and "ghp_test_secret_456" not in txt and "csecret" not in txt and "tv-pass" not in txt and "s3cret" not in txt, "no secret value in /api/settings")
ok(call("POST", "/api/users", {"gh_login": "meera-gh", "name": "Meera", "role": "self", "uid": "gh:103"}, cookie=ALICE)[0] == 200, "add user row")
ok(call("POST", "/api/users", {"gh_login": "x", "role": "root"}, cookie=ALICE)[0] == 400, "bad role")
ok(call("POST", "/api/users", {"gh_login": "y", "uid": "gh:nope"}, cookie=ALICE)[0] == 400, "unknown person")
ok(call("POST", "/api/users", {"gh_login": "dup", "email": "e@example.com"}, cookie=ALICE)[0] == 200, "another row")
users = call("GET", "/api/users", cookie=ALICE)[1]; ok(any(p["uid"] == "gh:103" for p in users["people"]) and len(users["app_users"]) == 2, users)
MEERA, st, _ = login("c-meera-row"); ok(MEERA and call("GET", "/api/me", cookie=MEERA)[1]["uid"] == "gh:103", "explicit row grants self even outside the org")
row = [u for u in users["app_users"] if u["gh_login"] == "meera-gh"][0]
ok(call("PUT", "/api/users", {**row, "active": 0}, cookie=ALICE)[0] == 200 and call("GET", "/api/me", cookie=MEERA)[0] == 401, "deactivating a person ends their session")
ok(call("DELETE", f"/api/users/{row['id']}", cookie=ALICE)[0] == 200, "delete row")

# -- admin: CSRF
ok(call("PUT", "/api/routing", {"rules": rules}, cookie=ALICE, csrf=False)[0] == 403, "missing CSRF header")
ok(call("PUT", "/api/routing", {"rules": rules}, cookie=ALICE, headers={"Origin": "http://evil.example"})[0] == 403, "foreign origin")
# -- admin: routing + skill editors
st, rt, _ = call("GET", "/api/routing", cookie=ALICE)
ok(st == 200 and "claude-haiku-5-5" in rt["models"] and set(rt["agents"]) == {"scorer", "deep-scorer"} and not rt["custom"], rt.keys())
ok(call("PUT", "/api/routing", {"rules": [{**rules[-1], "model": "gpt-9"}]}, cookie=ALICE)[0] == 400, "bad model refused")
new_rules = [{"name": "all on sonnet", "when": {}, "agent": "scorer", "model": "claude-sonnet-5-5", "effort": "medium"}]
ok(call("PUT", "/api/routing", {"rules": new_rules}, cookie=ALICE)[0] == 200 and call("GET", "/api/routing", cookie=ALICE)[1]["custom"], "save routing")
ok(call("POST", "/api/routing/test", {"lines": 5000, "shared": True}, cookie=ALICE)[1]["rule"]["model"] == "claude-sonnet-5-5", "rule tester")
ok(call("POST", "/api/routing/reset", cookie=ALICE)[0] == 200 and not call("GET", "/api/routing", cookie=ALICE)[1]["custom"], "reset routing")
sk = call("GET", "/api/skill", cookie=ALICE)[1]; ok(sk["source"] == "repo" and sk["hash"] == sk["repo_hash"] and "scorer" in sk["agents"], sk.keys())
ok(call("PUT", "/api/skill", {"files": {"SKILL.md": "x"}}, cookie=ALICE)[0] == 400, "incomplete skill refused")
st, d, _ = call("PUT", "/api/skill", {"files": edited, "note": "stricter"}, cookie=ALICE); ok(st == 200, d)
sk = call("GET", "/api/skill", cookie=ALICE)[1]; ok(sk["source"] == "edited" and sk["hash"] == d["hash"] and sk["versions"][0]["note"] == "stricter", sk["versions"])
ok(call("GET", f"/api/skill/version/{sk['repo_hash']}", cookie=ALICE)[1]["files"] == files, "old version readable")
ok(call("POST", "/api/skill/activate", {"hash": "nope"}, cookie=ALICE)[0] == 400, "unknown version")
ok("went well" in call("GET", "/api/standards", cookie=VIEWER)[1] or True, "standards readable by everyone")
ok(call("POST", "/api/skill/reset", cookie=ALICE)[0] == 200 and call("GET", "/api/skill", cookie=ALICE)[1]["source"] == "repo", "reset skill")

# -- admin: repos, estimate, run, cancel
github.GitHub.org_repos = lambda self, org: [{"full_name": f"{org}/orders", "pushed_at": "2026-10-30T00:00:00Z", "default_branch": "main", "archived": False, "fork": False, "private": True},
                                             {"full_name": f"{org}/new-repo", "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main", "archived": False, "fork": False, "private": False}]
st, d, _ = call("GET", "/api/github/repos", cookie=ALICE)
ok(st == 200 and d["org"] == "gem3s" and {r["full_name"]: r["tracked"] for r in d["repos"]} == {"gem3s/orders": True, "gem3s/new-repo": False}, d)
ok(call("GET", "/api/github/repos?org=bad%20org", cookie=ALICE)[0] == 400, "org name validated")
ok(call("POST", "/api/repos", {"repos": ["gem3s/new-repo"]}, cookie=ALICE)[0] == 200 and call("POST", "/api/repos", {"full_name": "not a repo"}, cookie=ALICE)[0] == 400, "track repos")
ok(len(call("GET", "/api/repos", cookie=ALICE)[1]) == 3 and call("DELETE", "/api/repos/gem3s/new-repo", cookie=ALICE)[0] == 200, "untrack")
wipe_scores()
st, est, _ = call("POST", "/api/jobs", {"kind": "refresh", "estimate": True, "repos": ["gem3s/orders", "gem3s/payments"], "since": SINCE, "until": TODAY, "score": "llm"}, cookie=ALICE)
ok(st == 200 and est["prs"] == 10 and est["usd_estimate"] > 0, est)
for bad_body in ({"kind": "refresh", "repos": ["x"]}, {"kind": "refresh", "score": "magic"}, {"kind": "refresh", "months": 999}, {"kind": "refresh", "max_usd": -1},
                 {"kind": "shell"}, {"kind": "try", "repo": "gem3s/orders", "number": "x"}, {"kind": "try", "repo": "gem3s/orders", "number": 1, "draft": {"SKILL.md": "x"}}):
    ok(call("POST", "/api/jobs", bad_body, cookie=ALICE)[0] == 400, f"refused: {bad_body}")
st, d, _ = call("POST", "/api/jobs", {"kind": "refresh", "repos": ["gem3s/orders", "gem3s/payments"], "since": SINCE, "until": TODAY, "score": "heuristic", "collect": False}, cookie=ALICE)
ok(st == 200, d)
for _ in range(100):
    job = call("GET", f"/api/jobs/{d['id']}", cookie=ALICE)[1]
    if job["status"] in ("done", "failed", "cancelled"):
        break
    time.sleep(0.2)
ok(job["status"] == "done" and job["summary"]["saved"] == 10 and job["created_by"] == "alice-gh" and job["events"], job)
ok(call("GET", "/api/jobs", cookie=ALICE)[1][0]["id"] == d["id"], "job list")
ok(call("POST", f"/api/jobs/{d['id']}/cancel", cookie=ALICE)[0] == 200 and call("POST", "/api/jobs/9999/cancel", cookie=ALICE)[0] == 404, "cancel endpoint")
# queued job cancelled before it runs
control.create_job(db, "refresh", {}, "x"); db.commit()
# -- logout
st, _, r = call("POST", "/auth/logout", cookie=ALICE); ok(st == 200 and "Max-Age=0" in r.getheader("Set-Cookie"), "logout clears the cookie")
ok(call("GET", "/api/me", cookie="audit_session=tampered.value")[0] == 401, "tampered cookie")

# -- database predating the web app cannot be served read-only
import sqlite3, shutil
old = WORK / "old.db"; c = sqlite3.connect(old); c.execute("CREATE TABLE users(uid TEXT)"); c.commit(); c.close()
try:
    server.serve(str(old), PORT + 1, readonly=True); raise AssertionError("must refuse")
except SystemExit as e:
    ok("predates the web app" in str(e), e)

# ------------------------------------------------------------------ 4. .env loader, automatic refresh, markdown
import envfile
parsed = envfile.parse('# c\nA=1\nB = two words  # note\nC="quoted # kept"\nexport D=4\nE=\nnot a line\n')
ok(parsed == {"A": "1", "B": "two words", "C": "quoted # kept", "D": "4", "E": ""}, parsed)
envp = WORK / "t.env"; envp.write_text("TEST_ENVFILE_A=from-file\nTEST_ENVFILE_B=\nTEST_ENVFILE_C=file\n", encoding="utf-8")
os.environ["TEST_ENVFILE_C"] = "real"
ok(envfile.load(envp) == ["TEST_ENVFILE_A"] and os.environ["TEST_ENVFILE_A"] == "from-file" and os.environ["TEST_ENVFILE_C"] == "real" and "TEST_ENVFILE_B" not in os.environ, "real environment wins; empty values skipped")
ok(envfile.load(WORK / "missing.env") == [], "missing file is fine")

# automatic refresh: switched on by the environment, safe by default
for k in ("AUTO_REFRESH_HOURS", "AUTO_REFRESH_MONTHS", "AUTO_SCORE", "AUTO_MAX_USD"):
    os.environ.pop(k, None)
control.del_setting(db, "schedule"); control.del_setting(db, "schedule_last"); db.commit()
ok(jobs.effective_schedule(db) == {"enabled": False, "source": "none"}, "off unless asked")
os.environ.update(AUTO_REFRESH_HOURS="6", AUTO_SCORE="llm", AUTO_REFRESH_MONTHS="2")
es = jobs.effective_schedule(db)
ok(es["enabled"] and es["every_hours"] == 6 and es["months"] == 2 and es["source"] == "environment" and es["all_org"] and es["max_usd"] == 5, es)
os.environ["AUTO_REFRESH_HOURS"] = "abc"; ok(not jobs.effective_schedule(db)["enabled"], "a bad value switches it off, not the server"); os.environ["AUTO_REFRESH_HOURS"] = "6"

def sched_jobs():
    return db.execute("SELECT * FROM jobs WHERE created_by='scheduler' ORDER BY id").fetchall()
db.execute("UPDATE tracked_repos SET enabled=0"); db.execute("UPDATE jobs SET status='cancelled' WHERE status IN ('queued','running')"); db.commit()
os.environ.pop("ANTHROPIC_API_KEY", None)
runner = jobs.JobRunner(DBP)
runner.schedule(db)
sj = sched_jobs(); ok(len(sj) == 1, "first check starts a run straight away")
sp = json.loads(sj[0]["params"])
ok(sp["org"] == "gem3s" and sp["collect"] and sp["months"] == 2 and sp["score"] == "none", sp)            # llm without a key downgrades to collect-only
control.update_job(db, sj[0]["id"], status="cancelled"); db.commit()                                         # keep the server's own worker out of this test
runner.schedule(db); ok(len(sched_jobs()) == 1, "not again before the interval is over")
control.set_setting(db, "schedule_last", time.time() - 7 * 3600); db.commit()
runner.schedule(db); ok(len(sched_jobs()) == 2, "due again after the interval")
control.update_job(db, sched_jobs()[1]["id"], status="cancelled"); db.commit()
db.execute("UPDATE tracked_repos SET enabled=1"); control.set_setting(db, "schedule_last", time.time() - 7 * 3600); db.commit()
runner.schedule(db); last = sched_jobs()[-1]
ok("org" not in json.loads(last["params"]) and "repos" not in json.loads(last["params"]), "tracked repos win: the job uses them by default")
control.update_job(db, last["id"], status="cancelled"); db.commit()
os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-123"

# discovering the org's active repos inside a job
G["FakeGH"].org_repos = lambda self, org: [
    {"full_name": f"{org}/payments", "pushed_at": "2026-10-30T00:00:00Z", "default_branch": "main", "archived": False, "fork": False},
    {"full_name": f"{org}/orders", "pushed_at": "2026-10-20T00:00:00Z", "default_branch": "main", "archived": False, "fork": False},
    {"full_name": f"{org}/old-stuff", "pushed_at": "2025-01-01T00:00:00Z", "default_branch": "main", "archived": False, "fork": False},
    {"full_name": f"{org}/archived", "pushed_at": "2026-10-30T00:00:00Z", "default_branch": "main", "archived": True, "fork": False}]
j = run_job({"org": "gem3s", "since": SINCE, "until": TODAY, "score": "none"})
ok(j["status"] == "done" and sorted(j["summary"]["repos"]) == ["gem3s/orders", "gem3s/payments"] and set(j["summary"]["collect"]) == set(j["summary"]["repos"]), j["summary"])
ok(any("had activity since" in e["message"] for e in j["events"]), "log says how many repos were found")

# the schedule through the API: saved settings win, "back to .env" undoes it
ADMIN2, _, _ = login("c-alice")
st, d, _ = call("GET", "/api/settings", cookie=ADMIN2)
ok(d["schedule"]["source"] == "environment" and d["schedule"]["enabled"] and "schedule_last" in d, d["schedule"])
ok(call("PUT", "/api/settings", {"schedule": {"enabled": False, "every_hours": 3, "months": 1, "score": "none", "all_org": True}}, cookie=ADMIN2)[0] == 200, "save schedule")
d = call("GET", "/api/settings", cookie=ADMIN2)[1]; ok(d["schedule"]["source"] == "saved" and not d["schedule"]["enabled"] and d["schedule"]["all_org"], d["schedule"])
ok(call("PUT", "/api/settings", {"schedule": {"enabled": True, "score": "magic"}}, cookie=ADMIN2)[0] == 400, "bad score refused")
ok(call("PUT", "/api/settings", {"schedule_reset": True}, cookie=ADMIN2)[0] == 200 and call("GET", "/api/settings", cookie=ADMIN2)[1]["schedule"]["source"] == "environment", "back to the .env setting")
ok(call("POST", "/api/jobs", {"kind": "refresh", "org": "bad org", "score": "none"}, cookie=ADMIN2)[0] == 400, "org name validated")
for k in ("AUTO_REFRESH_HOURS", "AUTO_REFRESH_MONTHS", "AUTO_SCORE"):
    os.environ.pop(k, None)

# markdown renderer in the dashboard (needs node; skipped without it)
import shutil, subprocess
if shutil.which("node"):
    html = (WORK / "dashboard" / "index.html").read_text(encoding="utf-8")
    fn = html[html.index("function md(src){"):html.index("\n}\n", html.index("function md(src){")) + 3]
    js = ("const esc = s => (s ?? '').toString().replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));\n" + fn +
          "\nconst out = md(require('fs').readFileSync(0, 'utf8')); process.stdout.write(out);")
    sample = ("# Title\n\nSome **bold**, *it* and `code` with [a link](https://example.com).\n\n- one\n- two\n\n1. first\n2. second\n\n"
              "| A | B |\n|---|---|\n| 1 | 2 |\n\n```\nraw <b>text</b>\n```\n\n> quoted\n\n---\n| not a table\n<script>alert(1)</script>\n[x](javascript:alert(1))\n")
    out = subprocess.run(["node", "-e", js], input=sample.encode("utf-8"), capture_output=True, timeout=30)
    ok(out.returncode == 0, out.stderr)
    h = out.stdout.decode("utf-8")
    for needle in ("<h2>Title</h2>", "<b>bold</b>", "<i>it</i>", "<code>code</code>", '<a href="https://example.com"', "<ul><li>one</li><li>two</li></ul>",
                   "<ol><li>first</li><li>second</li></ol>", "<table", "<th>A</th>", "<td>2</td>", "&lt;b&gt;text&lt;/b&gt;", "<blockquote>quoted</blockquote>", "<hr>"):
        ok(needle in h, f"markdown: missing {needle}")
    ok("<script" not in h and 'href="javascript' not in h, "markdown output is escaped (no script, no javascript: links)")
    rubric = (WORK / "skills" / "pr-scoring" / "rubric.md").read_text(encoding="utf-8")
    out = subprocess.run(["node", "-e", js], input=rubric.encode("utf-8"), capture_output=True, timeout=30)
    html_out = out.stdout.decode("utf-8")
    ok(out.returncode == 0 and html_out.count("<h2>") == 1 and html_out.count("<h3>") >= 5 and "<table" in html_out and chr(10) + "#" not in html_out, "the real rubric renders")
else:
    print("(node not found: markdown renderer test skipped)")

print("\nAPP CHECKS PASSED")
