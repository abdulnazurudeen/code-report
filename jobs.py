"""Background jobs started from the UI: collect from GitHub (plain code, no LLM), then score (LLM + our skill).

One worker thread runs one job at a time (SQLite has a single writer). Jobs live in the `jobs` table, so a restart
resumes the queue; a running job that was interrupted starts again, which is safe because collecting is idempotent
and scoring only touches PRs that still have no score. Batch jobs keep their batch ids in `jobs.state` and pick the
batch up again instead of paying twice.

Job kinds
  refresh  params: repos [owner/name] (default: tracked repos), months | since+until, collect (true),
           score "llm" | "heuristic" | "none", replace_heuristic, rescore_old_skill, max_prs, max_usd, batch
  try      params: repo, number, route {model, effort, agent}, draft {skill files}, compare_active
           scores one PR with the (optionally edited) skill WITHOUT saving it, to fine-tune the skill
"""
import concurrent.futures as cf, json, os, threading, time, traceback
from types import SimpleNamespace
import store, control, routing, skill, scoring

POLL_SECONDS = 2
BATCH_POLL_SECONDS = 20
SCHEDULE_CHECK_SECONDS = 60


# ------------------------------------------------------------------ helpers
def _github():
    import github
    return github.GitHub(github.load_token())


def _window(params):
    import audit
    a = SimpleNamespace(since=params.get("since"), until=params.get("until"), months=int(params.get("months") or 3))
    return audit.window(a)


def shared_pr(db, pr_id):
    people = store.pr_people(db, pr_id)
    return sum(1 for d in people if d["commits"]) > 1 or any(d["commits"] and not d["is_owner"] for d in people)


def effective_schedule(db):
    """The automatic-refresh settings in force: what an admin saved in the UI, else what the environment asks for.
      AUTO_REFRESH_HOURS=6   switch it on (needs GITHUB_TOKEN); AUTO_REFRESH_MONTHS=1 how far back each run looks
      AUTO_SCORE=none|heuristic|llm   what to do after collecting (default none: data only, no cost)
      AUTO_MAX_USD=5         spend cap per run when scoring with the LLM"""
    saved = control.get_setting(db, "schedule")
    if saved is not None:
        return {**saved, "source": "saved"}
    try:
        hours = float(os.environ.get("AUTO_REFRESH_HOURS") or 0)
        if hours > 0:
            return {"enabled": True, "every_hours": hours, "months": int(os.environ.get("AUTO_REFRESH_MONTHS") or 1),
                    "score": os.environ.get("AUTO_SCORE", "none"), "max_usd": float(os.environ.get("AUTO_MAX_USD") or 5),
                    "all_org": True, "source": "environment"}
    except ValueError:
        pass
    return {"enabled": False, "source": "none"}


def _repo_filter(params, db):
    repos = params.get("repos")
    if not repos:
        repos = [r["full_name"] for r in control.list_tracked(db) if r["enabled"]]
    bad = [r for r in repos if not control.REPO_RE.match(r)]
    if bad:
        raise ValueError(f"not repo names (owner/name): {bad}")
    return repos


def candidates(db, params, files, skill_hash):
    """PRs a scoring run would touch, honouring the window, repo list, max_prs and the re-score options."""
    since, until = _window(params)
    mode = params.get("score", "llm")
    repos = _repo_filter(params, db)
    store.backfill_contributions(db)
    todo = store.prs_needing_evaluation(
        db, since, until, repos,
        replace_heuristic=bool(params.get("replace_heuristic", mode == "llm")),
        skill_hash=skill_hash if params.get("rescore_old_skill") and mode == "llm" else None)
    if params.get("max_prs"):
        todo = todo[: int(params["max_prs"])]
    return todo


