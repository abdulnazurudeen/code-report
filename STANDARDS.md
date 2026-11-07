# Engineering Review Standards — rubric v1

This is the standard every PR is evaluated against. It is language-agnostic. Edit it freely; when you
change scoring rules, bump `RUBRIC_VERSION` in `store.py` so PRs are re-scored under the new rules.

Scores are 1–5 for every dimension:
**5** exemplary · **4** good, minor nits · **3** acceptable, some issues · **2** below standard, needs coaching · **1** serious problems.
Every score below 4 must cite concrete evidence (file:line, commit SHA, or comment) so it can be discussed in a 1:1.

---

## 1. Commit messages (weight 15%)

Follow **Conventional Commits**: `type(scope): summary`
Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, `revert`.

| Rule | Detail |
|---|---|
| Subject | Imperative mood ("add", not "added"), ≤ 72 chars, no trailing period |
| Meaningful | Says *what* changed. Banned as whole messages: "fix", "fix stuff", "wip", "update", "changes", "minor", "asdf", "temp" |
| Body | Explains *why* for anything non-trivial; wraps at ~72 chars |
| Traceability | References a ticket/issue (`#123`, `JIRA-45`) when one exists |
| Atomic | One logical change per commit; no "everything" commits mixing refactor + feature + formatting |
| History hygiene | No "fix review comments" / "oops" chains left unsquashed when the repo squash-merges is fine; on merge-commit repos they count against the score |

Anchors: **5** all commits conventional, clear, atomic with useful bodies · **3** mostly clear, some vague or oversized commits · **1** vague/meaningless messages throughout.

## 2. Code quality (weight 40%)

Claude reviews the diff independently, as a senior reviewer would, checking:

1. **Correctness** — logic errors, off-by-one, null/undefined handling, race conditions, wrong edge cases.
2. **Error handling** — failures caught and surfaced; no swallowed exceptions; retries bounded; resources released.
3. **Security** — no secrets/keys in code, input validated, no injection (SQL/command/XSS), authz checks present, no sensitive data in logs.
4. **Readability** — clear names, small focused functions, no dead/commented-out code, no magic numbers.
5. **Design** — follows existing patterns in the repo, no needless duplication, appropriate abstraction, no hidden coupling.
6. **Performance** — no N+1 queries, unbounded loops/memory, or blocking calls in hot paths.
7. **Config & compatibility** — no hard-coded environment values; migrations/backward compatibility handled.
8. **Scope** — PR does one thing; unrelated changes not mixed in.

Severity of each issue found: `blocker` (bug/security/data-loss risk), `major` (will cause problems or rework), `minor` (quality), `nit` (style).

Anchors: **5** no issues above nit · **4** minor only · **3** one major or several minor · **2** multiple majors · **1** any blocker merged.

## 3. Validation (weight 20%)

| Rule | Detail |
|---|---|
| Tests | New behaviour has tests; bug fixes add a regression test; changed behaviour updates tests |
| CI | CI green before merge; merging with failing/absent checks is a violation |
| Evidence | PR description says how it was tested (unit, manual steps, screenshots for UI) |
| PR description | Has *what*, *why*, *how tested*, and linked issue |
| Size | Prefer < 400 changed lines; > 1000 lines without justification is penalised (hard to review well) |

| Accuracy | Description and commit messages must match what the code actually does (re-check after follow-up commits; AI-generated descriptions included) |

Anchors: **5** solid tests + green CI + clear test notes · **3** partial tests or thin description · **1** no tests for risky change, or merged red.

*CI note:* look at **what** failed. A failing deploy-preview check caused by platform permissions (e.g. "must be a member of the Vercel team") is a setup problem, not a code failure — mention it, don't penalise the author for it. Failing build/lint/test checks do count.

## 4. Review response — how the author handled feedback (weight 25%)

For each review thread, classify:

- **Validity** — `valid` (correct and important), `nitpick` (correct, low impact), `invalid` (reviewer was wrong).
- **Resolution** — `fixed_in_code` (a later commit addresses it — cite SHA), `explained` (author gave a sound reason not to change), `dismissed` (marked resolved / ignored with no fix and no sound reason), `open` (unresolved at merge/close).

Violations: resolving a valid thread without a code change or explanation; merging with valid threads open; defensive or dismissive replies; pushing "fix" commits that don't actually fix the point raised.

Anchors: **5** every valid comment fixed or soundly explained · **3** one valid comment dismissed or left open · **1** several valid comments ignored.

When there were **no review comments**: approved by someone else → **4** (nothing to address); merged with **no review at all** (author self-merged or nobody approved) → **2**, because the author skipped the review step.

