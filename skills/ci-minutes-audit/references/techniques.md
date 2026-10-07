# Techniques that cut GitHub Actions minutes

Every entry was applied to a real repository and measured before and after. Each has the
same five parts:

- **Problem** — what the minutes were buying and why it was not worth it;
- **Change** — what to do;
- **Pitfall** — how the change went wrong, or would have, and what stops it;
- **Guard test** — a test that keeps the change honest after the next edit;
- **Measure** — what to compare before and after (the `analyse.py` figure, where one exists).

The id in each heading is the id `analyse.py` prints. Entries are grouped: habits (cost
nothing to adopt), workflow changes, and test-suite changes.

Two rules hold for all of them:

1. **A skipped check must be a PROVEN skip.** Every technique below removes work. Each one
   needs a stated reason why the removed work could not have found anything new — "nothing
   it reads changed since a green run", "the same tree already passed" — and a test that
   keeps that reason true. A skip without proof is just a hole.
2. **The deploy gate is never narrowed.** Narrow what runs on a push; never what runs right
   before code reaches a machine. See `push-light-suite`.

---

## Habits

### `local-sieve` — run the local sieve before pushing, push once

- **Problem.** A push is the unit of cost: each push to a branch runs the whole push suite.
  Pushing a fix for a red run that a local run would have caught pays the suite twice, and
  pushing after every commit pays it per commit.
- **Change.** One script that runs locally what CI runs on a push (lint, type check, the
  test suite), in the same order and with the same flags. Run it before pushing; commit as
  often as you like, push when the branch is ready for review or you need the green run.
  Do NOT run it as a git hook (it gets bypassed, and a slow hook trains people to skip it).
- **Pitfall.** The local script drifts from the workflow (a new step lands in CI only), so
  "green locally" stops predicting "green in CI". On a small machine, two full local runs at
  once can run it out of memory: serialise them.
- **Guard test.** A test that parses the workflow's steps and the local script's commands
  and fails when a CI step has no local counterpart (or lists the deliberate exceptions).
- **Measure.** Red push runs per merged PR and push runs per branch (`local-sieve` evidence:
  median and p90 pushes per branch, minutes of red attempts).

### `skip-ci-wip` — `[skip ci]` for work in progress

- **Problem.** Work that must leave the machine (a backup, a hand-over between sessions)
  is pushed long before anyone needs a CI answer on it.
- **Change.** Put `[skip ci]` (or `[ci skip]`, `[no ci]`, `[skip actions]`) in the HEAD
  commit's message; GitHub starts no `push` or `pull_request` workflow for it.
- **Pitfall.** The LAST push before review must be without it — the review and the deploy
  gate need a real green run on that exact SHA. A squash that keeps `[skip ci]` in the merged
  message skips the default branch's run too.
- **Guard test.** The deploy gate requires a green run on the deployed SHA (see
  `push-light-suite`); a `[skip ci]` SHA then cannot slip through.
- **Measure.** Runs on WIP/fixup commits per week (`skip-ci-wip`).

### `no-default-merge` — never merge the default branch just to keep a branch fresh

- **Problem.** "Merge main into my branch" creates a new tree and therefore a full push run,
  without changing anything the branch's author wrote. Measured: in the day after this rule
  was written, about twenty such merges still billed around fifteen minutes each.
- **Change.** Merge the default branch into a feature branch only on a real conflict, or when
  the person merging asks for it right before the merge. Put the rule in every contributor's
  and every agent's brief.
- **Pitfall.** A long-lived branch then runs its CI against an old base; the merge queue
  (or the person merging) must check the result on the merged tree — which the
  default branch's own run does.
- **Guard test.** None in code; it is a habit. Report the count weekly.
- **Measure.** Runs whose head commit is `Merge branch '<default>' into …` (`no-default-merge`).

### `rerun-failed-only` — re-run only the failed jobs

- **Problem.** "Re-run all jobs" after one flaky job pays every job again.
- **Change.** `gh run rerun <id> --failed` (or "Re-run failed jobs" in the UI). Do not
  re-run at all while an outage lasts: a job that failed with zero steps is the platform or
  the budget, not the code.
