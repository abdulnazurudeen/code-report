"""Which model / effort / agent scores which PR, and what a run will cost.

Rules are an ordered list; the first rule whose `when` matches wins. `when` keys (all must match):
  min_lines, max_lines   additions + deletions
  min_files, max_files   changed files
  shared                 true/false: PR has commits from more than one person
  truncated              true/false: the diff is too big to include whole
  repo                   list of repo names or globs (e.g. ["acme/payments-*"])
  path_regex             matches any changed file path
  state                  merged | open | closed
  prior_engine           heuristic | claude-code | anthropic-api | none (how it was scored before)
An empty `when` matches everything (use it for the last, default rule).
The default rules and model prices are in skills/pr-scoring/models.json; an admin's edited rules live in settings.
"""
import fnmatch, json, re
from pathlib import Path

CATALOG_FILE = Path(__file__).resolve().parent / "skills" / "pr-scoring" / "models.json"
SETTING_KEY = "routing_rules"
WHEN_KEYS = {"min_lines", "max_lines", "min_files", "max_files", "shared", "truncated", "repo", "path_regex", "state", "prior_engine"}
CHARS_PER_TOKEN = 3.5
MAX_BUNDLE_DIFF_CHARS = 100_000      # audit.MAX_DIFF_CHARS: a bigger diff is flagged truncated in the bundle


def catalog():
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def default_rules():
    return catalog()["default_rules"]


def load_rules(db=None):
    """The admin's rules if saved, else the defaults."""
    if db is not None:
        r = db.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
        if r:
            return json.loads(r[0])
    return default_rules()


def validate_rules(rules, agent_names):
    """Raise ValueError unless every rule is usable. Returns the cleaned list."""
    cat = catalog()
    if not isinstance(rules, list) or not 1 <= len(rules) <= 50:
        raise ValueError("give 1-50 rules")
    out = []
    for i, r in enumerate(rules, 1):
        when = r.get("when") or {}
        if not isinstance(when, dict) or set(when) - WHEN_KEYS:
            raise ValueError(f"rule {i}: unknown condition(s) {sorted(set(when) - WHEN_KEYS)}")
        if r.get("model") not in cat["models"]:
            raise ValueError(f"rule {i}: model must be one of {sorted(cat['models'])}")
        if r.get("effort") not in cat["efforts"]:
            raise ValueError(f"rule {i}: effort must be one of {cat['efforts']}")
        if r.get("agent") not in agent_names:
            raise ValueError(f"rule {i}: agent must be one of {sorted(agent_names)}")
        if when.get("path_regex"):
            try:
                re.compile(when["path_regex"])
            except re.error as e:
                raise ValueError(f"rule {i}: bad path_regex ({e})")
        for k in ("min_lines", "max_lines", "min_files", "max_files"):
            if k in when and not isinstance(when[k], int):
                raise ValueError(f"rule {i}: {k} must be a whole number")
        for k in ("shared", "truncated"):
            if k in when and not isinstance(when[k], bool):
                raise ValueError(f"rule {i}: {k} must be true or false")
        if "repo" in when and not (isinstance(when["repo"], list) and all(isinstance(x, str) for x in when["repo"])):
            raise ValueError(f"rule {i}: repo must be a list of names")
        out.append({"name": str(r.get("name") or f"Rule {i}")[:80], "when": when, "agent": r["agent"],
                    "model": r["model"], "effort": r["effort"]})
    if out[-1]["when"]:
        raise ValueError("the last rule must have no conditions (it is the default for everything else)")
    return out


def features(b, diff=None):
    """What the rules look at, from a review bundle (and the full diff if you have it)."""
    size = b.get("size") or {}
    diff = diff if diff is not None else b.get("diff") or ""
    paths = [m.group(1) for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", diff, re.M)] or b.get("files") or []
    return {"lines": (size.get("additions") or 0) + (size.get("deletions") or 0), "files": size.get("files") or len(paths),
            "shared": bool(b.get("shared_pr")), "truncated": len(diff) > MAX_BUNDLE_DIFF_CHARS, "repo": b.get("repo") or "",
            "paths": paths, "state": b.get("state"), "prior_engine": b.get("prior_engine") or "none"}


def matches(when, f):
    if "min_lines" in when and f["lines"] < when["min_lines"]: return False
    if "max_lines" in when and f["lines"] > when["max_lines"]: return False
    if "min_files" in when and f["files"] < when["min_files"]: return False
    if "max_files" in when and f["files"] > when["max_files"]: return False
    if "shared" in when and f["shared"] != when["shared"]: return False
    if "truncated" in when and f["truncated"] != when["truncated"]: return False
    if "state" in when and f["state"] != when["state"]: return False
    if "prior_engine" in when and f["prior_engine"] != when["prior_engine"]: return False
    if when.get("repo") and not any(fnmatch.fnmatch(f["repo"], pat) for pat in when["repo"]): return False
    if when.get("path_regex") and not any(re.search(when["path_regex"], p, re.I) for p in f["paths"]): return False
    return True


def choose(rules, f):
    """-> (index, rule) of the first matching rule."""
    for i, r in enumerate(rules):
        if matches(r.get("when") or {}, f):
            return i, r
    raise ValueError("no routing rule matched (the last rule should have no conditions)")


# ------------------------------------------------------------------ cost
def cost_usd(model, usage, batch=False):
    """USD for one call from its token usage {input, output, cache_read, cache_write}."""
    m = catalog()["models"].get(model)
    if not m:
        return None
    u = {k: (usage or {}).get(k, 0) or 0 for k in ("input", "output", "cache_read", "cache_write")}
    long_ctx = m.get("long_over") and (u["input"] + u["cache_read"] + u["cache_write"]) > m["long_over"]
    pin = m["input_long"] if long_ctx else m["input"]
    pout = m["output_long"] if long_ctx else m["output"]
    usd = (u["input"] * pin + u["output"] * pout + u["cache_read"] * m["cache_read"] + u["cache_write"] * pin * 1.25) / 1e6
    return round(usd * (0.5 if batch else 1), 4)


def estimate_pr(model, effort, agent_mode, in_chars, batch=False):
    """Rough USD for scoring one PR; the system prompt is assumed cached after the first call."""
    cat = catalog()
    out_tokens = cat["output_tokens_by_effort"].get(effort, 6000)
    in_tokens = int(in_chars / CHARS_PER_TOKEN)
    turns = 1
    if agent_mode == "agentic":                         # every turn re-reads the context (cached) and adds tool output
        turns = 6
        out_tokens = int(out_tokens * 1.5)
        in_tokens = int(in_tokens * 0.5)                # only the diff head is in the bundle; files are read on demand
    usage = {"input": in_tokens, "output": out_tokens, "cache_read": 6000 * turns + in_tokens * (turns - 1)}
    return {"usd": cost_usd(model, usage, batch), "input_tokens": in_tokens * turns, "output_tokens": out_tokens}
