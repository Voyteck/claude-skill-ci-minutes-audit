# ci-minutes-audit

A Claude Code skill (and two plain Python scripts) that shows where a repository's GitHub
Actions minutes go, and ranks the changes that would save the most, each sized from the
repository's own runs and each with a way to check that it worked.

## What it does

1. **Collects** runs and jobs for a time window with repo-scoped REST calls
   (`gh api repos/{owner}/{repo}/actions/runs?created=…`, `…/runs/{id}/jobs`). It also
   collects each push's changed paths and the pull requests opened in the window. It does
   not use the search API or GraphQL, which are often blocked or rate-limited. Everything
   goes into a local cache, so you can re-run the analysis without any API calls.
2. **Bills every job the way GitHub does.** Each job is rounded UP to a whole minute and
   multiplied by its runner's rate: Linux 1, Windows 2, macOS 10, self-hosted 0.
3. **Breaks the minutes down** by:
   - workflow and job;
   - event;
   - conclusion;
   - branch (default vs feature);
   - change type, from a configurable path-to-component map: docs-only, one component,
     several/shared;
   - the steps inside the most expensive job.
4. **Reports:**
   - minutes per opened PR;
   - run duration (median and p90);
   - the waste share (failed, cancelled and re-attempted minutes);
   - the rounding overhead;
   - with your plan's numbers, the month's burn-down and a projection: when the included
     minutes run out, and when the spending cap is hit.
5. **Proposes reductions** from a catalogue of techniques
   ([`skills/ci-minutes-audit/references/techniques.md`](skills/ci-minutes-audit/references/techniques.md)), ranked by expected minutes
   saved. Each technique comes with its pitfall, a guard test and what to measure before
   and after.

## Why

On a private repository, Actions minutes run out quietly. When the spending cap is reached,
GitHub stops starting jobs, so CI, deploys and every open PR freeze at once. The usual
reaction is to guess: drop a test job, cache something. This tool measures instead.

The other reason is safety. The biggest savings come from running fewer tests, and a
test that did not run proves nothing. Every technique in the catalogue states what
proves the skipped work, and which test keeps that proof true. One of these lessons
was learned the hard way: a narrowed run was once accepted as proof before a deploy.

## Install

**As a Claude Code plugin** (updates with the repository):

```text
/plugin marketplace add Voyteck/claude-skill-ci-minutes-audit
/plugin install ci-minutes-audit@voyteck-ci-minutes-audit
```

**As a plain skill**: copy `skills/ci-minutes-audit/` into your personal skills or one project's:

```bash
git clone https://github.com/Voyteck/claude-skill-ci-minutes-audit
cp -r claude-skill-ci-minutes-audit/skills/ci-minutes-audit ~/.claude/skills/                # every project on this machine
cp -r claude-skill-ci-minutes-audit/skills/ci-minutes-audit /path/to/repo/.claude/skills/    # one project, shared with its team
```

**On claude.ai** (Settings → Capabilities → Skills): upload a zip with the skill folder at
its root:

```bash
cd claude-skill-ci-minutes-audit/skills && zip -r ../ci-minutes-audit.zip ci-minutes-audit
```

Claude Code picks the skill up at the next session start.

The scripts also work without Claude. They need:

