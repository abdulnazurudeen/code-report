"""The scoring skill: SKILL.md (procedure), rubric.md (standards) and agents/*.md (who scores, how).

The files under skills/pr-scoring/ are the default. An admin can edit them in the UI; an edited copy is stored
in the database (skill_versions) and becomes the active skill until reset. Every version has a short hash that
is stored on each evaluation, so you can always tell which instructions produced a score.

An agent file is a small markdown file with `key: value` front matter:
    name, description, mode (single | agentic), effort, max_diff_chars, max_turns
and a body that is appended to the system prompt for that agent.
"""
import hashlib, json, re
from pathlib import Path
from store import now

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE / "skills" / "pr-scoring"
REQUIRED = ("SKILL.md", "rubric.md", "agents/scorer.md")
NAME_RE = re.compile(r"^(SKILL\.md|rubric\.md|agents/[a-z0-9][a-z0-9-]{0,40}\.md)$")
MAX_FILE = 60_000
MAX_FILES = 12
MODES = ("single", "agentic")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ACTIVE_KEY = "skill_active"          # settings key holding the hash of the active edited version


def repo_files():
    out = {}
    for p in sorted(SKILL_DIR.rglob("*.md")):
        out[p.relative_to(SKILL_DIR).as_posix()] = p.read_text(encoding="utf-8")
    return out


def version_hash(files):
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode()); h.update(b"\0"); h.update(files[k].replace("\r\n", "\n").encode()); h.update(b"\0")
    return h.hexdigest()[:12]


def active(db=None):
    """(files, hash, source) of the skill in force: the admin's saved version if there is one, else the repo files."""
    if db is not None:
        r = db.execute("SELECT value FROM settings WHERE key=?", (ACTIVE_KEY,)).fetchone()
        if r:
            h = json.loads(r[0])
            v = db.execute("SELECT files FROM skill_versions WHERE hash=?", (h,)).fetchone()
            if v:
                return json.loads(v[0]), h, "edited"
    files = repo_files()
    return files, version_hash(files), "repo"


def parse_agent(name, text):
    """-> dict(name, description, mode, effort, max_diff_chars, max_turns, prompt). Raises ValueError if malformed."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text.replace("\r\n", "\n"), re.S)
    if not m:
        raise ValueError(f"{name}: missing front matter (--- ... ---)")
    meta = {}
    for line in m.group(1).splitlines():
        if line.strip() and ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    a = {"name": meta.get("name") or Path(name).stem, "description": meta.get("description", ""),
         "mode": meta.get("mode", "single"), "effort": meta.get("effort", "high"), "prompt": m.group(2).strip()}
    try:
        a["max_diff_chars"] = int(meta.get("max_diff_chars", 600_000))
        a["max_turns"] = int(meta.get("max_turns", 12))
    except ValueError:
        raise ValueError(f"{name}: max_diff_chars / max_turns must be numbers")
    if a["mode"] not in MODES:
        raise ValueError(f"{name}: mode must be one of {MODES}")
    if a["effort"] not in EFFORTS:
        raise ValueError(f"{name}: effort must be one of {EFFORTS}")
    if not 1 <= a["max_turns"] <= 40 or not 10_000 <= a["max_diff_chars"] <= 4_000_000:
        raise ValueError(f"{name}: max_turns 1-40, max_diff_chars 10000-4000000")
    if a["name"] != Path(name).stem:
        raise ValueError(f"{name}: `name:` must equal the file name ({Path(name).stem})")
    return a


def agents(files):
    return {Path(k).stem: parse_agent(k, v) for k, v in files.items() if k.startswith("agents/")}


def validate(files):
    """Raise ValueError unless this set of files is a usable skill."""
    if not isinstance(files, dict) or not files:
        raise ValueError("no files")
    if len(files) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} files")
    for k, v in files.items():
        if not NAME_RE.match(k):
            raise ValueError(f"file name not allowed: {k!r} (SKILL.md, rubric.md, agents/<name>.md)")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{k} is empty")
        if len(v) > MAX_FILE:
            raise ValueError(f"{k} is too long (max {MAX_FILE} characters)")
    for k in REQUIRED:
        if k not in files:
            raise ValueError(f"{k} is required")
    agents(files)                       # parses every agent


def save_version(db, files, who=None, note=None):
    """Store an edited skill and make it active. Returns its hash."""
    validate(files)
    h = version_hash(files)
    db.execute("INSERT INTO skill_versions(hash, created_at, created_by, note, files) VALUES (?,?,?,?,?) "
               "ON CONFLICT(hash) DO UPDATE SET note=COALESCE(excluded.note, skill_versions.note)",
               (h, now(), who, note, json.dumps(files)))
    db.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               (ACTIVE_KEY, json.dumps(h), now(), who))
    return h


def activate(db, h, who=None):
    if not db.execute("SELECT 1 FROM skill_versions WHERE hash=?", (h,)).fetchone():
        raise ValueError("unknown version")
    db.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
               (ACTIVE_KEY, json.dumps(h), now(), who))


def reset(db):
    """Go back to the files in the repo."""
    db.execute("DELETE FROM settings WHERE key=?", (ACTIVE_KEY,))


def versions(db):
    return [dict(r) for r in db.execute("SELECT hash, created_at, created_by, note FROM skill_versions ORDER BY created_at DESC")]


def version_files(db, h):
    if h == version_hash(repo_files()):
        return repo_files()
    r = db.execute("SELECT files FROM skill_versions WHERE hash=?", (h,)).fetchone()
    return json.loads(r[0]) if r else None


def system_prompt(files, agent_name):
    """The system prompt for one agent: procedure + the agent's own instructions + the rubric."""
    a = agents(files).get(agent_name)
    if a is None:
        raise ValueError(f"unknown agent {agent_name!r}")
    return (files["SKILL.md"].strip() + "\n\n## Your role: " + a["name"] + "\n" + a["prompt"] +
            "\n\n# RUBRIC\n\n" + files["rubric.md"].strip() + "\n")