def estimate(db, params):
    """What a run would score and roughly cost, from the database alone (no GitHub or API calls)."""
    files, h, source = skill.active(db)
    rules = routing.load_rules(db)
    agents = skill.agents(files)
    todo = candidates(db, params, files, h)
    batch = bool(params.get("batch"))
    by_model, total, n_llm = {}, 0.0, 0
    for row in todo:
        f = {"lines": (row["additions"] or 0) + (row["deletions"] or 0), "files": row["changed_files"] or 0,
             "shared": shared_pr(db, row["id"]), "repo": row["full_name"], "state": None, "prior_engine": "none"}
        p = db.execute("SELECT files, state FROM pull_requests WHERE id=?", (row["id"],)).fetchone()
        f["paths"] = json.loads(p["files"] or "[]")
        f["state"] = p["state"]
        e = db.execute("SELECT engine FROM pr_evaluations WHERE pr_id=? AND head_sha=? AND rubric_version=?",
                       (row["id"], row["head_sha"], store.RUBRIC_VERSION)).fetchone()
        f["prior_engine"] = (e[0] or "claude-code") if e else "none"
        f["truncated"] = f["lines"] * 70 > routing.MAX_BUNDLE_DIFF_CHARS
        _, rule = routing.choose(rules, f)
        a = agents[rule["agent"]]
        overhead = 3000 + 400 * db.execute("SELECT count(*) FROM commits WHERE pr_id=?", (row["id"],)).fetchone()[0] \
            + (db.execute("SELECT COALESCE(sum(length(body)),0) FROM comments WHERE pr_id=?", (row["id"],)).fetchone()[0] or 0)
        in_chars = min(f["lines"] * 70, a["max_diff_chars"]) + overhead
        # batch mode cannot run the investigating (agentic) agent: those PRs stay online at full price
        est = routing.estimate_pr(rule["model"], rule["effort"], a["mode"], in_chars, batch and a["mode"] == "single")
        d = by_model.setdefault(rule["model"], {"prs": 0, "usd": 0.0, "rules": {}})
        d["prs"] += 1
        d["usd"] = round(d["usd"] + (est["usd"] or 0), 4)
        d["rules"][rule["name"]] = d["rules"].get(rule["name"], 0) + 1
        total += est["usd"] or 0
        n_llm += 1
    mode = params.get("score", "llm")
    return {"prs": len(todo), "mode": mode, "skill_hash": h, "skill_source": source, "batch": batch,
            "usd_estimate": round(total, 2) if mode == "llm" else 0.0, "by_model": by_model if mode == "llm" else {},
            "note": "Estimate from PR sizes; real cost varies (reasoning effort, review threads). Prices are in skills/pr-scoring/models.json."}


class Ctx:
    """What a running job can do: log, report progress and cost, check for a cancel request."""
    def __init__(self, db, job):
        self.db, self.id, self.job = db, job["id"], job
        self.params, self.state = job["params"] or {}, job["state"] or {}
        self.done = self.total = 0
        self.usage = {"input": 0, "output": 0}
        self.cost = 0.0
        self.t_flush = 0

    def log(self, msg, level="info"):
        control.job_event(self.db, self.id, msg, level)
        self.db.commit()

    def cancelled(self):
        r = self.db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,)).fetchone()
        return bool(r and r[0])

    def progress(self, done=None, total=None, force=False):
        self.done = self.done if done is None else done
        self.total = self.total if total is None else total
        if force or time.time() - self.t_flush > 1:
            self.flush()

    def add(self, ev):
        u = ev.get("usage") or {}
        self.usage["input"] += (u.get("input") or 0) + (u.get("cache_read") or 0) + (u.get("cache_write") or 0)
        self.usage["output"] += u.get("output") or 0
        self.cost = round(self.cost + (ev.get("cost_usd") or 0), 4)

    def save_state(self, **kw):
        self.state.update(kw)
        control.update_job(self.db, self.id, state=self.state)
        self.db.commit()

    def flush(self):
        control.update_job(self.db, self.id, progress_done=self.done, progress_total=self.total,
                           input_tokens=self.usage["input"], output_tokens=self.usage["output"], cost_usd=self.cost)
        self.db.commit()
        self.t_flush = time.time()