- **Pitfall.** Jobs that pass artefacts to each other: a re-run of a failed downstream job
  needs the upstream artefact to still exist (retention).
- **Guard test.** None; habit.
- **Measure.** Minutes in attempts > 1 spent on jobs that had passed in the previous attempt
  (`rerun-failed-only`).

### `watch-not-poll` — `gh run watch` instead of polling

- **Problem.** A "status" workflow dispatched to ask whether a detached job finished bills a
  whole minute per question; polling it in a loop bills a minute per iteration.
- **Change.** Watch a run you started with `gh run watch <id>` (free: it reads the API from
  your machine). For detached work on another machine, poll at most every 15–20 minutes and
  never in a loop.
- **Pitfall.** `gh run watch` in a foreground shell blocks an agent session; run it in the
  background and get notified on exit.
- **Guard test.** None; habit.
- **Measure.** Short (≤ 1 billed minute) dispatch runs per workflow per day (`watch-not-poll`).

### `batch-deploys` — batch deploys

- **Problem.** A deploy after every merge pays the deploy workflow — and, when the gate must
  run the full suite on a SHA that has no full green run, the full suite too — once per merge.
- **Change.** Deploy a batch of merged changes at once, at a time someone chooses. The one
  exception: a deploy that a measurement depends on goes out at once.
- **Pitfall.** A batch makes "which change broke production" harder to answer; keep batches
  small enough to bisect, and record the range each deploy shipped.
- **Guard test.** None; habit. A deploy workflow on `workflow_dispatch` only (no push
  trigger) makes it the default.
- **Measure.** Deploy runs per workflow per day and their minutes (`batch-deploys`).

### `new-job-cost` — a new job states its cost

- **Problem.** Every job added "just to check" bills forever, rounded up to a minute per run.
- **Change.** A PR that adds a job or a trigger states, in its body, the expected runs per
  day × billed minutes per run, and its trigger. A new `push` trigger needs an owner's yes.
- **Pitfall.** The estimate is written once and never compared with reality; re-run this
  audit a week later and compare.
- **Guard test.** Optional: a test that every job in the push workflow has a path condition
  or is listed as always-on with a reason.
- **Measure.** The job's row in the top cost centres, a week after it lands.

### `no-ci-branches` — measurement and tuning branches skip CI

- **Problem.** Branches that exist to be measured (benchmarks, tuning candidates,
  experiments) are judged by their measurement, not by the test suite, and nothing deploys
  from them — yet every push to them runs CI. Measured: about one minute in twenty.
- **Change.** Exclude them by name in the push trigger and put such work on those branches:

  ```yaml
  on:
    push:
      branches: ["**", "!measure/**", "!bench/**"]
  ```

- **Pitfall.** Someone puts real feature work on such a branch to "save minutes"; the merge
  to the default branch then lands untested. Keep a merge gate that requires a green run.
- **Guard test.** A test that the push trigger's negative patterns match exactly the
  documented list.
- **Measure.** Runs on branches matching the patterns (`no-ci-branches`; pass your patterns
  with `--no-ci-branches`).

---

## Workflow changes

### `cancel-superseded` — concurrency groups with cancel-in-progress

- **Problem.** Two pushes to one branch a minute apart run two full suites; the older result
  concerns code nobody will merge.
- **Change.**

  ```yaml
  concurrency:
    group: ci-${{ github.ref }}-${{ github.event_name == 'push' && 'push' || github.run_id }}
    cancel-in-progress: true
  ```

  Pushes share one group per branch, so a newer push cancels the older run. Full runs
  (dispatch, a call from the deploy gate) get a group per run, so a push never cancels them.
- **Pitfall.** On the default branch, cancelling means a burst of merges may leave no run
  finished on any intermediate SHA — fine if only the tip deploys. The opposite setting
  (`cancel-in-progress: false`, one pending) was replayed on real data and billed MORE: a
  stale run then runs to its end instead of stopping when the next push lands.
- **Guard test.** A test that the push workflow has a concurrency group keyed on the ref and
  that full-run events get a per-run group.
- **Measure.** Minutes runs spent after a newer run on the same branch had started
  (`cancel-superseded`); cancelled-run count rises, billed minutes fall.