## 5. Reviewer quality (scored per reviewer, per PR — not part of the author's overall)

| Good | Bad |
|---|---|
| Comments are correct and specific, with reasons | Invalid or misleading comments |
| Blockers raised as blockers (Request changes) | Approves a PR that contains blockers Claude found ("missed") |
| Checks tests and CI before approving | Approves > 200 changed lines with zero comments ("rubber-stamp") |
| Follows up that fixes were actually made | Accepts "resolved" with no code change |
| Tone is constructive | Only style nits on a PR with real problems |

Anchors: **5** caught all majors/blockers, valid comments · **3** missed a major, or mostly nits · **1** rubber-stamped or approved a blocker.

## 6. Rework (reported, also lowers code-quality score of the original PR)

- **self_fix** — a later PR/commit by the *same author* fixes a defect introduced by their earlier PR (same files/lines, within ~30 days, often titled fix/hotfix/follow-up).
- **fixed_by_other** — someone else had to fix it.
- **revert** — the PR was reverted.

The tool proposes candidates (same files, same/other author, fix-style title, references `#N`); Claude confirms or rejects each from the diffs. Only confirmed links are counted.

## 6b. Which PRs are included

Every PR with activity in the chosen period is evaluated — **open, merged, and closed without merging** — including PRs opened earlier but merged, closed or updated with new commits inside the period. All commits of each PR are collected.
**Closed without merging:** score the code as it was submitted. Use the conversation to note why it was closed (superseded, abandoned, wrong approach). Don't penalise closing obsolete work; do flag repeated abandoned PRs or work redone elsewhere.

## 6c. Shared PRs — who answers for what

When commits from more than one person land in a PR (another developer pushes to the branch, or a commit has
a `Co-authored-by:` trailer), each person answers for their own work:

| Who | Answers for |
|---|---|
| **Owner** (opened the PR) | Validation (description, tests, CI, size) and review response (handling feedback), plus commit messages and code of their own commits |
| **Every other committer** | Commit messages and code quality of **their own commits** only |

- Merge commits that only bring a base branch in (`Merge branch 'dev' into …`) are not authored work and are ignored.
- The PR-level scores still describe the PR as a whole. Add a `contributors` entry for **every** person in the
  bundle's `people` list who has commits, the owner included, scoring only that person's commits (use each
  commit's `files` to see who changed what). The importer rejects a shared-PR evaluation that leaves anyone out.
- Put `author` (login) and `commit_sha` on each code issue so it is charged to the person whose commit introduced it.
- A person's score for the PR is their weighted share: owner = all four dimensions; other committers =
  `(0.15·commit_msg + 0.40·code_quality) / 0.55`.

## 7. Branch promotions

PRs whose source is a long-lived branch (e.g. `dev-uae → uat`, `dev-uae → prod`) carry work already reviewed in its own PRs. The tool marks them `promotion`, lists them, and does not score them or use them for rework detection.

## 8. Direct pushes

Commits landing on the default branch without a PR are listed per user. They are not scored but are a process violation unless the repo explicitly allows it.

---

## Overall score

`overall = 0.15·commit_msg + 0.40·code_quality + 0.20·validation + 0.25·review_response` (computed by the tool, not by the model, so it is consistent).

## Evaluation output (one JSON file per PR)

```json
{
  "repo": "owner/name", "number": 123, "head_sha": "<sha from bundle>",
  "scores": {"commit_msg": 1-5, "code_quality": 1-5, "validation": 1-5, "review_response": 1-5},
  "tests_added": true,
  "commit_notes": "short evidence for commit score",
  "contributors": [{"login": "...", "commit_msg": 1-5, "code_quality": 1-5, "notes": "their commits, in one line"}],
  "code_issues": [{"severity": "blocker|major|minor|nit", "file": "path", "line": 0, "issue": "...", "author": "login", "commit_sha": "...", "caught_by_reviewer": false}],
  "validation_notes": "...",
  "threads": [{"comment_id": 0, "validity": "valid|nitpick|invalid", "resolution": "fixed_in_code|explained|dismissed|open", "fix_sha": null, "note": "..."}],
  "reviewers": [{"login": "...", "score": 1-5, "missed": ["..."], "notes": "..."}],
  "rework": [{"original_pr": 0, "kind": "self_fix|fixed_by_other|revert", "confirmed": true, "evidence": "..."}],
  "summary": "2-3 sentences on how the PR and its review went",
  "talking_points": ["specific, kind, actionable points for the 1:1"]
}
```

Tone for talking points: factual, specific, and coaching-oriented — what happened, why it matters, what to do next time. Mention what was done well, too.
