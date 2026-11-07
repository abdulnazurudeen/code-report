"""Scoring engines that run without a Claude Code session.

  heuristic      - rule-based, no LLM, no token, free. Objective checks only (commit format, CI, tests, review
                   process, risky patterns in the diff). It cannot judge logic, so code_quality is an estimate
                   capped at 4 and every result is marked engine="heuristic" in the dashboard.
  anthropic-api  - the same review the Claude Code agents do, through the Anthropic API with ANTHROPIC_API_KEY
                   (pay per use, needs `pip install -r requirements-llm.txt`). Good for unattended/scheduled runs.

Both return an evaluation dict in the STANDARDS.md JSON contract, ready for store.save_evaluation().
"""
import json, os, re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ------------------------------------------------------------------ heuristic (no LLM)
CONVENTIONAL = re.compile(r"^(feat|fix|refactor|perf|test|docs|build|ci|chore|revert|style)(\([^)]+\))?!?: \S")
BANNED = {"fix", "fixes", "fixed", "fix stuff", "wip", "update", "updated", "updates", "changes", "change", "minor",
          "asdf", "temp", "test", "done", "issue fixed", "bug fix", "bugfix", "fixes done", "changes done"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(test|spec)\.[a-z]+$|_test\.(py|go)$|test_[^/]+\.py$", re.I)
SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|sk_live_[0-9a-zA-Z]{10,}|"
                    r"(api[_-]?key|secret|password|server[_-]?key)\s*[:=]\s*['\"][^'\"\s]{12,}['\"])", re.I)