### `debounce-default-branch` — test the default branch on a schedule, not on every merge

- **Problem.** Every merge to the default branch starts a run, and most are cancelled by the
  next merge after they have already billed a few minutes. Measured: more than half of the
  default-branch push runs were cancelled mid-run.
- **Change.** No push trigger for the default branch; a schedule (every few hours) runs the
  same light suite on the tip. A slot with no merge since the last green run costs about a
  minute (see `identical-tree`). The deploy gate runs the suite itself on a tip no slot has
  covered. Replayed on a week of real runs: about 60 % fewer minutes for this stream.
- **Pitfall.** A red default branch shows up hours later instead of minutes; a new branch cut
  in that window inherits the red. Document "red in code you did not touch: check the
  default branch's last scheduled run first".
- **Guard test.** A test that runs the scope step for a push and for a schedule on a
  throwaway repository and requires the same decisions.
- **Measure.** Default-branch runs per day and the share cancelled (`debounce-default-branch`).

### `path-filter-docs` — path filters for docs-only changes

- **Problem.** A change to documentation alone runs the whole code suite.
- **Change.** Either a `paths-ignore` filter on the trigger, or (when the workflow is a
  required check) a first step that classifies the diff and skips the heavy steps:

  ```yaml
  on:
    push:
      paths-ignore: ["**/*.md", "docs/**"]
  ```

- **Pitfall.** A REQUIRED check that never starts blocks the merge forever; use the in-job
  classification (or a stub job with the same name) for required checks. Some tests DO read
  documentation (a test that the docs list every endpoint): those belong to an always-run set
  that a docs change still triggers. A `README.md` inside a code directory is still docs.
- **Guard test.** A test that derives which test files read which non-code files (path
  literals) and fails when a read file is covered by the ignore filter but its reader is not
  always-run.
- **Measure.** Docs-only runs of code workflows and their minutes (`path-filter-docs`): after,
  zero, or the always-run set's cost.

### `identical-tree` — skip a run whose tree is identical to an already-green tree

- **Problem.** A branch push and the merge commit that lands the same content, or a re-push
  of an unchanged tree, run the same suite on byte-identical code. Measured: about one push
  run in six.
- **Change.** First step: look up a successful run of the same workflow whose
  `head_commit.tree_id` equals this run's tree; if found, skip the heavy steps and RECORD THE
  PROOF — an artefact naming the run it borrowed from and what that run covered:

  ```bash
  tree=$(git rev-parse 'HEAD^{tree}')
  gh api "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs?status=success&per_page=50" \
    -q ".workflow_runs[] | select(.head_commit.tree_id == \"$tree\") | .id"
  # download that run's proof artefact; borrow only if it says it ran the FULL suite
  ```

- **Pitfall.** Borrowing from a run that was itself narrowed (see `narrow-tests`) proves only
  what that run ran. Borrow only from a run whose proof artefact says "full", and write
  "full" into this run's artefact only when it borrowed from one. A run older than the
  artefact's retention proves nothing.
- **Guard test.** Execute the step against a fake `gh` with three candidates — a full green
  run, a narrowed green run, a run with no artefact — and assert it borrows only from the
  first and records what it borrowed.
- **Measure.** Runs on a tree that already had a green run (`identical-tree`): their billed
  minutes should drop to about one each.

### `fold-short-jobs` — fold short jobs into steps of one job

- **Problem.** GitHub bills every job rounded UP to a whole minute, plus its own runner
  set-up and checkout. Two jobs of 40 seconds bill two minutes; as steps of one job they add
  80 seconds to it. Measured: rounding was about a fifth of all billed minutes in one week,
  and two small jobs billed about 40 % more than their work. Folding them into steps of the
  main job took about a quarter off the bill.
- **Change.** Jobs that wait for the main job anyway, or that are short and independent,
  become its steps, each behind the same `if:` condition the job had.
- **Pitfall.** A failed step now stops the steps after it; order them so the cheapest,
  most-often-red ones come first, or use `if: always()` deliberately. A deploy gate that
  checks job NAMES no longer sees the folded ones: move their proof into the main job's
  result.
