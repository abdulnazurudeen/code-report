"""Control-plane data for the web app: settings, tracked repos, app users (who may log in, as what), jobs.
Plain functions over a sqlite3 connection; the caller commits."""
import json, re
from store import now

ROLES = ("admin", "viewer", "self")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------ settings (non-secret JSON values)
def get_setting(db, key, default=None):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and r[0] is not None else default


def set_setting(db, key, value, who=None):
    db.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               (key, json.dumps(value), now(), who))


def del_setting(db, key):
    db.execute("DELETE FROM settings WHERE key=?", (key,))


# ------------------------------------------------------------------ tracked repos
def list_tracked(db):
    return [dict(r) for r in db.execute("SELECT * FROM tracked_repos ORDER BY full_name")]


def add_tracked(db, full_name, who=None, enabled=True):
    if not REPO_RE.match(full_name or ""):
        raise ValueError(f"not a repo name (owner/name): {full_name!r}")
    db.execute("INSERT INTO tracked_repos(full_name, enabled, added_by, added_at) VALUES (?,?,?,?) "
               "ON CONFLICT(full_name) DO UPDATE SET enabled=excluded.enabled", (full_name, int(enabled), who, now()))


def remove_tracked(db, full_name):
    db.execute("DELETE FROM tracked_repos WHERE full_name=?", (full_name,))


# ------------------------------------------------------------------ app users
def list_app_users(db):
    return [dict(r) for r in db.execute("SELECT * FROM app_users ORDER BY role, COALESCE(gh_login, email, name)")]


def upsert_app_user(db, data, who=None):
    """Create or update by id. Needs at least a GitHub login, GitHub id or email to recognise the person by."""
    role = data.get("role") or "self"
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    gh_login = (data.get("gh_login") or "").strip().lstrip("@") or None
    email = (data.get("email") or "").strip().lower() or None
    gh_id = int(data["gh_id"]) if data.get("gh_id") not in (None, "") else None
    if not (gh_login or email or gh_id):
        raise ValueError("give a GitHub login, GitHub id or email")
    uid = (data.get("uid") or "").strip() or None
    if uid and not db.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
        raise ValueError(f"unknown person {uid!r} (see the People list)")
    vals = (gh_id, gh_login, email, (data.get("name") or "").strip() or None, role, uid, int(data.get("active", 1)))
    if data.get("id"):
        db.execute("UPDATE app_users SET gh_id=?, gh_login=?, email=?, name=?, role=?, uid=?, active=? WHERE id=?",
                   (*vals, int(data["id"])))
        return int(data["id"])
    return db.execute("INSERT INTO app_users(gh_id, gh_login, email, name, role, uid, active, created_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (*vals, who, now())).lastrowid


def delete_app_user(db, user_id):
    db.execute("DELETE FROM app_users WHERE id=?", (int(user_id),))


def find_app_user(db, gh_id=None, gh_login=None, emails=()):
    """The explicit mapping row for a GitHub profile: by id, then login, then any verified email."""
    if gh_id is not None:
        r = db.execute("SELECT * FROM app_users WHERE gh_id=?", (gh_id,)).fetchone()
        if r:
            return dict(r)
    if gh_login:
        r = db.execute("SELECT * FROM app_users WHERE gh_login=?", (gh_login,)).fetchone()
        if r:
            return dict(r)
    for e in emails:
        r = db.execute("SELECT * FROM app_users WHERE email=?", (e,)).fetchone()
        if r:
            return dict(r)
    return None


def implicit_uid(db, gh_id=None, gh_login=None, emails=()):
    """A person already collected from the repos (users / aliases) that this GitHub profile obviously is."""
    if gh_id is not None and db.execute("SELECT 1 FROM users WHERE uid=? AND is_bot=0", (f"gh:{gh_id}",)).fetchone():
        return f"gh:{gh_id}"
    for alias in [gh_login, *emails]:
        if alias:
            r = db.execute("SELECT a.uid FROM user_aliases a JOIN users u ON u.uid=a.uid WHERE a.alias=? AND u.is_bot=0", (alias,)).fetchone()
            if r:
                return r[0]
    return None


def remember_profile(db, app_user_id, gh_id, gh_login):
    """Fill in GitHub id/login on a row that was created from an email, so later logins match by id."""
    db.execute("UPDATE app_users SET gh_id=COALESCE(gh_id, ?), gh_login=COALESCE(gh_login, ?) WHERE id=?",
               (gh_id, gh_login, app_user_id))


# ------------------------------------------------------------------ jobs
def create_job(db, kind, params, who=None):
    return db.execute("INSERT INTO jobs(kind, status, params, created_by, created_at) VALUES (?,?,?,?,?)",
                      (kind, "queued", json.dumps(params), who, now())).lastrowid


def job_event(db, job_id, message, level="info"):
    db.execute("INSERT INTO job_events(job_id, ts, level, message) VALUES (?,?,?,?)", (job_id, now(), level, str(message)[:2000]))


def _job(r):
    d = dict(r)
    for k in ("params", "state", "summary"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get_job(db, job_id, events=True):
    r = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    d = _job(r)
    if events:
        d["events"] = [dict(e) for e in db.execute("SELECT ts, level, message FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 300", (job_id,))][::-1]
    return d


def list_jobs(db, limit=50):
    return [{k: v for k, v in _job(r).items() if k not in ("state",)} for r in
            db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


def update_job(db, job_id, **fields):
    for k in ("params", "state", "summary"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*fields.values(), job_id])
