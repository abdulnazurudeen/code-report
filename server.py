"""The web app: dashboard pages, JSON API, login and the admin endpoints.

Roles (see auth.py): admin = everything, viewer = read everything, self = read only your own data.
Mutating requests need an admin and the CSRF header. The data API enforces the role on the server; the UI only hides things.
"""
import base64, datetime, hmac, http.server, json, os, re, sys, threading, time, urllib.parse, webbrowser
from pathlib import Path
import store, control, auth, skill, routing, scoring, jobs

HERE = Path(__file__).resolve().parent
DASH = HERE / "dashboard"
PAGES = {"/": "index.html", "/index.html": "index.html", "/admin.js": "admin.js", "/login": "login.html"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_BODY = 1_500_000
LOCAL_ADMIN = {"role": "admin", "login": "local", "name": "Local user", "uid": None, "source": "local"}
_repo_cache = {}


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


def clean_job_params(kind, b):
    """Whitelist and type-check job parameters from the browser."""
    if kind == "refresh":
        out = {}
        if b.get("repos") is not None:
            repos = b["repos"]
            if not isinstance(repos, list) or len(repos) > 200 or not all(isinstance(r, str) and control.REPO_RE.match(r.strip()) for r in repos):
                raise ValueError("repos must be a list of owner/name (at most 200)")
            out["repos"] = [r.strip() for r in repos]
        for k in ("since", "until"):
            if b.get(k):
                try:
                    out[k] = datetime.date.fromisoformat(str(b[k])).isoformat()
                except ValueError:
                    raise ValueError(f"{k} must be YYYY-MM-DD")
        if b.get("org"):
            if not re.match(r"^[A-Za-z0-9-]{1,39}$", str(b["org"])):
                raise ValueError("org is not a valid GitHub organisation name")
            out["org"] = b["org"]                      # with no repos: every repo of the org with recent activity
        out["months"] = _int(b.get("months", 3), "months", 1, 36)
        out["score"] = b.get("score", "llm")
        if out["score"] not in ("llm", "heuristic", "none"):
            raise ValueError("score must be llm, heuristic or none")
        for k in ("collect", "replace_heuristic", "rescore_old_skill", "batch"):
            if k in b:
                out[k] = bool(b[k])
        if b.get("max_prs"):
            out["max_prs"] = _int(b["max_prs"], "max_prs", 1, 5000)
        if b.get("max_usd"):
            try:
                out["max_usd"] = float(b["max_usd"])
            except (TypeError, ValueError):
                raise ValueError("max_usd must be a number")
            if not 0 < out["max_usd"] <= 100000:
                raise ValueError("max_usd must be between 0 and 100000")
        return out
    if kind == "try":
        if not control.REPO_RE.match(str(b.get("repo") or "")):
            raise ValueError("repo must be owner/name")
        out = {"repo": b["repo"], "number": _int(b.get("number"), "number", 1, 10**7), "compare_active": bool(b.get("compare_active", True))}
        r = b.get("route")
        if r:
            cat = routing.catalog()
            if r.get("model") not in cat["models"] or r.get("effort") not in cat["efforts"] or not isinstance(r.get("agent"), str):
                raise ValueError("route needs a known model, effort and an agent name")
            out["route"] = {k: r[k] for k in ("model", "effort", "agent")}
        if b.get("draft"):
            skill.validate(b["draft"])
            out["draft"] = b["draft"]
        return out
    raise ValueError("kind must be refresh or try")


def _int(v, name, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def self_repos(db, uid):
    return [r[0] for r in db.execute(
        "SELECT r.full_name FROM repos r WHERE EXISTS (SELECT 1 FROM pull_requests p WHERE p.repo_id=r.id AND (p.author_uid=:u "
        "OR EXISTS (SELECT 1 FROM pr_contributions pc WHERE pc.pr_id=p.id AND pc.uid=:u) "
        "OR EXISTS (SELECT 1 FROM reviews rv WHERE rv.pr_id=p.id AND rv.reviewer_uid=:u))) "
        "OR EXISTS (SELECT 1 FROM commits c WHERE c.repo_id=r.id AND c.author_uid=:u) ORDER BY r.full_name", {"u": uid})]


def serve(db_path, port, open_browser=False, host="127.0.0.1", readonly=False, public=False):
    cfg = auth.Config()
    local = is_loopback(host)
    if not cfg.any_login and not local and not public:
        raise SystemExit("Refusing to serve on a network address without a login: the dashboard shows people's scores and "
                         "security findings. Configure a login (GitHub OAuth: docs/github-oauth-setup.md, VIEWER_PASSWORD, or "
                         "DASHBOARD_PASSWORD / DASHBOARD_USERS), or pass --public.")
    if not Path(db_path).exists():
        raise SystemExit(f"Database not found: {db_path}")
    if not readonly:
        store.connect(db_path).close()                       # create / migrate the schema once; requests skip it
    else:
        probe = store.connect(db_path, readonly=True)
        try:
            if not probe.execute("SELECT 1 FROM sqlite_master WHERE name='app_users'").fetchone():
                raise SystemExit("This database predates the web app (no app_users table) and --readonly cannot upgrade it. "
                                 "Start the server once without --readonly (or run any audit.py command) to migrate it, then restart.")
        finally:
            probe.close()
    runner = None if readonly else jobs.JobRunner(db_path)
    if runner:
        runner.start()
    for p in cfg.problems():
        print(f"WARNING: {p}", file=sys.stderr, flush=True)

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "pr-audit"
        sys_version = ""

        def log_message(self, fmt, *a):
            if not local:                                    # keep an access log when hosted
                sys.stderr.write(f"{self.address_string()} {fmt % a}\n")

        # ---------------------------------------------------------------- plumbing
        def send(self, code, body, ctype="application/json", headers=()):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, where, headers=()):
            self.send(302, b"", "text/plain", [("Location", where), *headers])

        def body(self):
            try:
                return json.loads(self._raw or b"{}")
            except ValueError:
                raise HttpError(400, "body is not JSON")

        def db(self, write=False):
            return store.connect(db_path, readonly=readonly, migrate=False)

        def ip(self):
            return self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]

        def principal(self):
            h = self.headers.get("Authorization", "")
            if cfg.basic and h.startswith("Basic "):
                try:
                    name, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except ValueError:
                    return None
                if name in cfg.basic and hmac.compare_digest(cfg.basic[name].encode(), pw.encode()):
                    return {"role": "admin", "login": name, "name": name, "uid": None, "source": "basic"}
                return None
            tok = auth.read_cookie(self.headers.get("Cookie"), auth.SESSION_COOKIE)
            p = auth.verify(tok, cfg.key()) if tok else None
            if p and p.get("role") in control.ROLES:
                if p.get("src") == "github":                 # an admin may switch someone off without waiting for the cookie to expire
                    db = self.db()
                    try:
                        row = control.find_app_user(db, None, p.get("login"))
                    finally:
                        db.close()
                    if row and not row["active"]:
                        return None
                return {"role": p["role"], "login": p.get("login"), "name": p.get("name"), "uid": p.get("uid"), "source": p.get("src", "session")}
            if not cfg.any_login and local:
                return LOCAL_ADMIN
            return None

        # ---------------------------------------------------------------- dispatch
        def do_GET(self): self.dispatch("GET")
        def do_POST(self): self.dispatch("POST")
        def do_PUT(self): self.dispatch("PUT")
        def do_DELETE(self): self.dispatch("DELETE")

        def dispatch(self, method):
            # Always read the request body before answering: replying (e.g. 403) with an unread body can make the
            # client's connection reset before it sees the response.
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True
                return self.send(413, {"error": "body too large"})
            self._raw = self.rfile.read(n) if n else b""
            u = urllib.parse.urlparse(self.path)
            path, q = u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if method == "GET" and path == "/healthz":
                    return self.send(200, {"ok": True})
                if path.startswith("/auth/"):
                    return self.auth_route(method, path, q)
                if method == "GET" and path == "/login":
                    return self.send(200, (DASH / "login.html").read_bytes(), "text/html; charset=utf-8")
                me = self.principal()
                if me is None:
                    if method == "GET" and path in ("/", "/index.html") and cfg.login_page:
                        return self.redirect("/login")
                    return self.send(401, {"error": "login required"},
                                     headers=[] if cfg.login_page else [("WWW-Authenticate", 'Basic realm="PR audit"')])
                if method != "GET":
                    if readonly and path != "/auth/logout":
                        raise HttpError(403, "this server is read-only")
                    if not auth.csrf_ok(self.headers, self.headers.get("Host", "")):
                        raise HttpError(403, "missing or foreign request origin")
                    if me["role"] != "admin":
                        raise HttpError(403, "admin only")
                if method == "GET" and path in PAGES:
                    return self.send(200, (DASH / PAGES[path]).read_bytes(),
                                     "text/html; charset=utf-8" if PAGES[path].endswith(".html") else "text/javascript; charset=utf-8")
                if not path.startswith("/api/"):
                    return self.send(404, {"error": "not found"})
                return self.api(method, path[5:], q, me)
            except HttpError as e:
                self.send(e.code, {"error": e.msg})
            except PermissionError as e:
                self.send(403, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self.send(400, {"error": str(e) or "bad request"})
            except Exception as e:                            # never leak a traceback to the browser
                import traceback; traceback.print_exc()
                self.send(500, {"error": f"server error: {type(e).__name__}"})

        # ---------------------------------------------------------------- login
        def auth_route(self, method, path, q):
            secure = cfg.secure_cookies
            if method == "GET" and path == "/auth/config":
                return self.send(200, {"oauth": cfg.oauth, "viewer_password": bool(cfg.viewer_password), "basic": bool(cfg.basic), "org": cfg.org or None})
            if method == "GET" and path == "/auth/login":
                if not cfg.oauth:
                    raise HttpError(404, "GitHub login is not configured")
                state = auth.new_state()
                return self.redirect(auth.authorize_url(cfg, state),
                                     [("Set-Cookie", auth.cookie_header(auth.STATE_COOKIE, auth.sign({"s": state}, cfg.key(), 600), 600, secure))])
            if method == "GET" and path == "/auth/callback":
                st = auth.verify(auth.read_cookie(self.headers.get("Cookie"), auth.STATE_COOKIE) or "", cfg.key())
                clear = ("Set-Cookie", auth.cookie_header(auth.STATE_COOKIE, "", 0, secure))
                if not cfg.oauth or not st or not q.get("state") or not hmac.compare_digest(st["s"].encode(), q["state"].encode()) or not q.get("code"):
                    return self.send(400, self.page("Sign-in failed", "The sign-in link expired or was not started here. Go back and try again."), "text/html; charset=utf-8", [clear])
                try:
                    profile = auth.fetch_profile(cfg, q["code"])
                except PermissionError as e:
                    return self.send(403, self.page("Sign-in failed", str(e)), "text/html; charset=utf-8", [clear])
                db = store.connect(db_path, readonly=readonly, migrate=False)
                try:
                    me = auth.principal_for_profile(db, cfg, profile)
                    if not readonly:
                        db.commit()
                finally:
                    db.close()
                if me is None:
                    return self.send(403, self.page("No access", f"@{profile['login']} is not set up for this dashboard. Ask an admin to add "
                                                    "your GitHub login or email, or to make you a viewer."), "text/html; charset=utf-8", [clear])
                token = auth.sign({**{k: me[k] for k in ("role", "login", "name", "uid")}, "src": "github"}, cfg.key(), auth.HUMAN_TTL)
                return self.redirect("/", [clear, ("Set-Cookie", auth.cookie_header(auth.SESSION_COOKIE, token, auth.HUMAN_TTL, secure))])
            if method == "POST" and path == "/auth/viewer":
                if not auth.csrf_ok(self.headers, self.headers.get("Host", "")):
                    raise HttpError(403, "missing or foreign request origin")
                ip = self.ip()
                if auth.throttled(ip):
                    raise HttpError(429, "too many wrong passwords; try again in ten minutes")
                if not auth.check_viewer_password(cfg, self.body().get("password"), ip):
                    raise HttpError(401, "wrong password")
                token = auth.sign({"role": "viewer", "login": "viewer", "name": "Viewer", "uid": None, "src": "viewer"}, cfg.key(), auth.VIEWER_TTL)
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", auth.cookie_header(auth.SESSION_COOKIE, token, auth.VIEWER_TTL, secure))])
            if method == "POST" and path == "/auth/logout":
                if not auth.csrf_ok(self.headers, self.headers.get("Host", "")):
                    raise HttpError(403, "missing or foreign request origin")
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", auth.cookie_header(auth.SESSION_COOKIE, "", 0, secure))])
            raise HttpError(404, "not found")

        @staticmethod
        def page(title, msg):
            esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<title>{esc(title)}</title><body style='font:16px system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
                    f"<h1>{esc(title)}</h1><p>{esc(msg)}</p><p><a href='/login'>Back to sign in</a></p>").encode()

        # ---------------------------------------------------------------- API
        def api(self, method, path, q, me):
            parts = [urllib.parse.unquote(x) for x in path.split("/")]
            db = self.db()
            try:
                if method == "GET":
                    return self.api_get(db, parts, q, me)
                if me["role"] != "admin":
                    raise HttpError(403, "admin only")
                return self.api_write(db, method, parts, me)
            finally:
                db.close()

        def api_get(self, db, parts, q, me):
            role, uid = me["role"], me.get("uid")
            since, until, repo = q.get("since", "0000-00-00"), q.get("until", "9999-12-31"), q.get("repo") or None
            first = parts[0]
            if first == "me":
                return self.send(200, {**{k: me.get(k) for k in ("role", "login", "name", "uid", "source")}, "readonly": readonly,
                                       "org": cfg.org or None, "can_run": role == "admin" and not readonly})
            if first == "meta":
                m = store.meta(db)
                if role == "self":
                    m.update({"repos": self_repos(db, uid), "engines": {}, "pending_eval": None, "last_run": None})
                return self.send(200, m)
            if first == "team":
                t = store.team(db, since, until, repo)
                return self.send(200, [x for x in t if x["uid"] == uid] if role == "self" else t)
            if first == "user" and len(parts) == 2:
                if role == "self" and parts[1] != uid:
                    raise HttpError(403, "you can only see your own page")
                d = store.user_detail(db, parts[1], since, until, repo)
                return self.send(200 if d else 404, d or {"error": "not found"})
            if first == "pr" and len(parts) == 2:
                pr_id = int(parts[1])
                d = store.pr_detail(db, pr_id)
                if not d:
                    raise HttpError(404, "not found")
                if role == "self":
                    access = store.pr_access(db, pr_id, uid)
                    if access is None:
                        raise HttpError(403, "not one of your pull requests")
                    d = store.redact_pr_for_self(db, d, uid, access)
                return self.send(200, d)
            if first == "standards":
                files, h, _ = skill.active(db) if not readonly else (skill.repo_files(), None, None)
                return self.send(200, files["rubric.md"].encode(), "text/plain; charset=utf-8")
            if role != "admin":
                raise HttpError(403, "admin only")
            return self.api_admin_get(db, parts, q)

        def api_admin_get(self, db, parts, q):
            first = parts[0]
            if first == "settings":
                import github
                files, h, source = skill.active(db)
                return self.send(200, {
                    "github_token": github.token_present(), "anthropic_key": scoring.api_key_present(), "sdk_installed": scoring.sdk_installed(),
                    "org": cfg.org or None, "oauth": cfg.oauth, "viewer_password": bool(cfg.viewer_password), "public_url": cfg.public_url or None,
                    "session_secret": bool(cfg.secret), "problems": cfg.problems(), "readonly": readonly,
                    "skill_hash": h, "skill_source": source, "schedule": jobs.effective_schedule(db), "schedule_last": control.get_setting(db, "schedule_last"),
                    "concurrency": int(os.environ.get("AUDIT_CONCURRENCY", 3))})
            if first == "github" and parts[1:] == ["repos"]:
                return self.send(200, self.github_repos(db, q))
            if first == "repos":
                counts = {r[0]: r[1] for r in db.execute("SELECT r.full_name, count(p.id) FROM repos r LEFT JOIN pull_requests p ON p.repo_id=r.id GROUP BY r.id")}
                return self.send(200, [{**t, "prs": counts.get(t["full_name"], 0)} for t in control.list_tracked(db)])
            if first == "jobs":
                if len(parts) == 1:
                    return self.send(200, control.list_jobs(db))
                j = control.get_job(db, int(parts[1]))
                return self.send(200 if j else 404, j or {"error": "not found"})
            if first == "skill":
                if len(parts) == 3 and parts[1] == "version":
                    f = skill.version_files(db, parts[2])
                    return self.send(200 if f else 404, {"files": f} if f else {"error": "not found"})
                files, h, source = skill.active(db)
                ag = skill.agents(files)
                return self.send(200, {"files": files, "hash": h, "source": source, "repo_hash": skill.version_hash(skill.repo_files()),
                                       "versions": skill.versions(db),
                                       "agents": {n: {k: v for k, v in a.items() if k != "prompt"} for n, a in ag.items()}})
            if first == "routing":
                files, _, _ = skill.active(db)
                cat = routing.catalog()
                return self.send(200, {"rules": routing.load_rules(db), "default_rules": routing.default_rules(),
                                       "custom": db.execute("SELECT 1 FROM settings WHERE key=?", (routing.SETTING_KEY,)).fetchone() is not None,
                                       "models": cat["models"], "efforts": cat["efforts"], "agents": sorted(skill.agents(files))})
            if first == "users":
                return self.send(200, {"app_users": control.list_app_users(db),
                                       "people": [dict(r) for r in db.execute("SELECT uid, login, name FROM users WHERE is_bot=0 ORDER BY COALESCE(login,name)")]})
            raise HttpError(404, "not found")

        def github_repos(self, db, q):
            import github
            org = (q.get("org") or cfg.org or "").strip()
            if not re.match(r"^[A-Za-z0-9-]{1,39}$", org):
                raise ValueError("set GITHUB_ORG on the server, or give ?org=<organisation>")
            if not github.token_present():
                raise ValueError("GITHUB_TOKEN is not set on the server")
            hit = _repo_cache.get(org)
            if not hit or time.time() - hit[0] > 60 or q.get("refresh"):
                hit = (time.time(), github.GitHub(github.load_token()).org_repos(org))
                _repo_cache[org] = hit
            tracked = {t["full_name"] for t in control.list_tracked(db)}
            collected = {r[0]: r[1] for r in db.execute("SELECT r.full_name, count(p.id) FROM repos r LEFT JOIN pull_requests p ON p.repo_id=r.id GROUP BY r.id")}
            return {"org": org, "repos": [{**r, "tracked": r["full_name"] in tracked, "collected_prs": collected.get(r["full_name"])} for r in hit[1]]}

        def api_write(self, db, method, parts, me):
            first, who = parts[0], me.get("login")
            b = self.body() if method in ("POST", "PUT") else {}
            if first == "settings" and method == "PUT":
                if b.get("schedule_reset"):                  # go back to what the .env says
                    control.del_setting(db, "schedule")
                elif "schedule" in b:
                    s = b["schedule"] or {}
                    sc = {"enabled": bool(s.get("enabled")), "every_hours": _int(s.get("every_hours", 24), "every_hours", 1, 720),
                          "months": _int(s.get("months", 1), "months", 1, 36), "score": s.get("score", "none"),
                          "all_org": bool(s.get("all_org"))}
                    if sc["score"] not in ("llm", "heuristic", "none"):
                        raise ValueError("score must be llm, heuristic or none")
                    if s.get("max_usd"):
                        sc["max_usd"] = float(s["max_usd"])
                    control.set_setting(db, "schedule", sc, who)
                db.commit()
                return self.send(200, {"ok": True})
            if first == "repos":
                if method == "POST":
                    for name in b.get("repos") or [b.get("full_name")]:
                        control.add_tracked(db, name, who, b.get("enabled", True))
                    db.commit()
                    return self.send(200, {"ok": True})
                if method == "DELETE" and len(parts) == 3:
                    control.remove_tracked(db, f"{parts[1]}/{parts[2]}")
                    db.commit()
                    return self.send(200, {"ok": True})
            if first == "jobs":
                if method == "POST" and len(parts) == 1:
                    kind = b.get("kind", "refresh")
                    params = clean_job_params(kind, b)
                    if b.get("estimate"):
                        if kind != "refresh":
                            raise ValueError("estimate is for refresh jobs")
                        return self.send(200, jobs.estimate(db, params))
                    if db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0] >= 20:
                        raise ValueError("too many jobs are already waiting")
                    jid = control.create_job(db, kind, params, who)
                    db.commit()
                    runner.kick()
                    return self.send(200, {"id": jid})
                if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                    jid = int(parts[1])
                    j = db.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
                    if not j:
                        raise HttpError(404, "not found")
                    if j[0] == "queued":
                        control.update_job(db, jid, status="cancelled", finished_at=store.now())
                    elif j[0] == "running":
                        control.update_job(db, jid, cancel_requested=1)
                    db.commit()
                    return self.send(200, {"ok": True})
            if first == "skill":
                if method == "PUT":
                    h = skill.save_version(db, b.get("files"), who, (b.get("note") or "")[:200] or None)
                    db.commit()
                    return self.send(200, {"hash": h})
                if method == "POST" and parts[1:] == ["activate"]:
                    skill.activate(db, str(b.get("hash")), who)
                    db.commit()
                    return self.send(200, {"ok": True})
                if method == "POST" and parts[1:] == ["reset"]:
                    skill.reset(db)
                    db.commit()
                    return self.send(200, {"ok": True})
            if first == "routing":
                files, _, _ = skill.active(db)
                if method == "PUT":
                    rules = routing.validate_rules(b.get("rules"), set(skill.agents(files)))
                    control.set_setting(db, routing.SETTING_KEY, rules, who)
                    db.commit()
                    return self.send(200, {"rules": rules})
                if method == "POST" and parts[1:] == ["reset"]:
                    control.del_setting(db, routing.SETTING_KEY)
                    db.commit()
                    return self.send(200, {"ok": True})
                if method == "POST" and parts[1:] == ["test"]:       # which rule would a PR like this hit?
                    f = {"lines": int(b.get("lines", 0)), "files": int(b.get("files", 0)), "shared": bool(b.get("shared")),
                         "truncated": bool(b.get("truncated")), "repo": str(b.get("repo", "")), "paths": [str(x) for x in b.get("paths", [])][:200],
                         "state": b.get("state"), "prior_engine": b.get("prior_engine", "none")}
                    i, r = routing.choose(routing.load_rules(db), f)
                    return self.send(200, {"index": i, "rule": r})
            if first == "users":
                if method in ("POST", "PUT"):
                    uid = control.upsert_app_user(db, b, who)
                    db.commit()
                    return self.send(200, {"id": uid})
                if method == "DELETE" and len(parts) == 2:
                    control.delete_app_user(db, parts[1])
                    db.commit()
                    return self.send(200, {"ok": True})
            raise HttpError(404, "not found")

    srv = http.server.ThreadingHTTPServer((host, port), H)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    how = ("GitHub login" if cfg.oauth else "") + (" + " if cfg.oauth and (cfg.viewer_password or cfg.basic) else "") + \
          ("viewer password" if cfg.viewer_password else "") + (" + " if cfg.viewer_password and cfg.basic else "") + ("basic login" if cfg.basic else "")
    print(f"Dashboard running at {url}  (listening on {host}; login: {how or 'off - local use only, everyone is admin'}"
          f"{'; read-only' if readonly else ''})  - Ctrl+C to stop", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    finally:
        if runner:
            runner.stop()
