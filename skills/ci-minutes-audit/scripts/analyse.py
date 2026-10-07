#!/usr/bin/env python3
"""Turns a collected Actions dataset into a minutes report and ranked reductions.

Reads ``dataset.json`` written by ``collect.py`` and makes NO API call, so it can be re-run
with other options for free. It answers four questions:

1. **Where do the minutes go?** Billed minutes per job — every job rounded UP to a whole
   minute and multiplied by its runner's rate (Linux 1, Windows 2, macOS 10, self-hosted 0)
   — broken down by workflow and job, event, conclusion, branch type (default vs feature),
   and change type (docs-only, one component, several/shared), plus the steps inside the
   dominant job.
2. **How efficient is a change?** Minutes per opened pull request, billed minutes and
   wall-clock duration per run (median and p90), the waste share (failed, cancelled and
   re-attempted minutes) and the rounding overhead.
3. **Will the month fit?** With ``--plan-minutes`` (and optionally ``--cap-usd``): the
   burn-down against the plan's pace and a projection to the end of the month.
4. **What would save the most?** Each technique of ``references/techniques.md`` that the
   data can size is estimated from the window, scaled to 30 days and ranked; the others
   are listed with what to measure.

Usage::

    python3 analyse.py                                   # .ci-minutes-audit/dataset.json
    python3 analyse.py --components components.json --plan-minutes 3000 --cap-usd 50
    python3 analyse.py --json summary.json --top 15
    python3 analyse.py --self-test

Stdlib only.
"""

from __future__ import annotations

import argparse
import calendar
import fnmatch
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

#: Job conclusions whose minutes bought no answer anyone keeps.
WASTE = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})

#: Events whose runs answer "did this change break anything" — the ones a change type fits.
CHANGE_EVENTS = frozenset({"push", "pull_request", "pull_request_target", "merge_group"})

#: Paths that are documentation unless the component map says otherwise.
DEFAULT_DOCS = (r"(^|/)(docs?|site)/", r"\.(md|rst|adoc)$", r"(^|/)(LICENSE|NOTICE|AUTHORS)[^/]*$")

#: Paths that are shared by every component unless the map says otherwise.
DEFAULT_SHARED = (r"^\.github/", r"^[^/]+$")

#: Workflows that legitimately run on a docs-only change.
DEFAULT_DOCS_WORKFLOWS = r"(?i)\b(docs?|pages|site|mkdocs|spell|links?)\b"

#: Workflows that deploy or release.
DEFAULT_DEPLOY_WORKFLOWS = r"(?i)deploy|release|publish"

#: Branches that should not run CI at all (measurement, tuning and experiment branches).
DEFAULT_NO_CI_BRANCHES = (
    "measure/*",
    "bench/*",
    "benchmark/*",
    "experiment/*",
    "exp/*",
    "tuning/*",
    "tune/*",
    "spike/*",
    "perf/*",
)

#: A head commit that only freshens a branch from the default branch.
MERGE_DEFAULT = r"^Merge (remote-tracking )?branch '(origin/)?{default}'"

#: A head commit that is work in progress.
WIP = r"(?i)^(wip\b|\[wip\]|fixup!|squash!|tmp\b)"