- **Guard test.** A test that every flag the scope step outputs is consumed by some step
  condition and every step condition names an existing flag (a typo silently disables a step).
- **Measure.** Rounding overhead, billed minus exact (`fold-short-jobs` and the report's
  "rounding" line).

### `narrow-tests` — run only the tests the change needs

- **Problem.** The test step of the push suite runs the whole test tree on every push, even
  when the change touched one component or only documentation. Measured: the test step was
  about four fifths of a push run's billed minutes.
- **Change.** Five parts; all five are needed.
  1. **Diff base = the last GREEN run on this branch**, not the previous commit: a skip then
     means "this passed green and nothing it reads changed since". On a FRESH branch with no
     green run, use the newest green default-branch run whose SHA is an ANCESTOR of this
     commit (`gh api repos/O/R/compare/<candidate>...<sha> -q .status` is `ahead` or
     `identical`); the diff from it also holds the default branch's own later changes,
     which is conservative. Diff with `--no-renames`, so a moved file counts at both paths.
  2. **A noise filter**: paths no test reads (docs, data sets, machine scripts) are dropped
     before choosing components; keep it explicit and short.
  3. **A component → dependents map**: a change in component A runs A's tests plus the tests
     of every component that reads A's files. Guard it with a test that DERIVES the reads
     (path literals and imports in every test file) and fails on a read the map does not
     cover, and on an entry the derivation no longer finds.
  4. **Shared code or configuration → run everything**: shared libraries, the test runner's
     configuration, its root `conftest`, the dependency pins, the workflow itself, or two
     components changed at once.
  5. **Missing input → run everything**: no green base found, the base commit unreachable,
     the diff failed, the change list absent. An absent list is "unknown", never "empty".
  Always run a small always-on set (workspace-wide invariants) plus the changed test files
  and the tests that name them.
- **Pitfall.** A narrowed run is no proof for what it left out. If anything downstream —
  the deploy gate, the identical-tree shortcut — treats "green" as "fully tested", it now
  trusts a partial run. Record in an artefact whether the run was full; see
  `push-light-suite`. A narrower run also has fewer passes AND fewer skips: print what was
  selected and why in the log, or the counts stop meaning anything.
- **Guard test.** (a) Table tests of the selector: docs only, one component, a dependent, a
  shared path, two components, a missing list. (b) The dependents-map completeness test
  above. (c) A test that executes the real scope step against a throwaway repository for
  a fresh branch and checks the chosen base.
- **Measure.** The dominant job's median billed minutes on one-component and docs-only
  pushes, and the collected test count per run (`narrow-tests`). Measured locally per change
  type before shipping: no component changed ≈ 40 % of the full test time, one component
  40–90 %, shared code 100 %.

### `push-light-suite` — push runs without coverage and slow-marked tests; the deploy gate runs everything

- **Problem.** Coverage instrumentation and a handful of slow tests (subprocess-heavy
  guards, large matrices) are a large share of the test step but answer questions nobody
  asks on every push. Measured: coverage added about 55 % to the test time; eight slow
  tests were about 40 % of the full suite's time on two workers.
- **Change.** On `push`: no `--cov`, and deselect a marker (`-m "not slow"`). On a schedule,
  a manual dispatch and the deploy gate: everything, with coverage. Coverage without a
  threshold gates nothing anyway.
- **Pitfall — hit for real.** The deploy gate reused "a green run of this SHA" as proof and
  skipped the tests. After push runs became narrowed, a green push run proved only a part of
  the suite, and the gate accepted it as full proof — the review caught a path to production
  where the deselected tests never ran. The fix:
  - every run writes an artefact stating whether its test step was FULL (every path, no
    marker deselected);
  - the deploy gate skips the test suite only for a reused run whose artefact says full; for
    any other green run it runs the WHOLE suite before deploying;
  - a caller that forgets the input gets the safe default (run everything).
- **Guard test.** A test that executes the gate's decision step, the scope step and the test
  step with a fake `gh`, and asserts: the artefact says full exactly when everything was
  collected; no decision deploys without either a reused full run or a full run inside the
  deploy; every deselected marker runs on every path to a deploy. Mutate the gate (drop the
  check, flip the default) and the test must go red.