# ------------------------------------------------------------------ refresh = collect + score
def run_refresh(ctx):
    import audit
    db, params = ctx.db, ctx.params
    since, until = _window(params)
    if not params.get("repos") and params.get("org"):
        # "every active repo of the organisation": discover them now (read-only GitHub call)
        params["repos"] = audit.active_repos(_github(), params["org"], since)
        ctx.log(f"{len(params['repos'])} repo(s) in {params['org']} had activity since {since}")
    repos = _repo_filter(params, db)
    if not repos:
        raise ValueError("no repos: pick repos on the Run page, track some, or set GITHUB_ORG")
    mode = params.get("score", "llm")
    summary = {"window": [since, until], "repos": repos, "collect": {}, "failed_repos": {}, "saved": 0, "failed": []}
    if mode == "llm" and not scoring.api_key_present():
        raise ValueError("ANTHROPIC_API_KEY is not set on the server (needed to score with the LLM)")
    if params.get("collect", True) and not ctx.state.get("collected"):
        gh = _github()
        for i, name in enumerate(repos, 1):
            if ctx.cancelled():
                return "cancelled", summary
            ctx.log(f"collecting {name} ({i}/{len(repos)})")
            try:
                summary["collect"][name] = audit.collect(db, gh, name, since, until)
                db.execute("UPDATE tracked_repos SET last_collected_at=? WHERE full_name=?", (store.now(), name))
                db.commit()
                ctx.log(f"{name}: {summary['collect'][name]}")
            except (Exception, SystemExit) as e:             # GitHub helpers exit() on API errors; one repo must not stop the run
                db.rollback()
                summary["failed_repos"][name] = str(e)[:300]
                ctx.log(f"{name}: collect failed: {e}", "error")
        store.log_run(db, "collect", repos, since, until, summary["collect"])
        db.commit()
        ctx.save_state(collected=True)
    if mode == "none":
        return "done", summary
    return score_step(ctx, summary, mode, since, until)


def _prepare(ctx, gh, row, rules, files, agents_):
    import audit
    diff = gh.pr_diff(row["full_name"], row["number"])
    bundle = audit.make_bundle(ctx.db, row["id"], diff, gh)
    e = ctx.db.execute("SELECT engine FROM pr_evaluations WHERE pr_id=? AND head_sha=?", (row["id"], row["head_sha"])).fetchone()
    bundle["prior_engine"] = (e[0] or "claude-code") if e else "none"
    f = routing.features(bundle, diff)
    idx, rule = routing.choose(rules, f)
    return bundle, diff, {"model": rule["model"], "effort": rule["effort"], "agent": rule["agent"]}, rule["name"]


def _save(ctx, summary, label, ev, how):
    ctx.add(ev)
    ok, msg = store.save_evaluation(ctx.db, ev)
    if ok:
        summary["saved"] += 1
        ctx.db.commit()
        s = ev["scores"]
        ctx.log(f"scored {label}: {store.overall(s)} ({how}{', $%.3f' % ev['cost_usd'] if ev.get('cost_usd') else ''})")
    else:
        summary["failed"].append({"pr": label, "error": msg})
        ctx.log(f"{label}: {msg}", "warn")
    return ok


