"""Login, sessions and roles for the hosted dashboard.

Roles
  admin   everything: run jobs, manage repos / users / models, edit the skill
  viewer  read-only, every project and person (office TV: sign in with the shared VIEWER_PASSWORD)
  self    read-only, only the signed-in person's own data (matched to the collected people by GitHub id,
          verified email or an app_users row)

Sign-in methods (configure any):
  GitHub OAuth  GITHUB_OAUTH_CLIENT_ID / GITHUB_OAUTH_CLIENT_SECRET / PUBLIC_URL      (see docs/github-oauth-setup.md)
  Viewer password  VIEWER_PASSWORD
  HTTP Basic (local / scripts)  DASHBOARD_USERS / DASHBOARD_PASSWORD -> admin
Who gets which role: ADMIN_USERS / VIEWER_USERS (GitHub logins, comma separated), the app_users table, and
GITHUB_ORG membership (an org member who is also a collected person gets "self").
With nothing configured the dashboard only listens on localhost and everyone there is admin.
"""
import base64, hashlib, hmac, json, os, secrets, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
import control

SESSION_COOKIE = "audit_session"
STATE_COOKIE = "audit_oauth"
HUMAN_TTL = 12 * 3600
VIEWER_TTL = 30 * 86400            # a TV should stay signed in
SCOPE = "read:user user:email read:org"
_EPHEMERAL = secrets.token_bytes(32)


def _list(name):
    return {x.strip().lower().lstrip("@") for x in (os.environ.get(name) or "").split(",") if x.strip()}


class Config:
    """Read from the environment each time, so changes (and tests) take effect without a restart of the module."""
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.oauth_id = e.get("GITHUB_OAUTH_CLIENT_ID", "")
        self.oauth_secret = e.get("GITHUB_OAUTH_CLIENT_SECRET", "")
        self.public_url = (e.get("PUBLIC_URL") or "").rstrip("/")
        self.org = e.get("GITHUB_ORG", "").strip()
        self.admins, self.viewers = _list("ADMIN_USERS"), _list("VIEWER_USERS")
        self.viewer_password = e.get("VIEWER_PASSWORD", "")
        self.secret = (e.get("AUDIT_SESSION_SECRET") or "").encode() or None
        self.oauth_base = e.get("GITHUB_OAUTH_BASE", "https://github.com").rstrip("/")
        self.api_base = e.get("GITHUB_API", "https://api.github.com").rstrip("/")
        self.basic = {}
        for pair in filter(None, (e.get("DASHBOARD_USERS") or "").split(",")):
            name, _, pw = pair.strip().partition(":")
            if name and pw:
                self.basic[name] = pw
        if e.get("DASHBOARD_PASSWORD"):
            self.basic.setdefault(e.get("DASHBOARD_USER", "admin"), e["DASHBOARD_PASSWORD"])

    @property
    def oauth(self):
        return bool(self.oauth_id and self.oauth_secret and self.public_url)

    @property
    def login_page(self):          # sessions are used (and the login page shown) when OAuth or the viewer password is set
        return self.oauth or bool(self.viewer_password)

    @property
    def any_login(self):
        return self.login_page or bool(self.basic)

    @property
    def secure_cookies(self):
        return self.public_url.startswith("https://")

    def key(self):
        return self.secret or _EPHEMERAL      # without AUDIT_SESSION_SECRET, sessions end whenever the server restarts

    def problems(self):
        out = []
        if self.oauth_id and not (self.oauth_secret and self.public_url):
            out.append("GitHub OAuth needs GITHUB_OAUTH_CLIENT_SECRET and PUBLIC_URL as well")
        if self.login_page and not self.secret:
            out.append("AUDIT_SESSION_SECRET is not set: logins will be lost whenever the server restarts")
        if self.public_url and not self.public_url.startswith("https://") and "localhost" not in self.public_url and "127.0.0.1" not in self.public_url:
            out.append("PUBLIC_URL is not https: cookies and tokens would travel unencrypted")
        return out