- **Measure.** Test-step seconds on a push before and after (measure locally first, with and
  without `--cov`, with and without the marker); deploy-gate minutes after (they rise a
  little — that is the price of the proof).

### `fast-tests` — shorten fixed sleeps, cut hung network calls

- **Problem.** Tests that sleep a fixed time, wait for a real timeout, or resolve a host name
  that does not exist (a cache client pointed at a service host, a model client with a real
  URL) pay seconds each. Measured: one file made thousands of name lookups, some answered
  after two seconds; blocking the network took it from about 150 s to 26 s.
- **Change.**
  - A `conftest.py` for unit tests that refuses every name lookup and connection to a
    non-loopback host and FAILS the test that tried, so the cause is visible:

    ```python
    import socket, pytest

    _real_connect = socket.socket.connect
    _real_getaddrinfo = socket.getaddrinfo
    LOCAL = {"localhost", "127.0.0.1", "::1"}


    def _guard_getaddrinfo(host, *a, **k):
        if host not in LOCAL:
            pytest.fail(f"unit test resolved {host!r}: fake the client instead")
        return _real_getaddrinfo(host, *a, **k)


    def _guard_connect(self, address):
        if isinstance(address, tuple) and address[0] not in LOCAL:
            pytest.fail(f"unit test connected to {address!r}")
        return _real_connect(self, address)


    @pytest.fixture(autouse=True)
    def _no_network(monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _guard_getaddrinfo)
        monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    ```

    Unix sockets stay allowed, so a test that needs a real local service starts one on a
    socket.
  - Point service URLs at a closed loopback port, so a miss fails in microseconds.
  - Wait on an event (a file, a flag), not on a clock; where a timeout is the subject,
    shrink it in the test (seconds → tenths) and end a simulated hang as soon as the code
    under test gave up on it.
  - Print the slowest test phases in every CI log (`--durations=25`), so the run that adds a
    slow test shows it.
- **Pitfall.** A guard that silently returns an error instead of failing hides the slow
  path; fail loudly. A cost assertion in a parallel run must use CPU time
  (`time.thread_time()`), not wall-clock time.
- **Guard test.** A test that the guard is active: try to resolve an external name inside a
  unit test and expect the failure.
- **Measure.** The test step's median and the summed test time; the slowest phases list.

### `mutation-harness` — lean mutation and subprocess test harnesses

- **Problem.** A guard that runs the test suite in subprocesses (mutation checks: "break the
  code, the tests must go red") inherits the outer run's coverage, parallel workers and
  `PYTEST_ADDOPTS`, and runs every test of a file even though the verdict is per file.
- **Change.** In the subprocess: no coverage plugin, no xdist, `PYTEST_ADDOPTS` cleared, and
  `-x` (stop each file at its first failure — the verdict "red" is reached at the first
  failure, and it is the same failure with or without the rest). Measured: about 210 s → 57 s
  summed.

  ```python
  env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
  cmd = [
      sys.executable,
      "-m",
      "pytest",
      "-x",
      "-q",
      "-p",
      "no:cacheprovider",
      "-p",
      "no:cov",
      "-p",
      "no:xdist",
      test_file,
  ]
  ```

- **Pitfall.** `-x` is only valid when the harness asks "is this file red?"; a harness that
  counts failures needs all of them.
- **Guard test.** The harness's own mutation check: break one known thing, expect red.
- **Measure.** The harness's summed seconds before and after (`--durations`).

---

## Order of adoption

1. Habits first (`local-sieve`, `skip-ci-wip`, `no-default-merge`, `rerun-failed-only`,
   `watch-not-poll`, `batch-deploys`): no code, immediate.
2. `cancel-superseded`, `no-ci-branches`, `path-filter-docs`: one workflow edit each.
3. `fold-short-jobs`, `identical-tree`, `debounce-default-branch`: workflow restructuring.
4. `fast-tests`, `mutation-harness`: test-suite hygiene, measured locally.
5. `narrow-tests` with `push-light-suite` — the largest saving, and the only one that can
   let untested code reach production. Ship it together with the deploy-gate guard, never
   before it.

Re-run the audit a week after each step and compare the same figures.
