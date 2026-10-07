---
name: ci-minutes-audit
description: Audits where a repository's GitHub Actions minutes go and proposes measured reductions. Bills every job as GitHub does (rounded up per job), breaks minutes down by workflow/job, event, conclusion, branch and change type, reports waste, minutes per PR, burn-down and month-end projection against the plan and cap, then ranks proven techniques (test narrowing with a safe deploy gate, identical-tree skips, cancel-in-progress, path filters, folding short jobs) by minutes saved, each with how to verify it. Use it whenever someone mentions Actions minutes, CI cost or bill, billable minutes, a spending cap hit, "we ran out of minutes", slow or expensive CI, or wants to cut, audit or forecast CI usage — also in Polish ("minuty Actions", "koszty CI", "obniż koszty testów", "skończyły się minuty") — even if they only ask "why is our CI so expensive?".
---

# CI minutes audit

Find where a repository's GitHub Actions minutes go, and propose the reductions that save
the most — each one sized from the repository's own runs and each with a way to verify it.

The skill is read-only towards GitHub: it reads the REST API and never pushes, re-runs or
dispatches anything. Collecting costs API rate limit, not Actions minutes.

## Files

| file | what it is |
|---|---|
| `scripts/collect.py` | fetches runs, jobs (all attempts, with steps), changed paths and opened PRs for a window into a cache (`.ci-minutes-audit/`); re-runs reuse it |
| `scripts/analyse.py` | reads the cached `dataset.json`, prints the Markdown report; no API calls |
| `references/techniques.md` | the catalogue: problem, change, pitfall, guard test and measurement per technique — read the entries the report ranks highest before proposing them |

Both scripts are stdlib Python 3.11+; `collect.py` shells out to `gh`. Both have
`--self-test` (synthetic data, no network) — run it first if you changed either script.

## Workflow

### 1. Settle the inputs

Ask (or infer) before collecting:

- **Repository** — `owner/name`; default is `GITHUB_REPOSITORY` or the `origin` remote.
- **Window** — at least 14 days for a decision; 2–7 days is fine for a first look, but the
  report warns that per-30-day figures extrapolate a small sample. Use a window that
  starts on the 1st of the month if the user wants the burn-down.
- **Plan** (optional) — the plan's included minutes per month, the spending cap in USD, the
  overage price per Linux minute, and the month-to-date usage from the billing page. The
  plan is per ACCOUNT across all private repositories, so without the billing figure the
  repository's own minutes are a lower bound. Public repositories on standard runners are
  free; say so if the repository is public.

### 2. Collect

```bash
python3 <skill>/scripts/collect.py --days 14                    # or --since/--until
python3 <skill>/scripts/collect.py --days 14 --repo owner/name --cache /tmp/ci-audit
```

It calls only repo-scoped REST endpoints (`repos/{o}/{r}/actions/runs?created=…`,
`…/runs/{id}/jobs?filter=all`, `…/compare/…`, `…/commits/{sha}`, `…/pulls`), splits a day
past the list endpoint's 1000-result cap, and caches finished days, completed runs' jobs
and diffs. It does NOT use the search API (often blocked) or GraphQL (`gh repo view` and
`gh pr view` go through GraphQL; avoid them where it is blocked). `--no-files` skips the
changed paths when the rate limit is tight.

**Without `gh`** (or when it is not authenticated), use the GitHub MCP server's Actions
tools instead (`actions_list` with `list_workflow_runs` / `list_workflow_jobs`, or the older
`list_workflow_runs` / `list_workflow_jobs` tools — whichever the server exposes). Save
each raw response as a JSON file, then:

```bash
python3 <skill>/scripts/collect.py --import-runs runs_*.json --import-jobs jobs_*.json --offline \
    --repo owner/name --since 2025-01-01 --until 2025-01-15 --no-files
```

Change types then show as `unknown` unless you also fetch commit diffs (the MCP commit and
compare tools) — still report the rest.