def parse_time(value: str | None) -> datetime | None:
    """Parses a GitHub timestamp into an aware UTC datetime.

    Args:
        value: ``2025-01-31T10:00:00Z``, any ISO 8601 form, a plain date, or None.

    Returns:
        The moment, or None for None or an empty string.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def runner_rate(labels: Iterable[str] | None) -> int:
    """The billing multiplier of a job's runner, from its ``runs-on`` labels.

    Args:
        labels: The job's labels.

    Returns:
        0 for self-hosted, 10 for macOS, 2 for Windows, 1 otherwise (Linux). Larger
        runners have their own per-minute price; the report flags them.
    """
    text = " ".join(labels or []).lower()
    if "self-hosted" in text:
        return 0
    if "macos" in text:
        return 10
    if "windows" in text:
        return 2
    return 1


def job_minutes(job: dict[str, Any]) -> tuple[int, float]:
    """Billed and exact minutes of one job.

    GitHub bills each job from its start to its completion, rounded UP to a whole minute,
    times its runner's multiplier. A job that never started (skipped, queued, cancelled in
    the queue) costs nothing.

    Args:
        job: A job from the dataset.

    Returns:
        ``(billed, exact)``: the billed whole minutes and the unrounded minutes, both
        already multiplied by the runner rate.
    """
    start, end = parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
    if not start or not end or end <= start:
        return 0, 0.0
    rate = runner_rate(job.get("labels"))
    secs = (end - start).total_seconds()
    return math.ceil(secs / 60) * rate, secs / 60 * rate


def pct(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile.

    Args:
        values: The sample (need not be sorted).
        q: The quantile in [0, 1].

    Returns:
        The value at rank ``ceil(q * n)``, or 0.0 for an empty sample.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def job_key(name: str, keep_matrix: bool) -> str:
    """A job's name for grouping: the matrix suffix ``(a, b)`` dropped unless asked.

    Args:
        name: The job name as the API returns it.
        keep_matrix: Keep matrix variants apart.

    Returns:
        The grouping name.
    """
    return name if keep_matrix else re.sub(r"\s*\([^)]*\)\s*$", "", name) or name


@dataclass
class ComponentMap:
    """How changed paths map to components.

    Attributes:
        docs: Regexes of documentation paths.
        components: ``{component: [regex, ...]}``; empty means "the top-level directory".
        shared: Regexes of paths every component depends on (a change runs everything).
        ignore: Regexes of paths that matter to no component (dropped before classifying).
        unmatched: What a code path matching no component is: ``shared`` or ``ignore``.
    """

    docs: list[str] = field(default_factory=lambda: list(DEFAULT_DOCS))
    components: dict[str, list[str]] = field(default_factory=dict)
    shared: list[str] = field(default_factory=lambda: list(DEFAULT_SHARED))
    ignore: list[str] = field(default_factory=list)
    unmatched: str = "shared"

    @classmethod
    def load(cls, path: Path | None) -> ComponentMap:
        """Reads a component map from JSON, or returns the defaults.

        Args:
            path: A JSON file with any of the attributes as keys, or None.

        Returns:
            The map.

        Raises:
            ValueError: ``unmatched`` is neither ``shared`` nor ``ignore``.
        """
        if path is None:
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        out = cls(
            docs=data.get("docs", list(DEFAULT_DOCS)),
            components=data.get("components", {}),
            shared=data.get("shared", list(DEFAULT_SHARED)),
            ignore=data.get("ignore", []),
            unmatched=data.get("unmatched", "shared"),
        )
        if out.unmatched not in ("shared", "ignore"):
            raise ValueError("unmatched must be 'shared' or 'ignore'")
        return out

    def classify(self, files: list[str] | None) -> tuple[str, str | None]:
        """Classifies one change by the paths it touched.

        Args:
            files: The changed paths, or None when unknown.

        Returns:
            ``(class, component)``: class is ``docs-only`` (documentation alone),
            ``no component`` (documentation and ignored paths), ``one component``,
            ``several/shared``, ``no change`` (an empty diff) or ``unknown``; component is
            set for ``one component``.
        """
        if files is None:
            return "unknown", None
        if not files:
            return "no change", None
        hit = lambda pats, f: any(re.search(p, f) for p in pats)  # noqa: E731
        kept = [f for f in files if not hit(self.ignore, f)]
        code = [f for f in kept if not hit(self.docs, f)]
        comps: set[str] = set()
        for f in code:
            if hit(self.shared, f):
                return "several/shared", None
            if self.components:
                found = [c for c, pats in self.components.items() if hit(pats, f)]
            else:
                found = [f.split("/", 1)[0]] if "/" in f else []
            if found:
                comps.update(found)
            elif self.unmatched == "shared":
                return "several/shared", None
        if len(comps) > 1:
            return "several/shared", None
        if comps:
            return "one component", next(iter(comps))
        return ("docs-only" if not code and len(kept) == len(files) else "no component"), None


@dataclass
class Job:
    """One job of one attempt, with everything the breakdowns group by."""

    run: dict[str, Any]
    raw: dict[str, Any]
    workflow: str
    name: str
    attempt: int
    billed: int
    exact: float
    started: datetime | None
    completed: datetime | None


@dataclass
class Proposal:
    """One sized (or unsized) reduction.

    Attributes:
        tid: The technique id, a heading in ``references/techniques.md``.
        title: One line.
        saved: Estimated billed minutes saved in the window, or None when the data cannot
            size it.
        evidence: What in the data points at it.
        verify: What to measure before and after.
    """

    tid: str
    title: str
    saved: float | None
    evidence: str
    verify: str


VERIFY = {
    "path-filter-docs": "docs-only runs and their minutes per week: before > 0, after 0 "
    "(a required check still reports, from a stub job)",
    "narrow-tests": "median billed minutes of the dominant job on one-component pushes, and "
    "the test count per run; plus one full run before every deploy",
    "identical-tree": "runs whose tree already had a green run: billed minutes per such run "
    "drop to ~1; the proof artefact names the run it borrowed from",
    "no-default-merge": "runs whose head commit merges the default branch into a feature "
    "branch, per week",
    "cancel-superseded": "minutes of runs that finished after a newer run on the same branch "
    "had started; cancelled runs rise, billed minutes fall",
    "rerun-failed-only": "minutes in attempts > 1 spent on jobs that had already passed",
    "local-sieve": "red push runs per merged PR, and pushes per PR",
    "skip-ci-wip": "runs on WIP/fixup commits per week",
    "no-ci-branches": "runs on measurement/experiment branches: after 0",
    "batch-deploys": "deploy runs per day and their minutes",
    "watch-not-poll": "short dispatch runs per day",
    "fold-short-jobs": "rounding overhead (billed minus exact minutes) per run",
    "debounce-default-branch": "default-branch runs per day and how many were cancelled",
    "push-light-suite": "test-step seconds on a push with and without coverage and slow "
    "markers (measure locally first); deploy gate still runs everything",
    "fast-tests": "the slowest test phases (--durations) and the test step's median",
    "mutation-harness": "the harness's summed seconds before/after",
}


class Analysis:
    """All breakdowns and estimates of one dataset.

    Args:
        dataset: The parsed ``dataset.json``.
        cmap: The component map.
        keep_matrix: Keep matrix variants of a job apart.
        docs_workflows: Regex of workflows that legitimately run on docs-only changes.
        deploy_workflows: Regex of workflows that deploy.
        no_ci_branches: Globs of branches that should run no CI.
        narrow_keep: Share of the test step a narrowed run still pays (estimate input).
        catchable: Share of red pushes a local run would have caught (estimate input).
        steps_job: ``workflow / job`` whose steps to break down (default: the dominant job).

    Raises:
        ValueError: The dataset has no ``since``/``until`` window.
    """

    def __init__(
        self,
        dataset: dict[str, Any],
        cmap: ComponentMap | None = None,
        keep_matrix: bool = False,
        docs_workflows: str = DEFAULT_DOCS_WORKFLOWS,
        deploy_workflows: str = DEFAULT_DEPLOY_WORKFLOWS,
        no_ci_branches: Iterable[str] = DEFAULT_NO_CI_BRANCHES,
        narrow_keep: float = 0.5,
        catchable: float = 0.5,
        steps_job: str | None = None,
    ) -> None:
        self.ds = dataset
        self.cmap = cmap or ComponentMap()
        self.default = dataset.get("default_branch") or "main"
        since, until = parse_time(dataset.get("since")), parse_time(dataset.get("until"))
        if since is None or until is None:
            raise ValueError("the dataset has no window (since/until)")
        self.since: datetime = since
        self.until: datetime = until
        self.days = max((self.until - self.since).total_seconds() / 86400, 1 / 24)
        self.docs_workflows = docs_workflows
        self.deploy_workflows = deploy_workflows
        self.no_ci_branches = list(no_ci_branches)
        self.narrow_keep = narrow_keep
        self.catchable = catchable
        self.runs: list[dict[str, Any]] = dataset["runs"]
        self.jobs: list[Job] = []
        self.change: dict[int, tuple[str, str | None]] = {}
        for run in self.runs:
            ev = run.get("event") or "?"
            if ev in CHANGE_EVENTS:
                files = run.get("files")
                self.change[run["id"]] = self.cmap.classify(
                    None if files is None else files.get("files")
                )
            else:
                self.change[run["id"]] = (f"n/a ({ev})", None)
            for raw in run.get("jobs") or []:
                billed, exact = job_minutes(raw)
                self.jobs.append(
                    Job(
                        run,
                        raw,
                        run.get("name") or "?",
                        job_key(raw.get("name") or "?", keep_matrix),
                        int(raw.get("run_attempt") or 1),
                        billed,
                        exact,
                        parse_time(raw.get("started_at")),
                        parse_time(raw.get("completed_at")),
                    )
                )
        self.total = sum(j.billed for j in self.jobs)
        self.exact = sum(j.exact for j in self.jobs)
        by_job = self.table(lambda j: f"{j.workflow} / {j.name}")
        self.dominant = steps_job or (by_job[0][0] if by_job else None)

    # -- grouping -------------------------------------------------------------------------

    def table(self, key: Callable[[Job], str]) -> list[tuple[str, int, int]]:
        """Billed minutes and job count per key, largest first.

        Args:
            key: Maps a job to its group.

        Returns:
            ``[(key, minutes, jobs)]`` sorted by minutes, descending.
        """
        mins: Counter[str] = Counter()
        count: Counter[str] = Counter()
        for j in self.jobs:
            k = key(j)
            mins[k] += j.billed
            count[k] += 1
        return sorted(((k, mins[k], count[k]) for k in mins), key=lambda t: (-t[1], t[0]))

    def branch_type(self, run: dict[str, Any]) -> str:
        """``default`` for the default branch, ``feature`` for any other, ``none`` for none."""
        b = run.get("head_branch")
        return "none" if not b else ("default" if b == self.default else "feature")

    def run_minutes(self, run: dict[str, Any]) -> int:
        """Billed minutes of every attempt of one run."""
        return sum(job_minutes(j)[0] for j in run.get("jobs") or [])

    def run_wall(self, run: dict[str, Any]) -> float | None:
        """Wall-clock minutes of a run's final attempt: first job start to last job end."""
        att = int(run.get("run_attempt") or 1)
        js = [j for j in run.get("jobs") or [] if int(j.get("run_attempt") or 1) == att]
        starts = [t for t in (parse_time(j.get("started_at")) for j in js) if t]
        ends = [t for t in (parse_time(j.get("completed_at")) for j in js) if t]
        if not starts or not ends:
            return None
        return max(0.0, (max(ends) - min(starts)).total_seconds() / 60)

    def per30(self, minutes: float) -> float:
        """Scales minutes of the window to 30 days."""
        return minutes * 30 / self.days

    # -- steps ----------------------------------------------------------------------------

    def steps(self) -> tuple[list[tuple[str, int, float, float, float]], float]:
        """Step durations inside the dominant job's successful final-attempt runs.

        Returns:
            ``([(step, n, median s, p90 s, share of the job's seconds)], overhead share)``,
            where the overhead is job time outside every step (runner set-up, gaps).
        """
        secs: dict[str, list[float]] = defaultdict(list)
        job_total = 0.0
        step_total = 0.0
        for j in self.jobs:
            if f"{j.workflow} / {j.name}" != self.dominant or j.raw.get("conclusion") != "success":
                continue
            if j.attempt != int(j.run.get("run_attempt") or 1) or not j.raw.get("steps"):
                continue
            if not j.started or not j.completed:
                continue
            job_total += (j.completed - j.started).total_seconds()
            for s in j.raw["steps"]:
                a, b = parse_time(s.get("started_at")), parse_time(s.get("completed_at"))
                if a and b and b >= a:
                    d = (b - a).total_seconds()
                    secs[s.get("name") or "?"].append(d)
                    step_total += d
        rows = [
            (
                name,
                len(v),
                statistics.median(v),
                pct(v, 0.9),
                (sum(v) / job_total) if job_total else 0.0,
            )
            for name, v in secs.items()
        ]
        rows.sort(key=lambda r: -r[4])
        overhead = (job_total - step_total) / job_total if job_total else 0.0
        return rows, max(0.0, overhead)

    # -- budget ---------------------------------------------------------------------------

    def budget(
        self,
        plan: float | None,
        cap_usd: float | None,
        price: float,
        used: float | None,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Month-to-date burn against the plan and a projection to the month's end.

        The plan's minutes and the cap are per ACCOUNT, across every private repository;
        this dataset is one repository, so without ``used`` the month-to-date figure is a
        lower bound.

        Args:
            plan: Included minutes per month, or None to skip the section.
            cap_usd: The spending cap for overage, or None.
            price: Price of one overage minute (Linux rate; multipliers already applied).
            used: Month-to-date billed minutes from the billing page, or None to compute
                them from the dataset.
            as_of: "Now" for the projection (default: the window's end).

        Returns:
            The figures, or None when ``plan`` is None.
        """
        if plan is None:
            return None
        now = as_of or self.until
        assert now
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        dim = calendar.monthrange(now.year, now.month)[1]
        month_end = month_start + timedelta(days=dim)
        daily: Counter[str] = Counter()
        for j in self.jobs:
            if j.started and month_start <= j.started <= now:
                daily[f"{j.started:%Y-%m-%d}"] += j.billed
        covers = self.since <= month_start
        mtd_repo = sum(daily.values())
        source = (
            "billing page"
            if used is not None
            else ("this repository" if covers else "this repository, partial month")
        )
        used_now = used if used is not None else mtd_repo
        recent_from = max(self.since, now - timedelta(days=7))
        recent_days = max((now - recent_from).total_seconds() / 86400, 1 / 24)
        recent = sum(j.billed for j in self.jobs if j.started and recent_from <= j.started <= now)
        rate = recent / recent_days
        left_days = (month_end - now).total_seconds() / 86400
        projected = used_now + rate * left_days

        def when(target: float) -> str | None:
            if used_now >= target:
                return "already"
            if rate <= 0:
                return None
            t = now + timedelta(days=(target - used_now) / rate)
            return f"{t:%Y-%m-%d}" if t < month_end else None

        burn = []
        if covers and used is None:
            cum = 0.0
            for d in range(dim):
                day = month_start + timedelta(days=d)
                if day > now:
                    break
                cum += daily.get(f"{day:%Y-%m-%d}", 0)
                burn.append(
                    (f"{day:%Y-%m-%d}", daily.get(f"{day:%Y-%m-%d}", 0), cum, plan * (d + 1) / dim)
                )
        cap_minutes = plan + (cap_usd / price) if cap_usd is not None and price > 0 else None
        return {
            "plan": plan,
            "used": used_now,
            "used_source": source,
            "rate_per_day": rate,
            "days_left": left_days,
            "projected": projected,
            "pace_now": plan * (now - month_start).total_seconds() / 86400 / dim,
            "included_exhausted": when(plan),
            "cap_minutes": cap_minutes,
            "cap_reached": when(cap_minutes) if cap_minutes is not None else None,
            "overage_usd": max(0.0, projected - plan) * price,
            "burn": burn,
        }

    # -- proposals ------------------------------------------------------------------------

    def proposals(self) -> list[Proposal]:
        """Sizes every technique the data can size; lists the rest with their evidence.

        The estimates overlap (a superseded docs-only run counts in two of them), so they
        rank the candidates; they do not add up to a total.

        Returns:
            Sized proposals by minutes saved (descending), then unsized ones.
        """
        out: list[Proposal] = []
        change_runs = [r for r in self.runs if r.get("event") in CHANGE_EVENTS]
        feature = [r for r in change_runs if self.branch_type(r) == "feature"]

        # Docs-only changes that ran code workflows.
        docs_runs = [
            r
            for r in change_runs
            if self.change[r["id"]][0] == "docs-only"
            and not re.search(self.docs_workflows, r.get("name") or "")
        ]
        m = sum(self.run_minutes(r) for r in docs_runs)
        if m:
            out.append(
                Proposal(
                    "path-filter-docs",
                    "Skip code workflows on docs-only changes",
                    m,
                    f"{len(docs_runs)} docs-only runs of code workflows billed {m} min",
                    VERIFY["path-filter-docs"],
                )
            )

        # Narrowing the dominant job's test step on one-component changes.
        rows, _ = self.steps()
        test_share = rows[0][4] if rows else 1.0
        test_step = rows[0][0] if rows else "(no step data: whole job)"
        one = {r["id"] for r in change_runs if self.change[r["id"]][0] == "one component"}
        dom = sum(
            j.billed
            for j in self.jobs
            if j.run["id"] in one and f"{j.workflow} / {j.name}" == self.dominant
        )
        if dom:
            saved = dom * test_share * (1 - self.narrow_keep)
            out.append(
                Proposal(
                    "narrow-tests",
                    "Run only the tests the change needs (push runs)",
                    saved,
                    f"one-component runs paid {dom} min in `{self.dominant}`; its longest step "
                    f"`{test_step}` is {test_share:.0%} of the job; assumed a narrowed run keeps "
                    f"{self.narrow_keep:.0%} of it",
                    VERIFY["narrow-tests"],
                )
            )

        # Identical trees that already had a green run of the same workflow.
        green: dict[tuple[str, str], datetime] = {}
        for r in self.runs:
            tree = (r.get("head_commit") or {}).get("tree_id")
            end = self._run_end(r)
            if tree and r.get("conclusion") == "success" and end:
                k = (r.get("name") or "", tree)
                green[k] = min(green.get(k, end), end)
        same = []
        for r in change_runs:
            tree = (r.get("head_commit") or {}).get("tree_id")
            created = parse_time(r.get("created_at"))
            t = green.get((r.get("name") or "", tree or ""))
            if tree and created and t and t < created:
                same.append(r)
        m = sum(max(0, self.run_minutes(r) - 1) for r in same)
        if m:
            out.append(
                Proposal(
                    "identical-tree",
                    "Skip a run whose tree already passed",
                    m,
                    f"{len(same)} runs on a tree that already had a green run",
                    VERIFY["identical-tree"],
                )
            )

        # Merges of the default branch into a feature branch.
        pat = re.compile(MERGE_DEFAULT.format(default=re.escape(self.default)))
        merges = [
            r for r in feature if pat.search((r.get("head_commit") or {}).get("message") or "")
        ]
        m = sum(self.run_minutes(r) for r in merges)
        if m:
            out.append(
                Proposal(
                    "no-default-merge",
                    "Do not merge the default branch just to keep a branch fresh",
                    m,
                    f"{len(merges)} runs on such merges",
                    VERIFY["no-default-merge"],
                )
            )

        # Runs that kept going after a newer run on the same branch had started.
        sup_min, sup_n, cancelled = self._superseded()
        if sup_min:
            out.append(
                Proposal(
                    "cancel-superseded",
                    "Cancel superseded runs (concurrency group)",
                    sup_min,
                    f"{sup_n} runs finished after a newer run on the same "
                    f"branch had started ({cancelled} were already cancelled)",
                    VERIFY["cancel-superseded"],
                )
            )

        # Re-attempts that re-ran jobs which had already passed.
        m, n = self._rerun_waste()
        if m:
            out.append(
                Proposal(
                    "rerun-failed-only",
                    "Re-run only the failed jobs",
                    m,
                    f"{n} jobs re-ran after passing in the previous attempt",
                    VERIFY["rerun-failed-only"],
                )
            )

        # Red attempts on feature-branch pushes.
        red = self._red_attempt_minutes(feature)
        if red:
            per_branch = Counter(r.get("head_branch") for r in feature if r.get("event") == "push")
            counts = list(per_branch.values())
            out.append(
                Proposal(
                    "local-sieve",
                    "Run the local test sieve before pushing; push once",
                    red * self.catchable,
                    f"red attempts on feature branches billed {red} min (assumed "
                    f"{self.catchable:.0%} catchable locally); push runs per branch: "
                    f"median {pct(counts, 0.5):.0f}, p90 {pct(counts, 0.9):.0f}",
                    VERIFY["local-sieve"],
                )
            )

        # Work-in-progress commits.
        wip = [
            r
            for r in change_runs
            if re.search(WIP, (r.get("head_commit") or {}).get("message") or "")
        ]
        m = sum(self.run_minutes(r) for r in wip)
        if m:
            out.append(
                Proposal(
                    "skip-ci-wip",
                    "`[skip ci]` on work-in-progress pushes",
                    m,
                    f"{len(wip)} runs on WIP/fixup commits",
                    VERIFY["skip-ci-wip"],
                )
            )

        # Branches that should not run CI.
        noci = [
            r
            for r in change_runs
            if any(fnmatch.fnmatch(r.get("head_branch") or "", g) for g in self.no_ci_branches)
        ]
        m = sum(self.run_minutes(r) for r in noci)
        if m:
            out.append(
                Proposal(
                    "no-ci-branches",
                    "Measurement/experiment branches skip CI",
                    m,
                    f"{len(noci)} runs on branches matching {', '.join(self.no_ci_branches)}",
                    VERIFY["no-ci-branches"],
                )
            )

        # Several deploys a day.
        deploys = [r for r in self.runs if re.search(self.deploy_workflows, r.get("name") or "")]
        by_day: dict[tuple[str, str], list[int]] = defaultdict(list)
        for r in deploys:
            by_day[(r.get("name") or "", (r.get("created_at") or "")[:10])].append(
                self.run_minutes(r)
            )
        if deploys:
            med = statistics.median([m for v in by_day.values() for m in v])
            extra = sum(max(0, len(v) - 1) for v in by_day.values())
            if extra:
                out.append(
                    Proposal(
                        "batch-deploys",
                        "Batch deploys (one per batch, not per merge)",
                        extra * med,
                        f"{len(deploys)} deploy runs, {extra} beyond one a day "
                        f"per workflow (median {med:.0f} min)",
                        VERIFY["batch-deploys"],
                    )
                )

        # Short dispatch runs repeated: polling a detached job.
        short: dict[str, list[int]] = defaultdict(list)
        for r in self.runs:
            if r.get("event") == "workflow_dispatch":
                rm = self.run_minutes(r)
                if 0 < rm <= 1:
                    short[r.get("name") or "?"].append(rm)
        polled = {k: v for k, v in short.items() if len(v) >= 3}
        m = sum(sum(v) for v in polled.values())
        if m:
            out.append(
                Proposal(
                    "watch-not-poll",
                    "Watch runs instead of polling them",
                    m,
                    "short dispatch runs: "
                    + ", ".join(f"{k} ×{len(v)}" for k, v in sorted(polled.items())),
                    VERIFY["watch-not-poll"],
                )
            )

        # Short jobs whose rounding could be folded into another job.
        m, n = self._short_job_rounding()
        if m:
            out.append(
                Proposal(
                    "fold-short-jobs",
                    "Fold short jobs into steps of one job",
                    m,
                    f"{n} jobs under 2 min next to other jobs; rounding alone "
                    f"(set-up time not counted)",
                    VERIFY["fold-short-jobs"],
                )
            )

        # Default-branch push bursts.
        dpush = [
            r for r in self.runs if r.get("event") == "push" and self.branch_type(r) == "default"
        ]
        if len(dpush) / self.days > 6:
            ok = [self.run_minutes(r) for r in dpush if r.get("conclusion") == "success"]
            med = statistics.median(ok) if ok else 0
            m = max(0.0, sum(self.run_minutes(r) for r in dpush) - self.days * 6 * med)
            if m:
                canc = sum(1 for r in dpush if r.get("conclusion") == "cancelled")
                out.append(
                    Proposal(
                        "debounce-default-branch",
                        "Test the default branch on a schedule instead of every merge",
                        m,
                        f"{len(dpush)} push runs on the default branch "
                        f"({len(dpush) / self.days:.1f}/day, {canc} cancelled)",
                        VERIFY["debounce-default-branch"],
                    )
                )

        # Unsized: measured locally, pointed at by the step breakdown.
        if rows and rows[0][4] >= 0.4:
            out.append(
                Proposal(
                    "push-light-suite",
                    "Push runs without coverage and slow-marked "
                    "tests (deploy gate runs everything)",
                    None,
                    f"`{test_step}` is {test_share:.0%} of `{self.dominant}`",
                    VERIFY["push-light-suite"],
                )
            )
            out.append(
                Proposal(
                    "fast-tests",
                    "Shorten fixed sleeps, block network in unit tests",
                    None,
                    f"`{test_step}` median {rows[0][2] / 60:.1f} min",
                    VERIFY["fast-tests"],
                )
            )
        if any(
            re.search(r"(?i)mutat", f"{j.name} {[s.get('name') for s in j.raw.get('steps') or []]}")
            for j in self.jobs
        ):
            out.append(
                Proposal(
                    "mutation-harness",
                    "Lean mutation/subprocess harness",
                    None,
                    "a job or step names mutations",
                    VERIFY["mutation-harness"],
                )
            )
        sized = sorted((p for p in out if p.saved), key=lambda p: -(p.saved or 0))
        return sized + [p for p in out if p.saved is None]

    def _run_end(self, run: dict[str, Any]) -> datetime | None:
        ends = [t for t in (parse_time(j.get("completed_at")) for j in run.get("jobs") or []) if t]
        return max(ends) if ends else parse_time(run.get("updated_at"))

    def _superseded(self) -> tuple[float, int, int]:
        """Minutes a run spent after a newer run of the same workflow/branch/event began."""
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in self.runs:
            if r.get("event") in CHANGE_EVENTS and r.get("head_branch"):
                groups[(r.get("name") or "", r["head_branch"], r["event"])].append(r)
        saved, n, cancelled = 0.0, 0, 0
        for runs in groups.values():
            runs.sort(key=lambda r: r["created_at"])
            for older, newer in zip(runs, runs[1:], strict=False):
                t_new = parse_time(newer["created_at"])
                end = self._run_end(older)
                if not t_new or not end or end <= t_new:
                    continue
                if older.get("conclusion") == "cancelled":
                    cancelled += 1
                    continue
                att = int(older.get("run_attempt") or 1)
                extra = 0.0
                for j in older.get("jobs") or []:
                    if int(j.get("run_attempt") or 1) != att:
                        continue
                    a, b = parse_time(j.get("started_at")), parse_time(j.get("completed_at"))
                    if a and b and b > t_new:
                        extra += (
                            (b - max(a, t_new)).total_seconds() / 60 * runner_rate(j.get("labels"))
                        )
                if extra > 0:
                    saved += extra
                    n += 1
        return saved, n, cancelled

    def _rerun_waste(self) -> tuple[int, int]:
        """Minutes of jobs re-run in attempt k although they passed in attempt k-1."""
        m = n = 0
        for r in self.runs:
            by_att: dict[int, dict[str, str]] = defaultdict(dict)
            for j in r.get("jobs") or []:
                by_att[int(j.get("run_attempt") or 1)][j.get("name") or ""] = (
                    j.get("conclusion") or ""
                )
            for j in r.get("jobs") or []:
                k = int(j.get("run_attempt") or 1)
                if k > 1 and by_att[k - 1].get(j.get("name") or "") == "success":
                    b = job_minutes(j)[0]
                    if b:
                        m += b
                        n += 1
        return m, n

    def _red_attempt_minutes(self, runs: list[dict[str, Any]]) -> int:
        """Billed minutes of attempts that had a failed job, on push runs of these runs."""
        total = 0
        for r in runs:
            if r.get("event") != "push":
                continue
            by_att: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for j in r.get("jobs") or []:
                by_att[int(j.get("run_attempt") or 1)].append(j)
            for js in by_att.values():
                if any(j.get("conclusion") == "failure" for j in js):
                    total += sum(job_minutes(j)[0] for j in js)
        return total

    def _short_job_rounding(self) -> tuple[float, int]:
        """Rounding overhead of sub-2-minute jobs that share an attempt with other jobs."""
        m, n = 0.0, 0
        by_att: dict[tuple[int, int], list[Job]] = defaultdict(list)
        for j in self.jobs:
            if j.billed:
                by_att[(j.run["id"], j.attempt)].append(j)
        for js in by_att.values():
            if len(js) < 2:
                continue
            for j in js:
                if f"{j.workflow} / {j.name}" != self.dominant and j.exact < 2 * runner_rate(
                    j.raw.get("labels")
                ):
                    m += j.billed - j.exact
                    n += 1
        return m, n


# -- report -------------------------------------------------------------------------------


def _table(head: list[str], rows: Iterable[Iterable[Any]]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _share(rows: list[tuple[str, int, int]], total: int, top: int) -> str:
    return _table(
        ["", "billed min", "share", "jobs"],
        [(k, m, f"{m / total:.0%}" if total else "-", n) for k, m, n in rows[:top]],
    )


def build_summary(a: Analysis, budget: dict[str, Any] | None, top: int) -> dict[str, Any]:
    """Collects every figure of the report into one JSON-ready dict.

    Args:
        a: The analysis.
        budget: The budget figures, or None.
        top: How many rows per breakdown.

    Returns:
        The summary.
    """
    waste = sum(j.billed for j in a.jobs if (j.raw.get("conclusion") or "") in WASTE)
    reattempt = sum(j.billed for j in a.jobs if j.attempt > 1)
    pulls = a.ds.get("pulls")
    per_run = defaultdict(list)
    wall = defaultdict(list)
    for r in a.runs:
        per_run[r.get("name") or "?"].append(a.run_minutes(r))
        w = a.run_wall(r)
        if w is not None:
            wall[r.get("name") or "?"].append(w)
    change_tbl = a.table(lambda j: a.change[j.run["id"]][0])
    one_tbl = a.table(lambda j: a.change[j.run["id"]][1] or "")
    steps, overhead = a.steps()
    larger = sorted(
        {
            lbl
            for j in a.jobs
            for lbl in (j.raw.get("labels") or [])
            if re.search(r"\d+-cores?|large|xlarge|gpu|arm", lbl, re.I)
        }
    )
    return {
        "repo": a.ds.get("repo"),
        "since": a.ds.get("since"),
        "until": a.ds.get("until"),
        "days": a.days,
        "runs": len(a.runs),
        "jobs": len(a.jobs),
        "billed": a.total,
        "exact": a.exact,
        "rounding_share": (a.total - a.exact) / a.total if a.total else 0.0,
        "per_day": a.total / a.days,
        "per_30_days": a.per30(a.total),
        "by_job": a.table(lambda j: f"{j.workflow} / {j.name}")[:top],
        "by_workflow": a.table(lambda j: j.workflow)[:top],
        "by_event": a.table(lambda j: j.run.get("event") or "?"),
        "by_conclusion": a.table(lambda j: j.raw.get("conclusion") or "none"),
        "by_branch_type": a.table(lambda j: a.branch_type(j.run)),
        "by_change_type": change_tbl,
        "by_component": [r for r in one_tbl if r[0]][:top],
        "by_branch": a.table(lambda j: j.run.get("head_branch") or "-")[:top],
        "waste": waste,
        "waste_share": waste / a.total if a.total else 0.0,
        "reattempt": reattempt,
        "pulls_opened": None if pulls is None else len(pulls),
        "per_pull": (a.total / len(pulls)) if pulls else None,
        "runs_per_workflow": {
            k: {
                "runs": len(v),
                "billed_median": pct(v, 0.5),
                "billed_p90": pct(v, 0.9),
                "wall_median": pct(wall[k], 0.5),
                "wall_p90": pct(wall[k], 0.9),
            }
            for k, v in sorted(per_run.items(), key=lambda kv: -sum(kv[1]))[:top]
        },
        "dominant": a.dominant,
        "steps": steps[:top],
        "step_overhead": overhead,
        "larger_runners": larger,
        "missing": a.ds.get("missing"),
        "budget": budget,
        "proposals": [
            p.__dict__ | {"saved_per_30_days": a.per30(p.saved) if p.saved else None}
            for p in a.proposals()
        ],
    }


def render(s: dict[str, Any], top: int) -> str:
    """Renders the summary as a Markdown report.

    Args:
        s: The summary from ``build_summary``.
        top: Rows per breakdown.

    Returns:
        The report.
    """
    total = s["billed"]
    out = [
        f"# Actions minutes — {s['repo']}",
        "",
        f"Window {s['since']} .. {s['until']} ({s['days']:.1f} days): {s['runs']} runs, "
        f"{s['jobs']} jobs, **{total} billed min** ({s['per_day']:.0f}/day, "
        f"~{s['per_30_days']:.0f} per 30 days). Rounding each job up to a minute is "
        f"{s['rounding_share']:.0%} of them.",
        "",
    ]
    miss = s.get("missing") or {}
    if any(miss.get(k) for k in ("jobs", "files", "days")):
        out += [
            f"Missing from the cache: jobs of {len(miss.get('jobs') or [])} runs, changed "
            f"paths of {len(miss.get('files') or [])} runs, {len(miss.get('days') or [])} "
            "days.",
            "",
        ]
    if s["larger_runners"]:
        out += [
            f"Larger or ARM runners seen ({', '.join(s['larger_runners'])}): their "
            "per-minute price differs; minutes here use the OS multiplier only.",
            "",
        ]
    out += ["## Top cost centres (workflow / job)", "", _share(s["by_job"], total, top), ""]
    for title, key in (
        ("By event", "by_event"),
        ("By job conclusion", "by_conclusion"),
        ("By branch type", "by_branch_type"),
        ("By change type", "by_change_type"),
    ):
        out += [f"## {title}", "", _share(s[key], total, top), ""]
    if s["by_component"]:
        out += [
            "One-component changes, per component:",
            "",
            _share(s["by_component"], total, top),
            "",
        ]
    out += ["## Efficiency", ""]
    if s["per_pull"] is not None:
        out.append(
            f"- {s['pulls_opened']} pull requests opened: **{s['per_pull']:.1f} billed "
            "min per opened PR**."
        )
    out.append(
        f"- Waste (failed, cancelled, timed-out jobs): **{s['waste_share']:.0%}** "
        f"({s['waste']} min); re-attempts billed {s['reattempt']} min."
    )
    out += [
        "",
        _table(
            ["workflow", "runs", "billed/run median", "p90", "wall min median", "p90"],
            [
                (
                    k,
                    v["runs"],
                    v["billed_median"],
                    v["billed_p90"],
                    f"{v['wall_median']:.1f}",
                    f"{v['wall_p90']:.1f}",
                )
                for k, v in s["runs_per_workflow"].items()
            ],
        ),
        "",
    ]
    if s["steps"]:
        out += [
            f"## Steps of the dominant job `{s['dominant']}`",
            "",
            _table(
                ["step", "n", "median s", "p90 s", "share of job"],
                [(n, c, f"{m:.0f}", f"{p:.0f}", f"{sh:.0%}") for n, c, m, p, sh in s["steps"]],
            ),
            "",
            f"Job time outside any step (runner set-up): {s['step_overhead']:.0%}.",
            "",
        ]
    b = s.get("budget")
    if b:
        out += [
            "## Budget",
            "",
            f"- Used this month: {b['used']:.0f} of {b['plan']:.0f} included min "
            f"({b['used_source']}); plan pace today {b['pace_now']:.0f}.",
            f"- Recent rate {b['rate_per_day']:.0f} min/day; projected month end "
            f"**{b['projected']:.0f} min** ({b['days_left']:.1f} days left).",
            f"- Included minutes run out: {b['included_exhausted'] or 'not this month'}.",
        ]
        if b["used_source"] != "billing page":
            out.append(
                "- The plan counts every private repository of the account and this "
                "window"
                + (
                    " starts after the month began"
                    if "partial" in b["used_source"]
                    else " covers one repository"
                )
                + ": the dates above are late. Pass `--used-minutes` from the billing page."
            )
        if b["cap_minutes"] is not None:
            out.append(
                f"- Spending cap reached at {b['cap_minutes']:.0f} min: "
                f"{b['cap_reached'] or 'not this month'}; projected overage "
                f"{b['overage_usd']:.2f} USD."
            )
        if b["burn"]:
            out += [
                "",
                _table(
                    ["day", "billed", "cumulative", "plan pace"],
                    [(d, m, f"{c:.0f}", f"{p:.0f}") for d, m, c, p in b["burn"]],
                ),
            ]
        out.append("")
    out += [
        "## Proposed reductions",
        "",
        "Ranked by estimated billed minutes saved per 30 days. Estimates overlap — they "
        "rank candidates and do not add up. Details, pitfalls and guard tests: "
        "`references/techniques.md`.",
        "",
    ]
    if s["days"] < 7:
        out += [
            f"**The window is {s['days']:.1f} days**: every figure above is multiplied by "
            f"{30 / s['days']:.0f} to reach 30 days, and a technique backed by one or two "
            "runs is noise. Collect at least 14 days before deciding.",
            "",
        ]
    rows = []
    for i, p in enumerate(s["proposals"], 1):
        saved = f"~{p['saved_per_30_days']:.0f}" if p["saved"] else "measure"
        rows.append((i, f"`{p['tid']}`", p["title"], saved, p["evidence"], p["verify"]))
    out += [
        _table(["#", "technique", "change", "min/30 d", "evidence", "verify by"], rows)
        if rows
        else "Nothing the data can size; see the catalogue.",
        "",
    ]
    return "\n".join(out)


# -- self-test ----------------------------------------------------------------------------


def _synthetic() -> dict[str, Any]:
    """A two-day dataset whose every figure is known by hand (see ``self_test``)."""
    t0 = parse_time("2031-03-01T08:00:00Z")
    assert t0
    runs: list[dict[str, Any]] = []

    def at(minutes: float) -> datetime:
        return t0 + timedelta(minutes=minutes)

    def job(
        name: str,
        start: datetime,
        secs: float,
        conc: str = "success",
        att: int = 1,
        labels: list[str] | None = None,
        steps: list[tuple[str, float, float]] | None = None,
    ):
        return {
            "name": name,
            "run_attempt": att,
            "conclusion": conc,
            "status": "completed",
            "started_at": f"{start:%Y-%m-%dT%H:%M:%SZ}",
            "completed_at": f"{start + timedelta(seconds=secs):%Y-%m-%dT%H:%M:%SZ}",
            "labels": labels or ["ubuntu-latest"],
            "steps": [
                {
                    "name": n,
                    "started_at": f"{start + timedelta(seconds=a):%Y-%m-%dT%H:%M:%SZ}",
                    "completed_at": f"{start + timedelta(seconds=b):%Y-%m-%dT%H:%M:%SZ}",
                }
                for n, a, b in steps or []
            ],
        }

    def run(
        rid: int,
        wf: str,
        ev: str,
        branch: str,
        conc: str,
        created: datetime,
        jobs: list,
        files: list[str] | None = None,
        tree: str | None = None,
        msg: str = "change",
        att: int = 1,
    ) -> None:
        runs.append(
            {
                "id": rid,
                "name": wf,
                "event": ev,
                "head_branch": branch,
                "conclusion": conc,
                "status": "completed",
                "run_attempt": att,
                "created_at": f"{created:%Y-%m-%dT%H:%M:%SZ}",
                "head_sha": f"s{rid}",
                "head_commit": {"tree_id": tree or f"t{rid}", "message": msg},
                "jobs": jobs,
                "files": None if files is None else {"files": files},
            }
        )

    run(
        1,
        "CI",
        "push",
        "feat/a",
        "success",
        at(0),
        [
            job("unit", at(0), 125),
            job("lint", at(0), 30),
            {
                "name": "e2e",
                "run_attempt": 1,
                "conclusion": "skipped",
                "started_at": None,
                "completed_at": None,
            },
        ],
        ["docs/x.md", "README.md"],
    )
    run(
        2,
        "CI",
        "push",
        "feat/a",
        "success",
        at(10),
        [
            job("unit", at(10), 600, steps=[("Set up", 0, 60), ("Test", 60, 600)]),
            job("lint", at(10), 10),
        ],
        ["api/a.py"],
        tree="T2",
    )
    run(
        3,
        "CI",
        "push",
        "feat/a",
        "success",
        at(12),
        [
            job("unit", at(12), 300, "failure"),
            job("lint", at(12), 30),
            job("unit", at(30), 300, att=2),
            job("lint", at(30), 30, att=2),
        ],
        ["api/a.py", "web/b.js"],
        att=2,
    )
    run(
        4,
        "CI",
        "push",
        "feat/b",
        "success",
        at(40),
        [job("unit", at(40), 120), job("lint", at(40), 30)],
        ["web/c.js"],
        msg="Merge branch 'main' into feat/b",
    )
    run(
        5,
        "CI",
        "push",
        "main",
        "success",
        at(60),
        [job("unit", at(60), 600), job("lint", at(60), 30)],
        ["api/a.py"],
        tree="T2",
    )
    run(6, "Deploy", "workflow_dispatch", "main", "success", at(100), [job("deploy", at(100), 200)])
    run(7, "Deploy", "workflow_dispatch", "main", "success", at(300), [job("deploy", at(300), 200)])
    run(
        8,
        "Deploy",
        "workflow_dispatch",
        "main",
        "success",
        at(1500),
        [job("deploy", at(1500), 200)],
    )
    for i in range(3):
        run(
            9 + i,
            "Status",
            "workflow_dispatch",
            "main",
            "success",
            at(400 + i * 30),
            [job("poll", at(400 + i * 30), 20)],
        )
    run(
        12,
        "CI",
        "push",
        "measure/x",
        "success",
        at(500),
        [job("unit", at(500), 60), job("lint", at(500), 30)],
        ["api/z.py"],
    )
    run(
        13,
        "CI",
        "push",
        "feat/c",
        "failure",
        at(600),
        [job("unit", at(600), 90, "failure"), job("lint", at(600), 30)],
        ["api/q.py"],
        msg="WIP: try",
    )
    run(
        14,
        "CI",
        "push",
        "feat/d",
        "success",
        at(700),
        [job("unit-win", at(700), 61, labels=["windows-latest"])],
        None,
    )
    return {
        "repo": "o/r",
        "default_branch": "main",
        "since": "2031-03-01T00:00:00Z",
        "until": "2031-03-03T00:00:00Z",
        "runs": runs,
        "pulls": [{"number": 1}, {"number": 2}, {"number": 3}, {"number": 4}, {"number": 5}],
        "missing": {"jobs": [], "files": [], "days": []},
    }


def self_test() -> int:
    """Checks every figure of the analysis against a dataset computed by hand.

    Returns:
        0 when every check passes (an AssertionError is raised otherwise).
    """
    ds = _synthetic()
    a = Analysis(ds, no_ci_branches=["measure/*"])
    near = lambda x, y: abs(x - y) < 1e-6  # noqa: E731
    # Billing: ceil per job, Windows ×2, a skipped job is free.
    assert (
        job_minutes({"started_at": "2031-01-01T00:00:00Z", "completed_at": "2031-01-01T00:01:00Z"})[
            0
        ]
        == 1
    )
    assert (
        job_minutes({"started_at": "2031-01-01T00:00:00Z", "completed_at": "2031-01-01T00:01:01Z"})[
            0
        ]
        == 2
    )
    assert job_minutes({"started_at": None, "completed_at": None}) == (0, 0.0)
    assert (
        job_minutes(
            {
                "started_at": "2031-01-01T00:00:00Z",
                "completed_at": "2031-01-01T00:00:01Z",
                "labels": ["macos-14"],
            }
        )[0]
        == 10
    )
    assert a.total == 65, a.total
    assert a.dominant == "CI / unit", a.dominant
    ct = {k: m for k, m, _ in a.table(lambda j: a.change[j.run["id"]][0])}
    assert ct == {
        "docs-only": 4,
        "one component": 30,
        "several/shared": 12,
        "unknown": 4,
        "n/a (workflow_dispatch)": 15,
    }, ct
    bt = {k: m for k, m, _ in a.table(lambda j: a.branch_type(j.run))}
    assert bt == {"default": 26, "feature": 39}, bt
    s = build_summary(a, None, 10)
    assert s["waste"] == 7 and near(s["waste_share"], 7 / 65), s["waste"]
    assert s["reattempt"] == 6 and near(s["per_pull"], 13.0)
    assert s["runs_per_workflow"]["CI"]["billed_median"] == 4, s["runs_per_workflow"]["CI"]
    steps, _ = a.steps()
    assert steps[0][0] == "Test" and near(steps[0][4], 0.9), steps
    # The component map: explicit components, shared paths, unmatched code.
    cm = ComponentMap(components={"api": ["^api/"], "web": ["^web/"]}, shared=["^lib/"])
    assert cm.classify(["api/x.py", "docs/y.md"]) == ("one component", "api")
    assert cm.classify(["lib/z.py", "api/x.py"])[0] == "several/shared"
    assert cm.classify(["tools/t.py"])[0] == "several/shared"
    assert (
        ComponentMap(components={"api": ["^api/"]}, unmatched="ignore").classify(
            ["tools/t.py", "docs/a.md"]
        )[0]
        == "no component"
    )
    assert ComponentMap(ignore=["^tools/"]).classify(["tools/t.py"])[0] == "no component"
    assert cm.classify(["docs/a.md", "README.md"])[0] == "docs-only"
    assert cm.classify([])[0] == "no change"
    assert cm.classify(None)[0] == "unknown"
    # Every sized technique against its hand-computed value.
    got = {p.tid: p.saved for p in a.proposals()}
    want = {
        "narrow-tests": 25 * 0.9 * 0.5,
        "identical-tree": 10,
        "cancel-superseded": 8.0,
        "local-sieve": 9 * 0.5,
        "fold-short-jobs": 0.5 * 7 + 50 / 60,
        "path-filter-docs": 4,
        "batch-deploys": 4,
        "no-default-merge": 3,
        "skip-ci-wip": 3,
        "watch-not-poll": 3,
        "no-ci-branches": 2,
        "rerun-failed-only": 1,
    }
    for tid, val in want.items():
        assert tid in got and got[tid] is not None and near(got[tid], val), (tid, got.get(tid), val)
    assert "debounce-default-branch" not in got
    ranked = [p.tid for p in a.proposals() if p.saved]
    assert ranked[0] == "narrow-tests" and ranked[1] == "identical-tree", ranked
    assert "push-light-suite" in got and got["push-light-suite"] is None
    # Budget: the window covers the month start, two days at 32.5 min/day.
    b = a.budget(plan=100, cap_usd=10, price=0.01, used=None)
    assert b and b["used"] == 65 and near(b["rate_per_day"], 32.5), b
    assert near(b["projected"], 65 + 32.5 * 29), b["projected"]
    assert b["included_exhausted"] == "2031-03-04", b["included_exhausted"]
    assert b["cap_minutes"] == 1100 and b["cap_reached"] is None
    assert b["burn"][-1][2] == 65 and len(b["burn"]) == 3
    b2 = a.budget(plan=100, cap_usd=None, price=0.01, used=90)
    assert b2 and b2["used_source"] == "billing page" and not b2["burn"]
    report = render(build_summary(a, b, 10), 10)
    for needle in (
        "## Top cost centres",
        "## By change type",
        "per opened PR",
        "## Budget",
        "## Proposed reductions",
        "`narrow-tests`",
        "Collect at least",
    ):
        assert needle in report, needle
    # Re-analysing a saved summary needs no network: the dataset is plain JSON.
    json.dumps(build_summary(a, b, 10))
    print(
        "analyse.py self-test: OK (billing, breakdowns, change types, steps, "
        f"{len(want)} estimators, ranking, budget, report)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parses the command line and prints the report (or runs the self-test).

    Args:
        argv: The arguments without the program name (``sys.argv[1:]`` when None).

    Returns:
        The exit code: 0 on success, 2 when the dataset is missing.
    """
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--dataset", type=Path, default=Path(".ci-minutes-audit/dataset.json"))
    p.add_argument("--components", type=Path, help="JSON component map (see SKILL.md)")
    p.add_argument("--plan-minutes", type=float, help="included minutes per month")
    p.add_argument("--cap-usd", type=float, help="spending cap for overage")
    p.add_argument(
        "--price-per-minute",
        type=float,
        default=0.008,
        help="USD per overage Linux minute (check your plan; default 0.008)",
    )
    p.add_argument("--used-minutes", type=float, help="month-to-date minutes from the billing page")
    p.add_argument("--as-of", help="'now' for the projection (default: the window's end)")
    p.add_argument("--keep-matrix", action="store_true", help="keep matrix variants apart")
    p.add_argument("--docs-workflows", default=DEFAULT_DOCS_WORKFLOWS)
    p.add_argument("--deploy-workflows", default=DEFAULT_DEPLOY_WORKFLOWS)
    p.add_argument("--no-ci-branches", default=",".join(DEFAULT_NO_CI_BRANCHES))
    p.add_argument(
        "--narrow-keep",
        type=float,
        default=0.5,
        help="share of the test step a narrowed push still pays (default 0.5)",
    )
    p.add_argument(
        "--catchable",
        type=float,
        default=0.5,
        help="share of red pushes a local run would catch (default 0.5)",
    )
    p.add_argument("--steps-job", help="'workflow / job' to break into steps")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", type=Path, help="also write the summary as JSON")
    p.add_argument("--self-test", action="store_true")
    o = p.parse_args(argv)
    if o.self_test:
        return self_test()
    if not o.dataset.is_file():
        print(f"no dataset at {o.dataset}: run collect.py first", file=sys.stderr)
        return 2
    ds = json.loads(o.dataset.read_text(encoding="utf-8"))
    a = Analysis(
        ds,
        ComponentMap.load(o.components),
        o.keep_matrix,
        o.docs_workflows,
        o.deploy_workflows,
        [g for g in o.no_ci_branches.split(",") if g],
        o.narrow_keep,
        o.catchable,
        o.steps_job,
    )
    budget = a.budget(
        o.plan_minutes,
        o.cap_usd,
        o.price_per_minute,
        o.used_minutes,
        parse_time(o.as_of) if o.as_of else None,
    )
    summary = build_summary(a, budget, o.top)
    print(render(summary, o.top))
    if o.json:
        o.json.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
