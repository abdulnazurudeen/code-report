"""Scoring engines that run without a Claude Code session.

  heuristic      - rule-based, no LLM, no token, free. Objective checks only (commit format, CI, tests, review
                   process, risky patterns in the diff). It cannot judge logic, so code_quality is an estimate
                   capped at 4 and every result is marked engine="heuristic" in the dashboard.
  anthropic-api  - the same review the Claude Code agents do, through the Anthropic API with ANTHROPIC_API_KEY
                   (pay per use, needs `pip install -r requirements-llm.txt`). Instructions come from the scoring skill
                   (skills/pr-scoring/); routing.py picks model / effort / agent per PR. Modes: one streamed
                   call (single), an investigating tool loop (agentic), or the Batches API (half price).

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
import skill, routing                                  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5"
MAX_LLM_DIFF_CHARS = 600_000          # ~150k tokens; larger diffs are cut and the model is told so
AGENTIC_HEAD_CHARS = 20_000           # an investigating agent starts with only the head of the diff + a file list
BATCH_MAX_TOKENS = 32_000
SINGLE_MAX_TOKENS = 64_000
AGENTIC_MAX_TOKENS = 16_000

_INT15 = {"type": "integer", "enum": [1, 2, 3, 4, 5]}
_NSTR = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


# The importer depends on this shape (store.save_evaluation), so it lives in code, not in the editable skill.
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


def api_key_present():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def sdk_installed():
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _client(client=None):
    if client is not None:
        return client
    import anthropic                                   # optional dependency: pip install -r requirements-llm.txt
    return anthropic.Anthropic(max_retries=4)


def prepare_bundle(b, diff, max_chars):
    bundle = dict(b)
    bundle.pop("instructions", None)
    cut = len(diff) > max_chars
    bundle["diff"] = diff[:max_chars]
    bundle["diff_truncated"] = cut
    if cut:
        bundle["diff_note"] = (f"Diff is {len(diff)} chars; only the first {max_chars} are included. "
                               "Say so in the summary and judge what is visible.")
    return bundle


def build_params(b, diff, route, files, max_tokens=SINGLE_MAX_TOKENS):
    """Messages API parameters for one PR (shared by the streamed call, the batch and the cost estimate).
    route = {model, effort, agent}."""
    a = skill.agents(files)[route["agent"]]
    head = AGENTIC_HEAD_CHARS if a["mode"] == "agentic" else a["max_diff_chars"]
    bundle = prepare_bundle(b, diff, head)
    if a["mode"] == "agentic":
        bundle["diff_note"] = ("Only the start of the diff is included. Use list_changed_files, read_file_diff and "
                               "search_diff to read the rest before you score.")
        bundle["changed_files"] = [{"path": p, "diff_chars": len(t)} for p, t in split_diff(diff).items()][:400]
    return dict(
        model=route["model"], max_tokens=max_tokens,
        system=[{"type": "text", "text": skill.system_prompt(files, route["agent"]),
                 "cache_control": {"type": "ephemeral"}}],          # identical for every PR -> cached
        messages=[{"role": "user", "content": "PR bundle:\n" + json.dumps(bundle, ensure_ascii=False)}],
        output_config={"effort": route["effort"], "format": {"type": "json_schema", "schema": EVAL_SCHEMA}})


def _usage(msg):
    u = msg.usage
    return {"input": getattr(u, "input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


def _add(total, usage):
    for k, v in usage.items():
        total[k] = total.get(k, 0) + v


def parse_message(msg, b, route, skill_hash, usage=None, batch=False):
    """Final assistant message -> evaluation dict for store.save_evaluation. Raises RuntimeError on refusal/cut-off."""
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(getattr(msg, 'stop_details', None), 'category', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("output hit max_tokens")
    text = next((blk.text for blk in msg.content if blk.type == "text"), None)
    if text is None:
        raise RuntimeError("model returned no text")
    ev = json.loads(text)
    usage = usage or _usage(msg)
    model = getattr(msg, "model", None) or route["model"]
    ev.update({"engine": "anthropic-api", "repo": b["repo"], "number": b["number"], "head_sha": b["head_sha"],
               "model": model, "agent": route["agent"], "skill_hash": skill_hash, "usage": usage,
               "cost_usd": routing.cost_usd(route["model"], usage, batch)})
    return ev


def _stream(client, params):
    fallbacks = routing.catalog()["models"].get(params["model"], {}).get("fallbacks") and os.environ.get("AUDIT_FALLBACKS", "1") != "0"
    if fallbacks:      # if a safety classifier declines (e.g. a security-heavy diff), the API retries on a fallback model
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params) as s:
            return s.get_final_message()
    with client.messages.stream(**params) as s:
        return s.get_final_message()


# ---- agentic mode: the model reads the diff with tools
def split_diff(diff):
    parts, cur, buf = {}, None, []
    for line in diff.splitlines(keepends=True):
        m = re.match(r"diff --git a/.*? b/(.*)$", line.rstrip("\n"))
        if m:
            if cur is not None:
                parts[cur] = "".join(buf)
            cur, buf = m.group(1), [line]
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        parts[cur] = "".join(buf)
    return parts


TOOLS = [
    {"name": "list_changed_files", "description": "List every file in the PR diff with its diff size in characters.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "read_file_diff", "description": "Read the unified diff of one file. Long diffs come in pages of 30000 characters; pass offset to continue.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
                      "required": ["path"], "additionalProperties": False}},
    {"name": "search_diff", "description": "Regex search over the added lines of the whole diff. Returns up to 40 matches as path:line: text.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"], "additionalProperties": False}},
]
PAGE = 30_000


def run_tool(name, args, files_by_path):
    """Execute one tool call. Returns (text, is_error). Inputs are validated: the model may send anything."""
    if not isinstance(args, dict):
        return "arguments must be an object", True
    if name == "list_changed_files":
        return json.dumps([{"path": p, "diff_chars": len(t)} for p, t in files_by_path.items()][:1000]), False
    if name == "read_file_diff":
        path, off = args.get("path"), args.get("offset", 0)
        if not isinstance(path, str) or not isinstance(off, int) or isinstance(off, bool) or off < 0:
            return "need path (string) and optional offset (integer >= 0)", True
        if path not in files_by_path:
            return f"no such file in the diff: {path!r}", True
        text = files_by_path[path]
        chunk = text[off:off + PAGE]
        more = f"\n[showing {off}-{off + len(chunk)} of {len(text)}; call again with offset={off + len(chunk)}]" if off + PAGE < len(text) else ""
        return chunk + more, False
    if name == "search_diff":
        pat = args.get("pattern")
        if not isinstance(pat, str) or not pat or len(pat) > 300:
            return "need a pattern (string, up to 300 characters)", True
        try:
            rx = re.compile(pat, re.I)
        except re.error as e:
            return f"bad regex: {e}", True
        hits = []
        for p, t in files_by_path.items():
            for n, line in enumerate(t.splitlines(), 1):
                if line.startswith("+") and not line.startswith("+++") and rx.search(line):
                    hits.append(f"{p}:{n}: {line[1:200]}")
                    if len(hits) >= 40:
                        return "\n".join(hits) + "\n[stopped at 40 matches]", False
        return "\n".join(hits) or "no matches", False
    return f"unknown tool {name!r}", True


def _agentic(b, diff, route, files, skill_hash, client):
    a = skill.agents(files)[route["agent"]]
    params = build_params(b, diff, route, files, max_tokens=AGENTIC_MAX_TOKENS)
    params["tools"] = TOOLS
    by_path = split_diff(diff)
    messages, total, turns = list(params.pop("messages")), {}, 0
    while True:
        msg = client.messages.create(messages=messages, **params)
        _add(total, _usage(msg))
        if msg.stop_reason != "tool_use":
            return parse_message(msg, b, route, skill_hash, usage=total)
        turns += 1
        if turns > a["max_turns"] + 2:
            raise RuntimeError("agent kept calling tools after its turn budget")
        messages.append({"role": "assistant", "content": msg.content})
        results = []
        for blk in msg.content:
            if blk.type != "tool_use":
                continue
            if turns > a["max_turns"]:
                text, err = "Investigation budget used up. Answer now with the final JSON only.", False
            else:
                text, err = run_tool(blk.name, blk.input, by_path)
            results.append({"type": "tool_result", "tool_use_id": blk.id, "content": text[:PAGE + 200], **({"is_error": True} if err else {})})
        messages.append({"role": "user", "content": results})


def score_pr(b, diff, route, files=None, skill_hash=None, client=None):
    """Score one PR with the Anthropic API. route = {model, effort, agent}; files = skill files (default: the repo's).
    Returns an evaluation dict (engine='anthropic-api') with usage and cost_usd. SDK errors propagate."""
    if files is None:
        files = skill.repo_files()
        skill_hash = skill_hash or skill.version_hash(files)
    client = _client(client)
    if skill.agents(files)[route["agent"]]["mode"] == "agentic":
        return _agentic(b, diff, route, files, skill_hash, client)
    msg = _stream(client, build_params(b, diff, route, files))
    return parse_message(msg, b, route, skill_hash)


def llm_eval(b, diff, model=None, effort=None, client=None):
    """Compatibility wrapper: score with the repo's default skill and one model."""
    return score_pr(b, diff, {"model": model or os.environ.get("AUDIT_MODEL", DEFAULT_MODEL),
                              "effort": effort or os.environ.get("AUDIT_EFFORT", "high"), "agent": "scorer"}, client=client)


# ---- Message Batches API (half price, asynchronous)
BATCH_CHUNK = 500


def batch_submit(client, items):
    """items: [{custom_id, params}] (params from build_params with max_tokens=BATCH_MAX_TOKENS). Returns batch ids."""
    ids = []
    for i in range(0, len(items), BATCH_CHUNK):
        chunk = items[i:i + BATCH_CHUNK]
        ids.append(client.messages.batches.create(requests=[{"custom_id": it["custom_id"], "params": it["params"]} for it in chunk]).id)
    return ids


def batch_status(client, batch_id):
    b = client.messages.batches.retrieve(batch_id)
    c = b.request_counts
    return b.processing_status, {k: getattr(c, k, 0) for k in ("processing", "succeeded", "errored", "canceled", "expired")}


def batch_results(client, batch_id, metas, skill_hash):
    """Yield (custom_id, evaluation | None, error | None). metas[custom_id] = {bundle, route}."""
    for r in client.messages.batches.results(batch_id):
        m = metas.get(r.custom_id)
        if m is None:
            continue
        if r.result.type != "succeeded":
            yield r.custom_id, None, f"batch result: {r.result.type}" + (f" ({getattr(r.result.error, 'type', '')})" if r.result.type == "errored" else "")
            continue
        try:
            yield r.custom_id, parse_message(r.result.message, m["bundle"], m["route"], skill_hash, batch=True), None
        except Exception as e:                                       # one bad result must not lose the rest
            yield r.custom_id, None, f"{type(e).__name__}: {e}"[:300]


def batch_cancel(client, batch_id):
    client.messages.batches.cancel(batch_id)