- Python 3.11 or later (standard library only);
- an authenticated [`gh`](https://cli.github.com/) for collecting.

```bash
python3 skills/ci-minutes-audit/scripts/collect.py --days 14                       # repo from the origin remote
python3 skills/ci-minutes-audit/scripts/analyse.py --plan-minutes 3000 --cap-usd 50 --used-minutes 1840
python3 skills/ci-minutes-audit/scripts/analyse.py --components components.json --json summary.json
python3 skills/ci-minutes-audit/scripts/collect.py --self-test && python3 skills/ci-minutes-audit/scripts/analyse.py --self-test
```

If `gh` is not available, save the GitHub MCP server's run and job listings as JSON. Then
load them with `collect.py --import-runs … --import-jobs … --offline` (see [`SKILL.md`](skills/ci-minutes-audit/SKILL.md)).

## Example output

From the built-in synthetic dataset, trimmed:

```markdown
Window … (2.0 days): 14 runs, 24 jobs, **65 billed min** (32/day, ~975 per 30 days).
Rounding each job up to a minute is 18% of them.

## Top cost centres (workflow / job)
|  | billed min | share | jobs |
|---|---|---|---|
| CI / unit | 38 | 58% | 8 |
| Deploy / deploy | 12 | 18% | 3 |
| CI / lint | 8 | 12% | 8 |

## By change type
|  | billed min | share | jobs |
|---|---|---|---|
| one component | 30 | 46% | 10 |
| n/a (workflow_dispatch) | 15 | 23% | 6 |
| several/shared | 12 | 18% | 4 |
| docs-only | 4 | 6% | 3 |

## Efficiency
- 5 pull requests opened: **13.0 billed min per opened PR**.
- Waste (failed, cancelled, timed-out jobs): **11%** (7 min); re-attempts billed 6 min.

## Steps of the dominant job `CI / unit`
| step | n | median s | p90 s | share of job |
|---|---|---|---|---|
| Test | 1 | 540 | 540 | 90% |

## Budget
- Used this month: 65 of 3000 included min (this repository); plan pace today 194.
- Recent rate 32 min/day; projected month end **1008 min** (29.0 days left).

## Proposed reductions
| # | technique | change | min/30 d | evidence | verify by |
|---|---|---|---|---|---|
| 1 | `narrow-tests` | Run only the tests the change needs | ~169 | one-component runs paid 25 min in `CI / unit`; its longest step `Test` is 90% of the job … | median billed minutes of the dominant job on one-component pushes …; plus one full run before every deploy |
| 2 | `identical-tree` | Skip a run whose tree already passed | ~150 | 1 runs on a tree that already had a green run | … the proof artefact names the run it borrowed from |
| 3 | `cancel-superseded` | Cancel superseded runs | ~120 | 1 runs finished after a newer run on the same branch had started | … |
```

## The techniques

Habits, which need no code:

- run the local test suite before pushing, and push once;
- use `[skip ci]` for work in progress;
- do not merge the default branch just to keep a branch fresh;
- re-run only the failed jobs;
- watch runs with `gh run watch` instead of polling them;
- batch deploys;
- make every new job state its cost.

Workflow changes:

- concurrency groups with cancel-in-progress;
- no CI on measurement branches;
- path filters for docs-only changes;
- skip a tree that already passed, and keep a proof artefact;
- fold short jobs into steps;
- run the default branch on a schedule instead of on every merge.

Test-suite changes:

- narrow the tests to what changed:
  - diff from the last green run;
  - a noise filter;
  - a map from each component to the code that depends on it, with a completeness guard;
  - run everything when shared code changes;
  - run everything when any input is missing;
- no coverage and no slow-marked tests on push runs, while the deploy gate still runs the
  full suite;
- block network access in unit tests, and shorten fixed sleeps;
- a lean harness for mutation tests.

## Built from measurements

The catalogue comes from about two months of work on one private repository
(August–October 2026). That repository is a monorepo with a few applications, one large
Python test suite, PHP and JavaScript suites, and deploys run by hand. Every figure below
is a proportion, measured from run and job timestamps (the same arithmetic as
`analyse.py`) or from local timing of the test suite.

**Where the minutes went:**

- In the first month, two end-to-end jobs used **over 40 %** of billed minutes. They ran on
  every push but were only needed before a deploy. They moved into the deploy gate.
- Later, push runs of the light suite were **about four fifths** of the minutes. Rounding
  each job up to a whole minute was **about a fifth**. Two small side jobs billed about
  **40 % more** than the work they did. Turning them into steps of the main job took
  **about a quarter** off the bill.
- When the included minutes ran out:

  | source | share of minutes |
  |---|---|
  | pushes to feature branches | ~60 % |
  | pushes to the default branch | ~10 % |
  | deploys | ~10 % |
  | measurement branches | ~5 % |
  | everything else | the rest |
- **More than half** of the default-branch push runs were cancelled by the next merge
  after they had already started. A replay of the same week tested two alternatives:
  - a schedule every few hours used **about 60 % fewer** minutes for that stream;
  - turning `cancel-in-progress` off used **about 10 % more**.
- **About one push run in six** tested a tree that had already passed.
- The main test job was the large majority of the minutes, about three quarters in a
  later two-day sample. Its test step was **about four fifths** of a push run's billed
  minutes before narrowing.

**Minutes by change type** (push minutes in a two-day sample after the other changes):

| change | share |
|---|---|
| documentation only | ~1/3 |
| no component's code (docs, CI scripts, tests and tooling only) | ~1/3 |
| several components or shared code | ~1/4 |
| exactly one component | <1/10 |

So most pushes did not need most of the suite. On a monorepo, the "no component" class
is as large as docs-only, and only the component map makes it visible.

**Expected effect of narrowing** (local timing, two workers, against the full test step
before the change):

| change | share of the full test time |
|---|---|
| no component changed | ~40 % |
| one component | 40–90 % |
| shared code | 100 % |

- Coverage added about 55 % to test time, and eight slow-marked tests were close to 40 % of
  it. A whole-tree run with neither took about half as long as the old push run's test step.
- For a typical push, about 16 billed minutes became about 6 (**roughly 60 % less**).
- Blocking network access in unit tests made one file **over 80 % faster**: it had made
  thousands of DNS lookups.
- Stopping each file at its first failure in a mutation harness, without inherited
  coverage or workers, cut that harness's time by **about three quarters**.
- On a branch's first push, diffing from the newest green default-branch ancestor
  (instead of running everything) meant optional heavy steps no longer ran on
  **about four in five** first pushes.

**After the changes** (two-day sample):

- waste (failed and cancelled jobs) was about a fifth of the minutes;
- rounding was under a tenth;
- an opened PR cost about half an hour of billed minutes.

Treat these as orders of magnitude, not promises. Run the audit on your own repository.

## Limitations

- **Your bill may be higher than this count.** The plan's minutes are per account, across
  every private repository, and this tool sees one repository. Pass the month-to-date
  figure from the billing page.
- **Some costs are not counted:**
  - larger runners are flagged in the report but counted at their OS rate;
  - storage for artefacts and caches is not counted.
- **Change types are approximate.** The runs API does not say what a push changed. The
  tool diffs against the previous run on the same branch, then against the default branch,
  then against the head commit alone.
- **Savings estimates overlap.** Each estimate ranks one technique; do not add them up.
- **Short windows are noisy.** Collect at least 14 days before deciding anything.

## License

MIT - see [LICENSE](LICENSE). Plugin name `ci-minutes-audit`, skill name `ci-minutes-audit`.