### 3. Map paths to components (for the change-type breakdown)

Without a map, each top-level directory is a component and root files plus `.github/` are
shared. For a better answer, look at the repository's layout and its test tree, and write a
JSON map — which directories are separately testable components, which paths every
component depends on, and which paths no test reads:

```json
{
  "components": {"api": ["^api/"], "web": ["^web/"], "worker": ["^worker/"]},
  "shared":     ["^lib/", "^\\.github/workflows/", "^(pyproject\\.toml|package-lock\\.json)$"],
  "docs":       ["(^|/)docs/", "\\.md$"],
  "ignore":     ["^scripts/dev/", "^\\.vscode/"],
  "unmatched":  "shared"
}
```

Classes: `docs-only` (documentation alone), `no component` (documentation and ignored paths),
`one component`, `several/shared`, `no change` (empty diff), `unknown`, and `n/a (<event>)`
for schedules and dispatches. A push's change is diffed from the previous run's commit on
the same branch; the first run of a branch in the window uses the branch against the
default branch, and a merged branch falls back to its head commit.

### 4. Analyse

```bash
python3 <skill>/scripts/analyse.py --dataset .ci-minutes-audit/dataset.json \
    --components components.json --plan-minutes 3000 --cap-usd 50 --used-minutes 1840 \
    --json summary.json
```

Useful knobs: `--steps-job "Workflow / job"` (default: the most expensive job),
`--deploy-workflows REGEX`, `--docs-workflows REGEX`, `--no-ci-branches 'bench/*,exp/*'`,
`--keep-matrix`, and the two estimate assumptions `--narrow-keep` (share of the test step a
narrowed push still pays, default 0.5) and `--catchable` (share of red pushes a local run
would catch, default 0.5). Change them when the user knows better, and say which you used.

### 5. Read and verify before proposing

- Open `references/techniques.md` for the top three to five ranked techniques and adapt
  each to this repository's workflows. Read the actual workflow files before claiming a
  technique is missing — the data shows symptoms, the YAML shows causes (a technique may be
  in place and simply not catching the case).
- The estimates overlap (a superseded docs-only run counts in two of them); they rank
  candidates and do NOT add up. Never present their sum as the total saving.
- Unsized techniques (`measure`) need a local measurement first: run the suite with and
  without coverage, with and without the slow marker, with `--durations`.
- Anything that removes test work must keep a proof of what was skipped, and the deploy
  gate must still run, or require, the FULL suite. The catalogue's `push-light-suite` entry
  describes the failure where a narrowed push run was accepted as deploy proof — repeat that
  warning whenever you propose `narrow-tests`, `push-light-suite` or `identical-tree`.

### 6. Report to the user

Lead with the answer, then the evidence:

1. Where the minutes go: the top three cost centres with shares, and the dominant job's
   heaviest step.
2. The waste share and the rounding share; minutes per opened PR; run duration median/p90.
3. The budget: used vs plan pace, the projection, the date the included minutes (and the
   cap) run out — with the caveat when usage is not from the billing page.
4. The ranked proposals as a table: technique, expected minutes saved per 30 days, the
   evidence, the pitfall in one line, and what to measure before and after.
5. The next step: which change to make first (habits cost nothing; test narrowing saves the
   most but ships only together with its deploy-gate guard), and when to re-run the audit
   (a week after each change, same window length).

Do not implement the changes unless asked. Do not push, re-run or dispatch workflows to
"check" something — that spends the minutes this audit is about.

## Caveats the report cannot see

- Billing is per job: start to completion, rounded up, times the OS multiplier (Linux 1,
  Windows 2, macOS 10); self-hosted runners are free. Larger runners have their own
  per-minute price — the report flags their labels but counts them at the OS multiplier.
- Storage for artefacts and caches is billed separately and is not in this audit.
- Job and run history is retained for a limited time; very old windows may come back empty.
- A run that is still in progress is counted with the jobs that have finished.
