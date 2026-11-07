"""Minimal read-only GitHub client (stdlib only). Needs a token with read access to the repos."""
import json, os, subprocess, time, urllib.request, urllib.error
from pathlib import Path

API = os.environ.get("GITHUB_API", "https://api.github.com")


def load_token(token_file=None):
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"].strip()
    for f in filter(None, [token_file, Path(__file__).with_name(".github_token")]):
        if Path(f).exists():
            return Path(f).read_text(encoding="utf-8-sig").strip()
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        raise SystemExit("No GitHub token. Put it in .github_token next to audit.py, or set GITHUB_TOKEN.")


class GitHub:
    def __init__(self, token):
        self.token = token

    def _req(self, url, data=None, accept="application/vnd.github+json"):
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, headers={
                "Authorization": f"Bearer {self.token}", "Accept": accept,
                "User-Agent": "pr-audit", "X-GitHub-Api-Version": "2022-11-28"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code in (502, 503, 504) or (e.code in (403, 429) and "rate limit" in body.lower()):
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = max(5, int(reset) - int(time.time())) if reset and e.code != 502 else 5 * (attempt + 1)
                    time.sleep(min(wait, 900))
                    continue
                raise SystemExit(f"GitHub API {e.code} for {url}: {body[:300]}")
        raise SystemExit(f"GitHub API kept failing for {url}")

    def graphql(self, query, variables):
        out = json.loads(self._req(f"{API}/graphql", json.dumps({"query": query, "variables": variables}).encode()))
        if out.get("errors") and not out.get("data"):
            raise SystemExit(f"GraphQL error: {out['errors'][:2]}")
        return out["data"]

    def commit_files(self, full_name, sha):
        """Files one commit changed, with line counts (REST; GitHub caps the list at 300 files)."""
        c = json.loads(self._req(f"{API}/repos/{full_name}/commits/{sha}"))
        return [{"file": f["filename"], "additions": f.get("additions"), "deletions": f.get("deletions")} for f in c.get("files") or []]

    def org_repos(self, org):
        """Every repo of the org the token can see (REST, newest push first)."""
        out, page = [], 1
        while True:
            batch = json.loads(self._req(f"{API}/orgs/{org}/repos?per_page=100&sort=pushed&page={page}"))
            if not batch:
                return out
            out += [{"full_name": r["full_name"], "pushed_at": r["pushed_at"], "default_branch": r["default_branch"],
                     "archived": r["archived"], "fork": r["fork"]} for r in batch]
            page += 1

    def pr_diff(self, full_name, number):
        try:
            return self._req(f"{API}/repos/{full_name}/pulls/{number}", accept="application/vnd.github.diff").decode(errors="replace")
        except SystemExit as e:
            if "too_large" not in str(e) and "406" not in str(e):
                raise
        # GitHub refuses diffs over 300 files: rebuild one from the paged file list (up to 3000 files)
        parts = [f"# NOTE: GitHub refused the full diff (too many files); rebuilt from the PR file list.\n"]
        for page in range(1, 31):
            files = json.loads(self._req(f"{API}/repos/{full_name}/pulls/{number}/files?per_page=100&page={page}"))
            for f in files:
                name = f["filename"]
                parts.append(f"diff --git a/{f.get('previous_filename') or name} b/{name}\n"
                             f"# status: {f.get('status')} +{f.get('additions')} -{f.get('deletions')}\n"
                             f"--- a/{f.get('previous_filename') or name}\n+++ b/{name}\n"
                             + (f.get("patch") or "# (binary or too large to show)") + "\n")
            if len(files) < 100:
                break
        return "".join(parts)


ACTOR = "login ... on User { databaseId name } __typename"

PRS_QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    url
    defaultBranchRef { name }
    pullRequests(first: 20, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url state createdAt updatedAt mergedAt closedAt
        additions deletions changedFiles baseRefName headRefName headRefOid
        author { %(a)s }
        mergedBy { %(a)s }
        files(first: 100) { nodes { path } }
        commits(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { commit {
          oid message authoredDate additions deletions
          author { email name user { login databaseId } }
          authors(first: 5) { nodes { email name user { login databaseId } } }
          statusCheckRollup { state }
        } } }
        reviews(first: 50) { nodes { databaseId state body submittedAt author { %(a)s } } }
        reviewThreads(first: 100) { nodes {
          id isResolved isOutdated path line resolvedBy { %(a)s }
          comments(first: 50) { nodes { databaseId body createdAt author { %(a)s } } }
        } }
        comments(first: 50) { nodes { databaseId body createdAt author { %(a)s } } }
      }
    }
  }
}""" % {"a": ACTOR}

PR_COMMITS_QUERY = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) { pullRequest(number:$number) {
    commits(first: 100, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { commit {
      oid message authoredDate additions deletions
          author { email name user { login databaseId } }
          authors(first: 5) { nodes { email name user { login databaseId } } }
          statusCheckRollup { state }
    } } }
  } }
}"""

HISTORY_QUERY = """
query($owner:String!, $name:String!, $since:GitTimestamp!, $until:GitTimestamp!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, since: $since, until: $until, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          oid message authoredDate additions deletions
          author { email name user { login databaseId } }
          authors(first: 5) { nodes { email name user { login databaseId } } }
          associatedPullRequests(first: 10) { totalCount nodes { headRefName } }
        }
      }
    } } }
  }
}"""


def iter_prs(gh, full_name, since):
    """Yield PR nodes, most recently updated first, until updatedAt < since (nothing older can have
    activity in the window). Closed and merged PRs are included, not only open ones."""
    owner, name = full_name.split("/", 1)
    cursor = None
    while True:
        repo = gh.graphql(PRS_QUERY, {"owner": owner, "name": name, "cursor": cursor})["repository"]
        if repo is None:
            raise SystemExit(f"Repo {full_name} not found or token has no access")
        page = repo["pullRequests"]
        for node in page["nodes"]:
            if node["updatedAt"][:10] < since:
                return
            yield repo, node
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]


def all_pr_commits(gh, full_name, node):
    """Commits of a PR; follows pagination when a PR has more than 100 commits."""
    commits = [c["commit"] for c in node["commits"]["nodes"]]
    page = node["commits"].get("pageInfo") or {}
    owner, name = full_name.split("/", 1)
    while page.get("hasNextPage"):
        data = gh.graphql(PR_COMMITS_QUERY, {"owner": owner, "name": name, "number": node["number"],
                                             "cursor": page["endCursor"]})["repository"]["pullRequest"]["commits"]
        commits += [c["commit"] for c in data["nodes"]]
        page = data["pageInfo"]
    return commits


def iter_default_branch_commits(gh, full_name, since, until):
    owner, name = full_name.split("/", 1)
    cursor = None
    while True:
        repo = gh.graphql(HISTORY_QUERY, {"owner": owner, "name": name, "cursor": cursor,
                                          "since": f"{since}T00:00:00Z", "until": f"{until}T23:59:59Z"})["repository"]
        ref = repo and repo["defaultBranchRef"]
        if not ref:
            return
        hist = ref["target"]["history"]
        yield from hist["nodes"]
        if not hist["pageInfo"]["hasNextPage"]:
            return
        cursor = hist["pageInfo"]["endCursor"]