# ------------------------------------------------------------------ signed session cookie
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload, key, ttl):
    body = _b64(json.dumps({**payload, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def verify(token, key):
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(_unb64(sig), hmac.new(key, body.encode(), hashlib.sha256).digest()):
            return None
        p = json.loads(_unb64(body))
        return p if p.get("exp", 0) > time.time() else None
    except (ValueError, TypeError, KeyError):
        return None


def cookie_header(name, value, ttl, secure):
    return f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ttl}" + ("; Secure" if secure else "")


def read_cookie(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# ------------------------------------------------------------------ CSRF
def csrf_ok(headers, host):
    """State-changing requests must carry a custom header (a cross-site form or image can't set one) and, when the
    browser sends an Origin, it must be this site."""
    if headers.get("X-Requested-With") != "audit":
        return False
    origin = headers.get("Origin")
    return not origin or urllib.parse.urlparse(origin).netloc == host


# ------------------------------------------------------------------ viewer password: throttled
_fails, _lock = {}, threading.Lock()
MAX_FAILS, FAIL_WINDOW = 5, 600


def throttled(ip):
    with _lock:
        fresh = [t for t in _fails.get(ip, []) if t > time.time() - FAIL_WINDOW]
        _fails[ip] = fresh
        return len(fresh) >= MAX_FAILS


def record_fail(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def check_viewer_password(cfg, given, ip):
    """True if right. Wrong guesses are counted per address; too many in a row are refused for ten minutes."""
    if not cfg.viewer_password or throttled(ip):
        return False
    if hmac.compare_digest(cfg.viewer_password.encode(), (given or "").encode()):
        return True
    record_fail(ip)
    return False


# ------------------------------------------------------------------ GitHub OAuth
def http_json(method, url, headers=None, data=None):
    """Tiny JSON HTTP helper (tests replace this function)."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", "User-Agent": "pr-audit",
                                                                         **({"Content-Type": "application/json"} if body else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def new_state():
    return secrets.token_urlsafe(24)


def authorize_url(cfg, state):
    q = urllib.parse.urlencode({"client_id": cfg.oauth_id, "redirect_uri": cfg.public_url + "/auth/callback",
                                "scope": SCOPE, "state": state, "allow_signup": "false"})
    return f"{cfg.oauth_base}/login/oauth/authorize?{q}"


def fetch_profile(cfg, code):
    """Exchange the OAuth code and read who this is. The user's token is used for these calls only and then dropped.
    -> {id, login, name, emails (verified, lower case), org_member (True/False, None when GITHUB_ORG is not set)}"""
    st, tok = http_json("POST", f"{cfg.oauth_base}/login/oauth/access_token",
                        data={"client_id": cfg.oauth_id, "client_secret": cfg.oauth_secret, "code": code,
                              "redirect_uri": cfg.public_url + "/auth/callback"})
    token = (tok or {}).get("access_token")
    if st != 200 or not token:
        raise PermissionError("GitHub did not accept the sign-in code")
    h = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28", "Accept": "application/vnd.github+json"}
    st, user = http_json("GET", f"{cfg.api_base}/user", h)
    if st != 200 or not user or not user.get("id"):
        raise PermissionError("could not read the GitHub profile")
    st, emails = http_json("GET", f"{cfg.api_base}/user/emails", h)
    verified = [e["email"].lower() for e in (emails or []) if isinstance(e, dict) and e.get("verified")] if st == 200 else []
    member = None
    if cfg.org:
        st, m = http_json("GET", f"{cfg.api_base}/user/memberships/orgs/{urllib.parse.quote(cfg.org)}", h)
        member = st == 200 and (m or {}).get("state") == "active"
    return {"id": user["id"], "login": user["login"], "name": user.get("name"), "emails": verified, "org_member": member}


# ------------------------------------------------------------------ who may do what
def principal_for_profile(db, cfg, p):
    """Role for a signed-in GitHub profile, or None (access denied). The caller commits `db`."""
    login = p["login"].lower()
    row = control.find_app_user(db, p["id"], p["login"], p["emails"])
    if row and not row["active"]:
        return None
    role = "admin" if login in cfg.admins else row["role"] if row else "viewer" if login in cfg.viewers else None
    uid = row["uid"] if row and row.get("uid") else None
    if uid is None and (row or p["org_member"] is not False):
        # an explicit row, or an org member (or no org configured): the collected person this GitHub profile obviously is
        uid = control.implicit_uid(db, p["id"], p["login"], p["emails"])
    if role is None and uid:
        role = "self"
    if role == "self" and not uid:
        return None
    if role is None:
        return None
    if row:
        try:
            control.remember_profile(db, row["id"], p["id"], p["login"])
        except sqlite3.OperationalError:          # read-only server: matching still works, it just isn't remembered
            pass
    return {"role": role, "login": p["login"], "name": p.get("name") or p["login"], "uid": uid, "source": "github"}