def score_step(ctx, summary, mode, since, until):
    db, params = ctx.db, ctx.params
    files, skill_hash, source = skill.active(db)
    agents_, rules = skill.agents(files), routing.load_rules(db)
    todo = candidates(db, params, files, skill_hash)
    ctx.progress(0, len(todo), force=True)
    ctx.log(f"{len(todo)} PR(s) to score with {mode}" + (f" (skill {skill_hash}, {source})" if mode == "llm" else ""))
    if not todo:
        return "done", summary
    max_usd = float(params["max_usd"]) if params.get("max_usd") else None
    gh = _github()
    if mode == "heuristic":
        for row in todo:
            if ctx.cancelled():
                return "cancelled", summary
            try:
                diff = gh.pr_diff(row["full_name"], row["number"])
                import audit
                ev = scoring.heuristic_eval(audit.make_bundle(db, row["id"], diff, gh), diff)
                _save(ctx, summary, f"{row['full_name']}#{row['number']}", ev, "rule-based")
            except (Exception, SystemExit) as e:
                summary["failed"].append({"pr": f"{row['full_name']}#{row['number']}", "error": str(e)[:300]})
            ctx.progress(ctx.done + 1)
        return "done", summary
    client = scoring._client()
    if params.get("batch"):
        return score_batch(ctx, summary, todo, gh, client, rules, files, skill_hash, agents_, max_usd)
    return score_online(ctx, summary, todo, gh, client, rules, files, skill_hash, max_usd)


def score_online(ctx, summary, todo, gh, client, rules, files, skill_hash, max_usd):
    conc = max(1, min(8, int(os.environ.get("AUDIT_CONCURRENCY", 3))))
    pending, stop_reason = {}, None

    def drain(block):
        nonlocal pending
        done, _ = cf.wait(list(pending), timeout=None if block else 0, return_when=cf.FIRST_COMPLETED)
        for fut in done:
            label, how = pending.pop(fut)
            try:
                _save(ctx, summary, label, fut.result(), how)
            except Exception as e:
                summary["failed"].append({"pr": label, "error": f"{type(e).__name__}: {e}"[:300]})
                ctx.log(f"{label}: {type(e).__name__}: {e}", "warn")
            ctx.progress(ctx.done + 1)

    with cf.ThreadPoolExecutor(max_workers=conc) as pool:
        for row in todo:
            if ctx.cancelled():
                stop_reason = "cancelled"
                break
            if max_usd is not None and ctx.cost >= max_usd:
                stop_reason = f"stopped at the ${max_usd:.2f} cost cap"
                break
            while len(pending) >= conc:
                drain(True)
            label = f"{row['full_name']}#{row['number']}"
            try:
                bundle, diff, route, rule_name = _prepare(ctx, gh, row, rules, files, None)
            except (Exception, SystemExit) as e:
                summary["failed"].append({"pr": label, "error": str(e)[:300]})
                ctx.log(f"{label}: could not prepare: {e}", "warn")
                ctx.progress(ctx.done + 1)
                continue
            fut = pool.submit(scoring.score_pr, bundle, diff, route, files, skill_hash, client)
            pending[fut] = (label, f"{route['model']}/{route['agent']} via “{rule_name}”")
            drain(False)
        while pending:
            drain(True)
    if stop_reason:
        ctx.log(stop_reason, "warn")
        summary["stopped"] = stop_reason
    ctx.progress(force=True)
    return ("cancelled" if stop_reason == "cancelled" else "done"), summary


