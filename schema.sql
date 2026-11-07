-- PR / code-review audit store (GitHub). Natural keys + UPSERTs => overlapping runs never duplicate.
PRAGMA foreign_keys = ON;

-- Users are discovered automatically from the repos.
-- uid = 'gh:<GitHub numeric user id>' (stable even if the login is renamed)
--       'email:<address>' only for commit authors not linked to any GitHub account
CREATE TABLE IF NOT EXISTS users (
  uid        TEXT PRIMARY KEY,
  login      TEXT,
  name       TEXT,
  is_bot     INTEGER DEFAULT 0
);

-- Every login / commit email that maps to a user (one person, many emails => one uid)
CREATE TABLE IF NOT EXISTS user_aliases (
  alias  TEXT PRIMARY KEY COLLATE NOCASE,
  uid    TEXT NOT NULL REFERENCES users(uid) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS repos (
  id             INTEGER PRIMARY KEY,
  full_name      TEXT NOT NULL UNIQUE,          -- owner/name
  url            TEXT,
  default_branch TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
  id            INTEGER PRIMARY KEY,
  repo_id       INTEGER NOT NULL REFERENCES repos(id),
  number        INTEGER NOT NULL,
  title         TEXT,
  body          TEXT,
  author_uid    TEXT REFERENCES users(uid) ON UPDATE CASCADE,
  state         TEXT,                           -- open / merged / closed
  base_branch   TEXT,
  head_branch   TEXT,
  kind          TEXT DEFAULT 'change',   -- change / promotion (branch-to-branch merge, not scored)
  head_sha      TEXT,                           -- changes => PR is re-evaluated
  created_at    TEXT,
  merged_at     TEXT,
  merged_by_uid TEXT,
  closed_at     TEXT,
  activity_at   TEXT,                           -- latest of created / merged / closed / last commit
  additions     INTEGER, deletions INTEGER, changed_files INTEGER,
  ci_status     TEXT,                           -- SUCCESS / FAILURE / PENDING / unknown
  files         TEXT,                           -- JSON list of changed paths
  url           TEXT,
  last_synced   TEXT,
  UNIQUE (repo_id, number)
);
CREATE INDEX IF NOT EXISTS ix_pr_author  ON pull_requests(author_uid, created_at);

CREATE TABLE IF NOT EXISTS commits (
  repo_id      INTEGER NOT NULL REFERENCES repos(id),
  sha          TEXT NOT NULL,
  author_uid   TEXT REFERENCES users(uid) ON UPDATE CASCADE,
  authored_at  TEXT,
  message      TEXT,
  additions    INTEGER, deletions INTEGER,
  pr_id        INTEGER REFERENCES pull_requests(id),
  direct_push  INTEGER DEFAULT 0,
  PRIMARY KEY (repo_id, sha)
);
CREATE INDEX IF NOT EXISTS ix_commit_author ON commits(author_uid, authored_at);

-- Extra authors of a commit ("Co-authored-by:" trailers). The main author is commits.author_uid.
CREATE TABLE IF NOT EXISTS commit_coauthors (
  repo_id  INTEGER NOT NULL REFERENCES repos(id),
  sha      TEXT NOT NULL,
  uid      TEXT REFERENCES users(uid) ON UPDATE CASCADE,
  PRIMARY KEY (repo_id, sha, uid)
);

-- Who answers for what in a PR. The owner (PR author) answers for validation and review response;
-- every person whose commits are in the PR answers for the commit messages and code quality of their own
-- commits. Single-author PRs have one row (the owner, with the PR's scores).
CREATE TABLE IF NOT EXISTS pr_contributions (
  pr_id           INTEGER NOT NULL REFERENCES pull_requests(id),
  head_sha        TEXT NOT NULL,
  rubric_version  TEXT NOT NULL,
  uid             TEXT NOT NULL REFERENCES users(uid) ON UPDATE CASCADE,
  is_owner        INTEGER NOT NULL DEFAULT 0,
  commits         INTEGER, additions INTEGER, deletions INTEGER,
  commit_msg      REAL, code_quality REAL, validation REAL, review_response REAL,
  overall         REAL,
  notes           TEXT,
  PRIMARY KEY (pr_id, head_sha, rubric_version, uid)
);
CREATE INDEX IF NOT EXISTS ix_contrib_uid ON pr_contributions(uid);

CREATE TABLE IF NOT EXISTS reviews (
  host_id       TEXT PRIMARY KEY,
  pr_id         INTEGER NOT NULL REFERENCES pull_requests(id),
  reviewer_uid  TEXT REFERENCES users(uid) ON UPDATE CASCADE,
  state         TEXT,                           -- APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED
  body          TEXT,
  submitted_at  TEXT
);

CREATE TABLE IF NOT EXISTS review_threads (
  thread_id        TEXT PRIMARY KEY,
  pr_id            INTEGER NOT NULL REFERENCES pull_requests(id),
  path             TEXT,
  line             INTEGER,
  is_resolved      INTEGER,
  is_outdated      INTEGER,
  resolved_by_uid  TEXT
);

-- kind = 'review' (inline, belongs to a thread) or 'conversation' (PR timeline comment)
CREATE TABLE IF NOT EXISTS comments (
  host_id     TEXT PRIMARY KEY,
  pr_id       INTEGER NOT NULL REFERENCES pull_requests(id),
  thread_id   TEXT REFERENCES review_threads(thread_id),
  kind        TEXT,
  author_uid  TEXT REFERENCES users(uid) ON UPDATE CASCADE,
  body        TEXT,
  created_at  TEXT
);

-- ===== Claude's evaluations (keyed by code version + rubric version) =====
CREATE TABLE IF NOT EXISTS pr_evaluations (
  pr_id            INTEGER NOT NULL REFERENCES pull_requests(id),
  head_sha         TEXT NOT NULL,
  rubric_version   TEXT NOT NULL,
  commit_msg       INTEGER, code_quality INTEGER, validation INTEGER, review_response INTEGER,
  overall          REAL,
  tests_added      INTEGER,
  commit_notes     TEXT,
  validation_notes TEXT,
  code_issues      TEXT,     -- JSON
  summary          TEXT,
  talking_points   TEXT,     -- JSON
  evaluated_at     TEXT,
  PRIMARY KEY (pr_id, head_sha, rubric_version)
);

-- one row per review thread (keyed by the thread's first comment id)
CREATE TABLE IF NOT EXISTS thread_evaluations (
  comment_id     TEXT NOT NULL,
  pr_id          INTEGER NOT NULL REFERENCES pull_requests(id),
  head_sha       TEXT NOT NULL,
  rubric_version TEXT NOT NULL,
  validity       TEXT,       -- valid / nitpick / invalid
  resolution     TEXT,       -- fixed_in_code / explained / dismissed / open
  fix_sha        TEXT,
  note           TEXT,
  PRIMARY KEY (comment_id, head_sha, rubric_version)
);

CREATE TABLE IF NOT EXISTS reviewer_evaluations (
  pr_id          INTEGER NOT NULL REFERENCES pull_requests(id),
  head_sha       TEXT NOT NULL,
  rubric_version TEXT NOT NULL,
  reviewer_uid   TEXT NOT NULL,
  score          INTEGER,
  missed         TEXT,       -- JSON
  notes          TEXT,
  PRIMARY KEY (pr_id, head_sha, rubric_version, reviewer_uid)
);

-- Rework: pr_id (later) fixes/reverts original_pr. Proposed by heuristics, confirmed by Claude.
CREATE TABLE IF NOT EXISTS rework_links (
  pr_id        INTEGER NOT NULL REFERENCES pull_requests(id),
  original_pr  INTEGER NOT NULL REFERENCES pull_requests(id),
  kind         TEXT,         -- self_fix / fixed_by_other / revert
  reason       TEXT,         -- why it was proposed
  confirmed    INTEGER,      -- NULL = not reviewed yet, 1 = confirmed, 0 = rejected
  evidence     TEXT,
  PRIMARY KEY (pr_id, original_pr)
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, repos TEXT, since TEXT, until TEXT, stats TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Latest evaluation for the PR's current code under the current rubric
CREATE VIEW IF NOT EXISTS v_pr AS
SELECT p.*, r.full_name AS repo, u.login AS author_login,
       e.commit_msg, e.code_quality, e.validation, e.review_response, e.overall, e.tests_added,
       e.commit_notes, e.validation_notes, e.code_issues, e.summary, e.talking_points, e.evaluated_at,
       e.rubric_version, e.engine
FROM pull_requests p
JOIN repos r ON r.id = p.repo_id
LEFT JOIN users u ON u.uid = p.author_uid
LEFT JOIN pr_evaluations e ON e.pr_id = p.id AND e.head_sha = p.head_sha
     AND e.rubric_version = (SELECT value FROM meta WHERE key = 'rubric_version');