RISKY = [(re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*=|\beval\("), "major",
          "renders/executes unsanitised content (XSS / code-injection risk) - check the input is trusted"),
         (re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED"), "major",
          "TLS certificate verification disabled")]
DEBUG = re.compile(r"\bconsole\.(log|debug)\(|\bprint\(|\bdebugger\b")
COMMENTED_CODE = re.compile(r"^\s*(//|#)\s*(const|let|var|return|if|for|await|import|def|function|<\w)")
TODO = re.compile(r"\b(TODO|FIXME|HACK)\b")
TEMPLATE_HINTS = ("- [ ]", "<!--", "describe your changes", "type of change", "how has this been tested")


def _subject(msg):
    return (msg or "").strip().splitlines()[0].strip() if (msg or "").strip() else ""


def _commit_score(msg):
    s = _subject(msg)
    if not s or s.lower().rstrip(".!") in BANNED or len(s.split()) <= 1:
        return 1, f'"{s}" says nothing about the change'
    conv, short = bool(CONVENTIONAL.match(s)), len(s) <= 72 and not s.endswith(".")
    body = len((msg or "").strip().splitlines()) > 2
    if conv and short:
        return (5 if body else 4), None
    if conv:
        return 3, f'"{s[:60]}…" is over 72 chars or ends with a period'
    if len(s.split()) >= 3:
        return 3, f'"{s[:60]}" is not Conventional Commits (type(scope): summary)'
    return 2, f'"{s}" is vague and not Conventional Commits'


def _diff_files(diff):
    """{path: [added lines]} from a unified diff."""
    files, cur = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
            files.setdefault(cur, [])
        elif cur and line.startswith("+") and not line.startswith("+++"):
            files[cur].append(line[1:])
    return files


def _merged_without_approval(b):
    if b.get("state") != "merged" and not b.get("merged_at"):
        return False
    merged_at = b.get("merged_at") or "9999"
    return not any(r.get("state") == "APPROVED" and r.get("reviewer") != b.get("author")
                   and (r.get("submitted_at") or "") <= merged_at for r in b.get("reviews") or [])


def heuristic_eval(b, diff=None):
    diff = diff if diff is not None else b.get("diff") or ""
    commits = [c for c in b.get("commits") or [] if not (c.get("message") or "").startswith("Merge ")]
    issues, points = [], []

    # commit messages
    scored = [(c, *_commit_score(c.get("message"))) for c in commits]
    commit_msg = round(sum(s for _, s, _ in scored) / len(scored)) if scored else 3
    bad = [n for _, _, n in scored if n][:3]

    # code quality - only what rules can see; never above 4 because logic was not reviewed
    files = _diff_files(diff)
    debug = commented = todos = 0
    for path, added in files.items():
        test_file = bool(TEST_PATH.search(path))
        for i, line in enumerate(added):
            if SECRET.search(line) and not test_file and "process.env" not in line and "os.environ" not in line:
                issues.append({"severity": "blocker", "file": path, "line": None, "caught_by_reviewer": False,
                               "issue": "looks like a hard-coded secret/credential - rotate it and load it from configuration"})
            for rx, sev, text in RISKY:
                if rx.search(line) and not test_file:
                    issues.append({"severity": sev, "file": path, "line": None, "issue": text, "caught_by_reviewer": False})
            debug += bool(DEBUG.search(line)) and not test_file
            commented += bool(COMMENTED_CODE.search(line))
            todos += bool(TODO.search(line))
    if debug > 5:
        issues.append({"severity": "minor", "file": "(several)", "line": None, "caught_by_reviewer": False,
                       "issue": f"{debug} debug log/print statements added"})
    if commented > 10:
        issues.append({"severity": "minor", "file": "(several)", "line": None, "caught_by_reviewer": False,
                       "issue": f"{commented} lines of commented-out code added instead of deleting it"})
    if todos > 3:
        issues.append({"severity": "nit", "file": "(several)", "line": None, "caught_by_reviewer": False,
                       "issue": f"{todos} TODO/FIXME markers added"})
    size = b.get("size") or {}
    lines = (size.get("additions") or 0) + (size.get("deletions") or 0)
    top_dirs = {p.split("/")[0] for p in files if "/" in p}
    if lines > 1500 and len(top_dirs) > 3:
        issues.append({"severity": "major", "file": "(PR)", "line": None, "caught_by_reviewer": False,
                       "issue": f"{lines} changed lines across {len(top_dirs)} top-level areas - likely several changes in one PR"})
    sev = [i["severity"] for i in issues]
    code_quality = (1 if "blocker" in sev else 2 if sev.count("major") >= 2 else
                    3 if "major" in sev or sev.count("minor") >= 3 else 4)
    # de-duplicate identical issues per file
    seen, uniq = set(), []
    for i in issues:
        k = (i["severity"], i["file"], i["issue"])
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    issues = uniq[:25]

    # validation
    tests_added = any(TEST_PATH.search(p) for p in files)
    v, vnotes = 5, []
    ci = (b.get("ci_status") or "unknown").upper()
    merged = b.get("state") == "merged" or bool(b.get("merged_at"))
    if ci in ("FAILURE", "ERROR"):
        v -= 2 if merged else 1
        vnotes.append(f"CI {ci}{' at merge' if merged else ''} (which check failed is not known to the tool)")
    elif ci != "SUCCESS":
        v -= 1
        vnotes.append("no CI result found")
    if not tests_added and lines > 50:
        v -= 1
        vnotes.append("no test files changed")
    body = (b.get("body") or "").strip()
    if len(body) < 40 or (sum(h in body.lower() for h in TEMPLATE_HINTS) >= 2 and "- [x]" not in body.lower()):
        v -= 1
        vnotes.append("description empty or unfilled template")
    if lines > 1000:
        v -= 1
        vnotes.append(f"{lines} changed lines (> 1000)")
    validation = max(1, min(5, v))

    # review response + reviewer rubber-stamps
    threads = b.get("threads") or []
    open_threads = [t for t in threads if not t.get("is_resolved") and not t.get("is_outdated")]
    if threads:
        review_response = 5 if not open_threads else 3 if len(open_threads) == 1 else 2
        rnote = f"{len(threads)} review threads, {len(open_threads)} left unresolved"
    elif _merged_without_approval(b):
        review_response, rnote = 2, "merged without an approval before the merge"
    elif not merged:
        review_response, rnote = 3, "not merged; no review feedback to respond to"
    else:
        review_response, rnote = 4, "approved by someone else; no comments to address"
    reviewers = []
    for r in b.get("reviews") or []:
        if r.get("state") == "APPROVED" and r.get("reviewer") != b.get("author") and lines > 200 \
                and not (r.get("body") or "").strip() \
                and not any(c.get("author") == r.get("reviewer") for t in threads for c in t.get("comments") or []):
            reviewers.append({"login": r["reviewer"], "score": 2, "missed": [],
                              "notes": f"approved {lines} changed lines with no comments (rubber-stamp; rule-based)"})

    # shared PRs: each committer is scored on their own commits
    contributors = []
    if b.get("shared_pr"):
        for person in b.get("people") or []:
            shas = set(person.get("shas") or [])           # includes commits they co-authored
            mine = [s for c, s, _ in scored if c.get("sha") in shas or c.get("author") == person["login"]]
            if person.get("commits"):
                contributors.append({"login": person["login"],
                                     "commit_msg": round(sum(mine) / len(mine)) if mine else commit_msg,
                                     "code_quality": code_quality, "notes": "rule-based: code quality not attributable per commit"})

    if bad:
        points.append("Commit messages: " + "; ".join(bad))
    points += [f"Validation: {n}" for n in vnotes]
    points += [f"{i['severity']}: {i['issue']} ({i['file']})" for i in issues if i["severity"] in ("blocker", "major")]
    if not points:
        points.append("No rule-based problems found; a human or LLM review is still needed for logic and design.")
    return {
        "engine": "heuristic",
        "repo": b["repo"], "number": b["number"], "head_sha": b["head_sha"],
        "scores": {"commit_msg": commit_msg, "code_quality": code_quality, "validation": validation,
                   "review_response": review_response},
        "tests_added": tests_added,
        "commit_notes": "; ".join(bad) or "commit subjects follow the format",
        "contributors": contributors,
        "code_issues": issues,
        "validation_notes": "; ".join(vnotes) or "CI green, tests changed, description present",
        "threads": [], "reviewers": reviewers, "rework": [],
        "summary": ("Rule-based estimate (no LLM): code quality reflects only pattern checks "
                    f"(secrets, risky APIs, debug code, scope), not a reading of the logic. {rnote.capitalize()}."),
        "talking_points": points[:8],
    }


# ------------------------------------------------------------------ Anthropic API (LLM, no Claude Code needed)
DEFAULT_MODEL = "claude-opus-5-5"
MAX_LLM_DIFF_CHARS = 600_000          # ~150k tokens; larger diffs are cut and the model is told so

_INT15 = {"type": "integer", "enum": [1, 2, 3, 4, 5]}
_NSTR = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


EVAL_SCHEMA = _obj({
    "scores": _obj({"commit_msg": _INT15, "code_quality": _INT15, "validation": _INT15, "review_response": _INT15}),
    "tests_added": {"type": "boolean"},
    "commit_notes": {"type": "string"},
    "contributors": {"type": "array", "items": _obj({"login": {"type": "string"}, "commit_msg": _INT15,
                                                       "code_quality": _INT15, "notes": {"type": "string"}})},
    "code_issues": {"type": "array", "items": _obj({
        "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
        "file": {"type": "string"}, "line": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "issue": {"type": "string"}, "author": _NSTR, "commit_sha": _NSTR, "caught_by_reviewer": {"type": "boolean"}})},
    "validation_notes": {"type": "string"},
    "threads": {"type": "array", "items": _obj({
        "comment_id": {"type": "string"},
        "validity": {"type": "string", "enum": ["valid", "nitpick", "invalid"]},
        "resolution": {"type": "string", "enum": ["fixed_in_code", "explained", "dismissed", "open"]},
        "fix_sha": _NSTR, "note": {"type": "string"}})},
    "reviewers": {"type": "array", "items": _obj({"login": {"type": "string"}, "score": _INT15,
                                                    "missed": {"type": "array", "items": {"type": "string"}},
                                                    "notes": {"type": "string"}})},
    "rework": {"type": "array", "items": _obj({
        "original_pr": {"type": "integer"},
        "kind": {"type": "string", "enum": ["self_fix", "fixed_by_other", "revert"]},
        "confirmed": {"type": "boolean"}, "evidence": {"type": "string"}})},
    "summary": {"type": "string"},
    "talking_points": {"type": "array", "items": {"type": "string"}},
})

SYSTEM_INTRO = """You are an independent senior code reviewer auditing one pull request for an engineering-quality audit.
You receive a JSON bundle: PR metadata, CI status, commits, reviews, review threads, conversation, rework candidates and the diff.
Score it strictly against the rubric below and return only the JSON object the output schema describes.

Rules the importer depends on:
- One `threads` entry per bundle thread, comment_id copied exactly. Reviewer `login` exactly as in the bundle.
- Confirm or reject every rework candidate from the diffs; record confirmed ones on this (fixing) PR.
- shared_pr true: a `contributors` entry for every human in `people` with commits (AI co-authors such as claude,
  cursoragent, copilot are bots - never list them); put author + commit_sha on each code issue.
- CI: the bundle has only the overall status, not which check failed - say so, don't guess.
- Open, never-reviewed PR -> review_response 3. Merged with no approval before the merge -> 2.
- Every score below 4 cites concrete evidence (file:line, commit SHA or comment). Talking points are kind and specific.

RUBRIC (STANDARDS.md):
"""


def llm_eval(b, diff, model=None, effort=None, client=None):
    """Score one bundle with the Anthropic API. Raises RuntimeError on refusal/truncation; SDK errors propagate."""
    import anthropic                                   # optional dependency: pip install -r requirements-llm.txt
    client = client or anthropic.Anthropic(max_retries=4)
    model = model or os.environ.get("AUDIT_MODEL", DEFAULT_MODEL)
    bundle = dict(b)
    bundle.pop("instructions", None)
    if len(diff) > MAX_LLM_DIFF_CHARS:
        bundle["diff"] = diff[:MAX_LLM_DIFF_CHARS]
        bundle["diff_note"] = (f"Diff is {len(diff)} chars; only the first {MAX_LLM_DIFF_CHARS} are included. "
                               "Say so in the summary and judge what is visible.")
    else:
        bundle["diff"] = diff
    bundle["diff_truncated"] = len(diff) > MAX_LLM_DIFF_CHARS
    system = [{"type": "text", "text": SYSTEM_INTRO + (HERE / "STANDARDS.md").read_text(encoding="utf-8"),
               "cache_control": {"type": "ephemeral"}}]           # rubric is identical for every PR -> cached
    params = dict(
        model=model, max_tokens=64000, system=system,
        messages=[{"role": "user", "content": "PR bundle:\n" + json.dumps(bundle, ensure_ascii=False)}],
        output_config={"effort": effort or os.environ.get("AUDIT_EFFORT", "high"),
                       "format": {"type": "json_schema", "schema": EVAL_SCHEMA}},
    )
    if os.environ.get("AUDIT_FALLBACKS", "1") != "0":
        # if a safety classifier declines (e.g. a security-heavy diff), the API retries on a fallback model
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params) as s:
            msg = s.get_final_message()
    else:
        with client.messages.stream(**params) as s:
            msg = s.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(msg.stop_details, 'category', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("output hit max_tokens")
    text = next(blk.text for blk in msg.content if blk.type == "text")
    ev = json.loads(text)
    ev.update({"engine": "anthropic-api", "repo": b["repo"], "number": b["number"], "head_sha": b["head_sha"]})
    usage = msg.usage
    ev["_usage"] = {"input": usage.input_tokens, "output": usage.output_tokens,
                    "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0}
    return ev