def score_batch(ctx, summary, todo, gh, client, rules, files, skill_hash, agents_, max_usd):
    """Half-price asynchronous scoring. Agentic PRs can't be batched; they are scored online first."""
    state = ctx.state
    if not state.get("batches"):
        items, metas, online = [], {}, []
        for row in todo:
            if ctx.cancelled():
                return "cancelled", summary
            label = f"{row['full_name']}#{row['number']}"
            try:
                bundle, diff, route, rule_name = _prepare(ctx, gh, row, rules, files, agents_)
            except (Exception, SystemExit) as e:
                summary["failed"].append({"pr": label, "error": str(e)[:300]})
                continue
            if agents_[route["agent"]]["mode"] == "agentic":
                online.append(row)
                continue
            cid = f"pr{row['id']}"
            items.append({"custom_id": cid, "params": scoring.build_params(bundle, diff, route, files, scoring.BATCH_MAX_TOKENS)})
            metas[cid] = {"repo": bundle["repo"], "number": bundle["number"], "head_sha": bundle["head_sha"], "route": route, "label": label}
        if online:
            ctx.log(f"{len(online)} PR(s) need the investigating agent: scoring them online (not batchable)")
            sub = score_online(ctx, summary, online, gh, client, rules, files, skill_hash, max_usd)
            if sub[0] == "cancelled":
                return sub
        if items:
            ids = scoring.batch_submit(client, items)
            ctx.save_state(batches=ids, metas=metas, batch_total=len(items))
            ctx.log(f"submitted {len(items)} request(s) as batch {', '.join(ids)}; results usually arrive within an hour")
        else:
            return "done", summary
    ids, metas = state["batches"], state["metas"]
    if ctx.total != len(todo) or ctx.done == 0 and ctx.total > len(metas):
        ctx.progress(0, len(metas), force=True)          # resumed after a restart: only the batched PRs are left
    cancel_sent = False
    while True:
        if ctx.cancelled() and not cancel_sent:
            for b in ids:
                try:
                    scoring.batch_cancel(client, b)
                except Exception as e:
                    ctx.log(f"cancel {b}: {e}", "warn")
            cancel_sent = True
            ctx.log("cancel requested; collecting what already finished")
        statuses = [scoring.batch_status(client, b) for b in ids]
        if all(s[0] == "ended" for s in statuses):
            break
        ctx.log("batch: " + ", ".join(f"{s[1].get('succeeded', 0)} done, {s[1].get('processing', 0)} waiting, {s[1].get('errored', 0)} failed" for s in statuses))
        time.sleep(BATCH_POLL_SECONDS)
    for b in ids:
        for cid, ev, err in scoring.batch_results(client, b, {k: {"bundle": v, "route": v["route"]} for k, v in metas.items()}, skill_hash):
            label = metas[cid]["label"]
            if ev is None:
                summary["failed"].append({"pr": label, "error": err})
                ctx.log(f"{label}: {err}", "warn")
            else:
                _save(ctx, summary, label, ev, f"{ev['model']}/{ev['agent']} batch")
            ctx.progress(ctx.done + 1)
    ctx.progress(force=True)
    return ("cancelled" if ctx.cancelled() else "done"), summary


# ------------------------------------------------------------------ try = score one PR, don't save
def _brief(ev):
    return {"scores": ev["scores"], "overall": store.overall(ev["scores"]), "summary": ev.get("summary"),
            "talking_points": ev.get("talking_points"), "code_issues": ev.get("code_issues"), "model": ev.get("model"),
            "agent": ev.get("agent"), "skill_hash": ev.get("skill_hash"), "usage": ev.get("usage"), "cost_usd": ev.get("cost_usd")}


def run_try(ctx):
    import audit
    db, p = ctx.db, ctx.params
    if not scoring.api_key_present():
        raise ValueError("ANTHROPIC_API_KEY is not set on the server")
    row = db.execute("SELECT p.id, p.head_sha, r.full_name, p.number FROM pull_requests p JOIN repos r ON r.id=p.repo_id "
                     "WHERE r.full_name=? AND p.number=?", (p.get("repo"), p.get("number"))).fetchone()
    if not row:
        raise ValueError("that PR is not collected yet; collect its repo first")
    active_files, active_hash, _ = skill.active(db)
    gh = _github()
    ctx.log(f"fetching {row['full_name']}#{row['number']}")
    diff = gh.pr_diff(row["full_name"], row["number"])
    bundle = audit.make_bundle(db, row["id"], diff, gh)
    rules = routing.load_rules(db)
    route = p.get("route") or {}
    if not (route.get("model") and route.get("effort") and route.get("agent")):
        _, rule = routing.choose(rules, routing.features(bundle, diff))
        route = {"model": rule["model"], "effort": rule["effort"], "agent": rule["agent"]}
    client = scoring._client()
    out = {"route": route, "results": {}}
    runs = []
    if p.get("draft"):
        skill.validate(p["draft"])
        runs.append(("draft", p["draft"], skill.version_hash(p["draft"])))
        if p.get("compare_active", True):
            runs.append(("active", active_files, active_hash))
    else:
        runs.append(("active", active_files, active_hash))
    for name, files, h in runs:
        if route["agent"] not in skill.agents(files):
            out["results"][name] = {"error": f"agent {route['agent']!r} does not exist in this version"}
            continue
        ctx.log(f"scoring with the {name} skill ({h}) on {route['model']}")
        ev = scoring.score_pr(bundle, diff, route, files, h, client)
        ctx.add(ev)
        out["results"][name] = _brief(ev)
        ctx.flush()
    cur = db.execute("SELECT commit_msg, code_quality, validation, review_response, overall, engine, model, skill_hash "
                     "FROM v_pr WHERE id=?", (row["id"],)).fetchone()
    out["stored"] = dict(cur) if cur and cur["overall"] is not None else None
    return "done", out


# ------------------------------------------------------------------ the worker
HANDLERS = {"refresh": run_refresh, "try": run_try}


class JobRunner(threading.Thread):
    def __init__(self, db_path):
        super().__init__(daemon=True, name="job-runner")
        self.db_path = db_path
        self.wake = threading.Event()
        self.halt = threading.Event()
        self.last_schedule_check = 0.0

    def kick(self):
        self.wake.set()

    def stop(self):
        self.halt.set(); self.wake.set()

    def run(self):
        db = store.connect(self.db_path)
        n = db.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount
        if n:
            for r in db.execute("SELECT id FROM jobs WHERE status='queued'"):
                control.job_event(db, r[0], "server restarted: resuming this job")
        db.commit()
        while not self.halt.is_set():
            try:
                job = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if job:
                    self.execute(db, job[0])
                    continue
                if time.time() - self.last_schedule_check > SCHEDULE_CHECK_SECONDS:
                    self.last_schedule_check = time.time()
                    self.schedule(db)
            except Exception:
                traceback.print_exc()
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def schedule(self, db):
        """Automatic refresh: collect (and optionally score) on its own, so the data stays current. Runs when the
        schedule is on (saved in the UI, or switched on by AUTO_REFRESH_HOURS) and GITHUB_TOKEN is set."""
        import github
        s = effective_schedule(db)
        if not s["enabled"] or not github.token_present():
            return
        last = control.get_setting(db, "schedule_last")
        every = max(1, float(s.get("every_hours") or 24)) * 3600
        if last and time.time() - last < every:
            return
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            return
        score = s.get("score", "none")
        if score == "llm" and not scoring.api_key_present():
            score = "none"                               # never fail a scheduled run just because the key is missing
        params = {"months": s.get("months", 1), "score": score, "max_usd": s.get("max_usd"), "collect": True}
        if not any(r["enabled"] for r in control.list_tracked(db)):
            org = os.environ.get("GITHUB_ORG", "").strip()
            if not (s.get("all_org") and org):
                return
            params["org"] = org
        control.set_setting(db, "schedule_last", time.time(), "scheduler")
        control.create_job(db, "refresh", params, "scheduler")
        db.commit()

    def execute(self, db, job_id):
        control.update_job(db, job_id, status="running", started_at=store.now())
        db.commit()
        job = control.get_job(db, job_id, events=False)
        ctx = Ctx(db, job)
        status, summary, error = "failed", None, None
        try:
            status, summary = HANDLERS[job["kind"]](ctx)
        except (Exception, SystemExit) as e:
            db.rollback()
            error = f"{type(e).__name__}: {e}"[:500] if not isinstance(e, SystemExit) else str(e)[:500]
            ctx.log(error, "error")
            if not isinstance(e, (ValueError, SystemExit, PermissionError)):
                traceback.print_exc()
        ctx.flush()
        control.update_job(db, job_id, status=status, summary=summary, error=error, finished_at=store.now())
        control.job_event(db, job_id, f"{status}" + (f": {error}" if error else ""))
        db.commit()
