#!/usr/bin/env python3
"""Collects a repository's GitHub Actions runs, jobs and changed paths into a local cache.

The audit needs three things per workflow run: the run itself (event, branch, conclusion,
head commit), its jobs with their timestamps and steps (billing is per job), and the paths
the run's commit changed (to tell a docs-only push from one that touched code). This
script fetches them with REPO-SCOPED REST calls only — ``gh api repos/{owner}/{repo}/...``
— because the search API is blocked or rate-limited in many environments.

Everything fetched is cached under ``--cache`` (default ``.ci-minutes-audit/``):

* ``repo.json`` — the repository name and its default branch;
* ``runs/<YYYY-MM-DD>.json`` — the runs created on one UTC day, kept only once the day is
  over and every run in it has completed (an unfinished day is fetched again);
* ``jobs/<run id>-a<attempt>.json`` — a completed run's jobs, every attempt;
* ``files/<sha>.json`` — the paths a commit changed (default branch: against its parent;
  any other branch: against the merge base with the default branch);
* ``pulls.json`` — the pull requests opened in the window (for "minutes per opened PR");
* ``dataset.json`` — everything above assembled for ``analyse.py``.

A second run over the same window therefore costs almost no API calls, and ``analyse.py``
never calls the API at all.

Without ``gh``: fetch the same objects with the GitHub MCP server's Actions tools, save the
raw responses as files, and import them (``--import-runs``/``--import-jobs``) followed by
``--offline``; see ``SKILL.md``.

Stdlib only; ``gh`` is the only external program.

Usage::

    python3 collect.py --days 14
    python3 collect.py --since 2025-01-01 --until 2025-01-31 --repo owner/name
    python3 collect.py --offline                # assemble dataset.json from the cache
    python3 collect.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

#: The list-runs endpoint returns at most this many results for one ``created`` query.
RESULT_CAP = 1000

#: Fields kept from a run object; the rest (URLs, repository copies) is dropped.
RUN_FIELDS = (
    "id",
    "name",
    "workflow_id",
    "path",
    "event",
    "status",
    "conclusion",
    "head_branch",
    "head_sha",
    "run_attempt",
    "run_number",
    "created_at",
    "updated_at",
    "run_started_at",
    "display_title",
)

#: Fields kept from a job object.
JOB_FIELDS = (
    "id",
    "run_id",
    "name",
    "run_attempt",
    "status",
    "conclusion",
    "started_at",
    "completed_at",
    "labels",
    "workflow_name",
    "head_branch",
)

ApiFn = Callable[[str], Any]


class GitHubApiFailure(RuntimeError):
    """A ``gh api`` call failed in a way a retry did not fix."""


def parse_time(value: str) -> datetime:
    """Parses a GitHub timestamp or a plain date into an aware UTC datetime.

    Args:
        value: ``2025-01-31``, ``2025-01-31T10:00:00Z`` or any ISO 8601 form.

    Returns:
        The moment in UTC; a plain date means its midnight.
    """
    text = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def iso(moment: datetime) -> str:
    """Formats a moment the way GitHub's ``created`` qualifier accepts it.

    Args:
        moment: An aware datetime.

    Returns:
        ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_api(path: str, retries: int = 3) -> Any:
    """Calls ``gh api`` with a GET and returns the parsed JSON.

    Args:
        path: The REST path, e.g. ``repos/o/r/actions/runs?per_page=100``.
        retries: Attempts for transient failures (5xx, secondary rate limit).

    Returns:
        The decoded JSON body.

    Raises:
        GitHubApiFailure: The call failed with a non-transient error, or kept failing.
    """
    delay = 2.0
    last = ""
    for _ in range(retries):
        proc = subprocess.run(
            ["gh", "api", "-X", "GET", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout or "null")
        last = (proc.stderr or proc.stdout).strip()
        transient = re.search(r"HTTP 5\d\d|rate limit|timed out|connection reset", last, re.I)
        if not transient:
            break
        time.sleep(delay)
        delay *= 2
    raise GitHubApiFailure(f"gh api {path}: {last[:300]}")


def detect_repo() -> str | None:
    """Finds ``owner/name`` from ``GITHUB_REPOSITORY`` or the ``origin`` remote.

    ``gh repo view`` is not used: it goes through GraphQL, which some environments block.

    Returns:
        ``owner/name``, or None when neither source names a GitHub repository.
    """
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def _trim(obj: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {k: obj.get(k) for k in fields}


def trim_run(run: dict[str, Any]) -> dict[str, Any]:
    """Keeps the fields of a run the analysis reads.

    Args:
        run: A run object from the REST API (or an MCP tool, which returns the same shape).

    Returns:
        A smaller dict with ``head_commit`` reduced to id, tree, message and timestamp and
        ``pull_requests`` to their numbers.
    """
    out = _trim(run, RUN_FIELDS)
    commit = run.get("head_commit") or {}
    out["head_commit"] = {
        "id": commit.get("id"),
        "tree_id": commit.get("tree_id"),
        "message": (commit.get("message") or "")[:500],
        "timestamp": commit.get("timestamp"),
    }
    out["pull_requests"] = [p.get("number") for p in run.get("pull_requests") or []]
    return out


def trim_job(job: dict[str, Any]) -> dict[str, Any]:
    """Keeps the fields of a job the analysis reads, steps included.

    Args:
        job: A job object from the REST API.

    Returns:
        A smaller dict; each step keeps its name, number, conclusion and timestamps.
    """
    out = _trim(job, JOB_FIELDS)
    out["steps"] = [
        _trim(s, ("name", "number", "status", "conclusion", "started_at", "completed_at"))
        for s in job.get("steps") or []
    ]
    return out


class Collector:
    """Fetches and caches runs, jobs, changed paths and pull requests for one repository.

    Args:
        repo: ``owner/name``.
        cache: The cache directory.
        api: The function that performs one GET (``gh_api``; a fake in the self-test).
        offline: Make no API call; use only what the cache holds.
        workers: Parallel API calls.
        refresh: Ignore cached run lists and refetch them.
        now: The current moment (injectable for the self-test).
    """

    def __init__(
        self,
        repo: str,
        cache: Path,
        api: ApiFn = gh_api,
        offline: bool = False,
        workers: int = 6,
        refresh: bool = False,
        now: datetime | None = None,
    ) -> None:
        self.repo = repo
        self.cache = cache
        self.api = api
        self.offline = offline
        self.workers = workers
        self.refresh = refresh
        self.now = now or datetime.now(UTC)
        self.calls = 0
        self.missing_days: list[str] = []
        for sub in ("runs", "jobs", "files"):
            (cache / sub).mkdir(parents=True, exist_ok=True)

    # -- low level ------------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        if self.offline:
            raise GitHubApiFailure(f"offline: {path} is not cached")
        self.calls += 1
        return self.api(path)

    @staticmethod
    def _read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # -- repository -----------------------------------------------------------------------

    def default_branch(self) -> str:
        """Returns the repository's default branch, cached in ``repo.json``.

        Returns:
            The branch name; ``main`` when offline with nothing cached.
        """
        meta = self.cache / "repo.json"
        if meta.is_file():
            data = self._read(meta)
            if data.get("repo") == self.repo and data.get("default_branch"):
                return data["default_branch"]
        try:
            branch = self._get(f"repos/{self.repo}")["default_branch"]
        except GitHubApiFailure:
            return "main"
        self._write(meta, {"repo": self.repo, "default_branch": branch})
        return branch

    # -- runs -----------------------------------------------------------------------------

    def _runs_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetches every run created in [start, end], splitting past the result cap."""
        query = f"created={iso(start)}..{iso(end)}"
        base = f"repos/{self.repo}/actions/runs?per_page=100&{query}"
        first = self._get(f"{base}&page=1")
        total = first.get("total_count", 0)
        if total > RESULT_CAP and end - start > timedelta(minutes=10):
            # Whole seconds: `created_at` has no fraction, so the halves leave no gap.
            middle = (start + (end - start) / 2).replace(microsecond=0)
            return self._runs_between(start, middle) + self._runs_between(
                middle + timedelta(seconds=1), end
            )
        runs = list(first.get("workflow_runs", []))
        page = 1
        while len(runs) < min(total, RESULT_CAP):
            page += 1
            batch = self._get(f"{base}&page={page}").get("workflow_runs", [])
            if not batch:
                break
            runs += batch
        return runs

    def runs(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Returns the trimmed runs created in the window, one cached file per UTC day.

        A day's file is reused only when the day ended at least an hour ago and every run
        in it had completed when it was written; otherwise the day is fetched again.

        Args:
            since: The window's start.
            until: The window's end.

        Returns:
            The runs, oldest first.
        """
        out: list[dict[str, Any]] = []
        day = since.replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= until:
            day_end = day + timedelta(days=1) - timedelta(seconds=1)
            file = self.cache / "runs" / f"{day:%Y-%m-%d}.json"
            cached = None
            if file.is_file() and (not self.refresh or self.offline):
                cached = self._read(file)
            if cached is not None and (cached.get("final") or self.offline):
                runs = cached["runs"]
            elif self.offline:
                runs = []
                self.missing_days.append(f"{day:%Y-%m-%d}")
            else:
                try:
                    runs = [trim_run(r) for r in self._runs_between(day, day_end)]
                except GitHubApiFailure:
                    if cached is None:
                        raise
                    runs = cached["runs"]
                else:
                    final = (self.now - day_end > timedelta(hours=1)) and all(
                        r.get("status") == "completed" for r in runs
                    )
                    self._write(file, {"final": final, "runs": runs})
            out += [r for r in runs if since <= parse_time(r["created_at"]) <= until]
            day += timedelta(days=1)
        uniq = {r["id"]: r for r in out}
        return sorted(uniq.values(), key=lambda r: r["created_at"])

    # -- jobs -----------------------------------------------------------------------------

    def jobs(self, run: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Returns every attempt's jobs of one run, cached once the run has completed.

        Args:
            run: A trimmed run.

        Returns:
            The trimmed jobs, or None when they are not cached and cannot be fetched.
        """
        file = self.cache / "jobs" / f"{run['id']}-a{run.get('run_attempt') or 1}.json"
        if file.is_file():
            return self._read(file)
        try:
            jobs: list[dict[str, Any]] = []
            page = 1
            while True:
                data = self._get(
                    f"repos/{self.repo}/actions/runs/{run['id']}/jobs"
                    f"?filter=all&per_page=100&page={page}"
                )
                batch = data.get("jobs", [])
                jobs += [trim_job(j) for j in batch]
                if len(jobs) >= data.get("total_count", 0) or not batch:
                    break
                page += 1
        except GitHubApiFailure:
            return None
        if run.get("status") == "completed":
            self._write(file, jobs)
        return jobs

    # -- changed paths --------------------------------------------------------------------

    def files(
        self, run: dict[str, Any], default_branch: str, previous: str | None = None
    ) -> dict[str, Any] | None:
        """Returns the paths a run's push changed, cached by the pair of commits.

        The runs API does not say what a push changed, so the best available basis is used:

        1. ``previous`` given (the head of the previous run on the same branch): the diff
           ``compare/<previous>...<sha>`` — what changed since that run;
        2. a feature branch without one: ``compare/<default>...<sha>``, the branch against
           its merge base — unless the branch is already merged (empty diff), then 3;
        3. the head commit against its first parent (``commits/<sha>``).

        An empty list means "no change" (a re-push of the same tree); the API lists at most
        300 files, and ``truncated`` says so.

        Args:
            run: A trimmed run.
            default_branch: The repository's default branch.
            previous: The head SHA of the previous run on the same branch, if known.

        Returns:
            ``{"files": [...], "truncated": bool, "basis": str}``, or None when unknown.
        """
        sha = run.get("head_sha")
        if not sha:
            return None
        key = f"{previous}..{sha}" if previous else sha
        file = self.cache / "files" / f"{key}.json"
        if file.is_file():
            return self._read(file)
        try:
            if previous:
                data = self._get(f"repos/{self.repo}/compare/{previous}...{sha}")
                basis = "previous-run"
            else:
                data, basis = {}, "commit"
                if run.get("head_branch") != default_branch:
                    data = self._get(f"repos/{self.repo}/compare/{default_branch}...{sha}")
                    basis = "branch"
                if not data.get("files"):
                    data = self._get(f"repos/{self.repo}/commits/{sha}")
                    basis = "commit"
        except GitHubApiFailure:
            return None
        names = [f.get("filename") for f in data.get("files") or [] if f.get("filename")]
        for f in data.get("files") or []:
            if f.get("previous_filename"):
                names.append(f["previous_filename"])
        result = {
            "files": sorted(set(names)),
            "truncated": len(data.get("files") or []) >= 300,
            "basis": basis,
        }
        self._write(file, result)
        return result

    @staticmethod
    def previous_heads(runs: list[dict[str, Any]]) -> dict[int, str | None]:
        """For each run, the head SHA of the latest earlier run on its branch with another SHA.

        Args:
            runs: Trimmed runs, any order.

        Returns:
            ``{run id: previous head SHA or None}``.
        """
        out: dict[int, str | None] = {}
        last: dict[str, list[str]] = {}
        for r in sorted(runs, key=lambda r: r["created_at"]):
            heads = last.setdefault(r.get("head_branch") or "", [])
            prev = next((h for h in reversed(heads) if h != r.get("head_sha")), None)
            out[r["id"]] = prev
            if r.get("head_sha") and (not heads or heads[-1] != r["head_sha"]):
                heads.append(r["head_sha"])
        return out

    # -- pull requests --------------------------------------------------------------------

    def pulls(self, since: datetime, until: datetime) -> list[dict[str, Any]] | None:
        """Returns the pull requests opened in the window.

        Args:
            since: The window's start.
            until: The window's end.

        Returns:
            ``[{"number", "created_at", "head_ref"}]``, or None when unknown.
        """
        file = self.cache / "pulls.json"
        cached = self._read(file) if file.is_file() else None
        if self.offline:
            if cached is None:
                return None
            return [p for p in cached if since <= parse_time(p["created_at"]) <= until]
        out: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                batch = self._get(
                    f"repos/{self.repo}/pulls?state=all&sort=created&direction=desc"
                    f"&per_page=100&page={page}"
                )
                if not batch:
                    break
                for p in batch:
                    out.append(
                        {
                            "number": p.get("number"),
                            "created_at": p.get("created_at"),
                            "head_ref": (p.get("head") or {}).get("ref"),
                        }
                    )
                if parse_time(batch[-1]["created_at"]) < since or len(batch) < 100:
                    break
                page += 1
        except GitHubApiFailure:
            return None if cached is None else cached
        merged = {p["number"]: p for p in (cached or []) + out}
        self._write(file, sorted(merged.values(), key=lambda p: p["number"]))
        return [p for p in out if since <= parse_time(p["created_at"]) <= until]

    # -- assembly -------------------------------------------------------------------------

    def collect(
        self, since: datetime, until: datetime, with_files: bool = True, with_pulls: bool = True
    ) -> dict[str, Any]:
        """Collects the window and writes ``dataset.json``.

        Args:
            since: The window's start.
            until: The window's end.
            with_files: Fetch each run's changed paths (one call per distinct head SHA).
            with_pulls: Fetch the pull requests opened in the window.

        Returns:
            The dataset: repository, default branch, window, runs with ``jobs`` and
            ``files`` attached, pull requests, and what is missing.
        """
        default = self.default_branch()
        runs = self.runs(since, until)
        with ThreadPoolExecutor(self.workers) as pool:
            all_jobs = list(pool.map(self.jobs, runs))
        files_by_run: dict[int, dict[str, Any] | None] = {}
        if with_files:
            prev = self.previous_heads(runs)
            # One call per distinct (previous, head) pair: a push starts several workflows.
            pairs = {(prev[r["id"]], r["head_sha"]): r for r in runs if r.get("head_sha")}
            with ThreadPoolExecutor(self.workers) as pool:
                got = list(pool.map(lambda kv: self.files(kv[1], default, kv[0][0]), pairs.items()))
            by_pair = dict(zip(pairs.keys(), got, strict=True))
            files_by_run = {r["id"]: by_pair.get((prev[r["id"]], r.get("head_sha"))) for r in runs}
        missing_jobs = []
        for run, jobs in zip(runs, all_jobs, strict=True):
            run["jobs"] = jobs if jobs is not None else []
            if jobs is None:
                missing_jobs.append(run["id"])
            run["files"] = files_by_run.get(run["id"])
        pulls = self.pulls(since, until) if with_pulls else None
        dataset = {
            "repo": self.repo,
            "default_branch": default,
            "since": iso(since),
            "until": iso(until),
            "collected_at": iso(self.now),
            "runs": runs,
            "pulls": pulls,
            "missing": {
                "jobs": missing_jobs,
                "files": sorted(i for i, f in files_by_run.items() if f is None),
                "days": self.missing_days,
            },
        }
        self._write(self.cache / "dataset.json", dataset)
        return dataset


def import_mcp(cache: Path, run_files: list[Path], job_files: list[Path]) -> tuple[int, int]:
    """Imports raw responses saved from the GitHub MCP tools into the cache layout.

    Run files may hold ``{"workflow_runs": [...]}`` or a bare list; job files
    ``{"jobs": [...]}`` or a bare list, every job carrying its ``run_id``. Imported days are
    marked final, so ``--offline`` uses them as they are.

    Args:
        cache: The cache directory.
        run_files: Saved run-list responses.
        job_files: Saved job-list responses.

    Returns:
        The number of runs and of jobs imported.
    """
    for sub in ("runs", "jobs"):
        (cache / sub).mkdir(parents=True, exist_ok=True)
    by_day: dict[str, dict[int, dict[str, Any]]] = {}
    attempts: dict[int, int] = {}
    n_runs = 0
    for f in run_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("workflow_runs", []) if isinstance(data, dict) else data
        for r in items:
            t = trim_run(r)
            by_day.setdefault(t["created_at"][:10], {})[t["id"]] = t
            attempts[t["id"]] = t.get("run_attempt") or 1
            n_runs += 1
    for day, runs in by_day.items():
        file = cache / "runs" / f"{day}.json"
        old = json.loads(file.read_text(encoding="utf-8"))["runs"] if file.is_file() else []
        merged = {r["id"]: r for r in old} | runs
        Collector._write(file, {"final": True, "runs": list(merged.values())})
    by_run: dict[int, list[dict[str, Any]]] = {}
    n_jobs = 0
    for f in job_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("jobs", []) if isinstance(data, dict) else data
        for j in items:
            by_run.setdefault(j["run_id"], []).append(trim_job(j))
            n_jobs += 1
    for run_id, jobs in by_run.items():
        Collector._write(cache / "jobs" / f"{run_id}-a{attempts.get(run_id, 1)}.json", jobs)
    return n_runs, n_jobs


# -- self-test ------------------------------------------------------------------------------


def _fake_api(now: datetime) -> tuple[ApiFn, list[str]]:
    """A fake GitHub with 230 runs over two days, one day past the result cap in size."""
    calls: list[str] = []
    runs = []
    for i in range(230):
        created = now - timedelta(days=2) + timedelta(minutes=12 * i)
        runs.append(
            {
                "id": 1000 + i,
                "name": "CI",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main" if i % 3 == 0 else f"feat/{i % 5}",
                "head_sha": f"sha{i % 40}",
                "run_attempt": 1,
                "created_at": iso(created),
                "head_commit": {"id": f"sha{i % 40}", "tree_id": f"tree{i % 40}", "message": "m"},
                "pull_requests": [],
            }
        )

    def grab(pattern: str, text: str) -> re.Match[str]:
        match = re.search(pattern, text)
        assert match, (pattern, text)
        return match

    def api(path: str) -> Any:
        calls.append(path)
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if "/actions/runs?" in path:
            a, b = grab(r"created=([^.]+)\.\.([^&]+)", path).groups()
            lo, hi = parse_time(a), parse_time(b)
            hits = [r for r in runs if lo <= parse_time(r["created_at"]) <= hi]
            page = int(grab(r"[?&]page=(\d+)", path).group(1))
            # The real endpoint stops at 1000 results; the fake caps at 60 to exercise it.
            visible = hits[:60]
            return {
                "total_count": len(hits) if len(hits) <= 60 else 1001,
                "workflow_runs": visible[(page - 1) * 100 : page * 100],
            }
        if "/jobs?" in path:
            rid = int(grab(r"runs/(\d+)/jobs", path).group(1))
            return {
                "total_count": 1,
                "jobs": [
                    {
                        "id": rid * 10,
                        "run_id": rid,
                        "name": "unit",
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2031-01-01T00:00:00Z",
                        "completed_at": "2031-01-01T00:01:01Z",
                        "labels": ["ubuntu-latest"],
                        "steps": [],
                    }
                ],
            }
        if "/commits/" in path or "/compare/" in path:
            return {
                "files": [
                    {"filename": "docs/a.md"},
                    {"filename": "src/x.py", "previous_filename": "src/y.py"},
                ]
            }
        if "/pulls?" in path:
            return [
                {
                    "number": 1,
                    "created_at": iso(now - timedelta(hours=5)),
                    "head": {"ref": "feat/1"},
                }
            ]
        raise AssertionError(path)

    # `self_test` lowers RESULT_CAP to 60 so the fake's cap behaves like the real 1000.
    return api, calls


def self_test() -> int:
    """Runs the collector against a fake GitHub and checks splitting, caching and import.

    Returns:
        0 when every check passes (an AssertionError is raised otherwise).
    """
    global RESULT_CAP
    saved_cap = RESULT_CAP
    RESULT_CAP = 60
    tmp = Path(tempfile.mkdtemp(prefix="ci-minutes-audit-"))
    try:
        now = parse_time("2031-01-10T12:00:00Z")
        api, calls = _fake_api(now)
        since, until = now - timedelta(days=2), now
        c = Collector("o/r", tmp / "cache", api=api, now=now)
        ds = c.collect(since, until)
        assert len(ds["runs"]) == 230, len(ds["runs"])
        assert len({r["id"] for r in ds["runs"]}) == len(ds["runs"]), "duplicate runs"
        assert all(r["jobs"] for r in ds["runs"]), "a run without jobs"
        assert ds["default_branch"] == "main"
        assert ds["runs"][0]["files"]["files"] == ["docs/a.md", "src/x.py", "src/y.py"]
        assert ds["runs"][0]["files"]["basis"] == "commit"  # first run on main
        assert any(re.search(r"compare/sha\d+\.\.\.sha\d+$", c) for c in calls), (
            "no previous-run diff"
        )
        assert ds["runs"][1]["files"]["basis"] == "branch"  # first run on its feature branch
        assert sum(1 for c in calls if "/actions/runs?" in c and "page=1" in c) > 3, "no cap split"
        heads = Collector.previous_heads(
            [
                {"id": 1, "created_at": "1", "head_branch": "b", "head_sha": "x"},
                {"id": 2, "created_at": "2", "head_branch": "b", "head_sha": "x"},
                {"id": 3, "created_at": "3", "head_branch": "b", "head_sha": "y"},
                {"id": 4, "created_at": "4", "head_branch": "c", "head_sha": "y"},
            ]
        )
        assert heads == {1: None, 2: None, 3: "x", 4: None}, heads
        assert ds["pulls"] and ds["pulls"][0]["number"] == 1
        assert not ds["missing"]["jobs"]
        first_calls = len(calls)
        # Second pass: the finished days, every job and every SHA come from the cache.
        c2 = Collector("o/r", tmp / "cache", api=api, now=now)
        ds2 = c2.collect(since, until)
        today_calls = len(calls) - first_calls
        assert len(ds2["runs"]) == len(ds["runs"])
        assert all(
            "/jobs?" not in p and "/compare/" not in p and "/commits/" not in p
            for p in calls[first_calls:]
        ), "a cached object was fetched again"
        assert today_calls <= 6, today_calls  # only today's unfinished run list + pulls
        # Offline: no call at all.
        c3 = Collector("o/r", tmp / "cache", api=api, offline=True, now=now)
        before = len(calls)
        ds3 = c3.collect(since, until)
        assert len(calls) == before and len(ds3["runs"]) == len(ds["runs"])
        # MCP import.
        raw = tmp / "mcp_runs.json"
        raw.write_text(
            json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 7,
                            "name": "CI",
                            "event": "push",
                            "status": "completed",
                            "created_at": "2031-02-01T10:00:00Z",
                            "run_attempt": 2,
                            "head_sha": "x",
                        }
                    ]
                }
            )
        )
        rawj = tmp / "mcp_jobs.json"
        rawj.write_text(json.dumps({"jobs": [{"id": 70, "run_id": 7, "name": "unit"}]}))
        assert import_mcp(tmp / "imp", [raw], [rawj]) == (1, 1)
        assert (tmp / "imp" / "jobs" / "7-a2.json").is_file()
        c4 = Collector("o/r", tmp / "imp", api=api, offline=True, now=now)
        ds4 = c4.collect(parse_time("2031-02-01"), parse_time("2031-02-02"), with_files=False)
        assert [r["id"] for r in ds4["runs"]] == [7] and ds4["runs"][0]["jobs"][0]["name"] == "unit"
        assert ds4["missing"]["days"] == ["2031-02-02"], ds4["missing"]
        assert parse_time("2031-03-04").isoformat() == "2031-03-04T00:00:00+00:00"
        assert math.isclose(
            parse_time("2031-03-04T01:00:00+01:00").timestamp(),
            parse_time("2031-03-04").timestamp(),
        )
    finally:
        RESULT_CAP = saved_cap
        shutil.rmtree(tmp, ignore_errors=True)
    print("collect.py self-test: OK (cap split, pagination, cache reuse, offline, MCP import)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parses the command line and runs the collection or the self-test.

    Args:
        argv: The arguments without the program name (``sys.argv[1:]`` when None).

    Returns:
        The exit code: 0 on success, 2 on a usage or API problem.
    """
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--repo", help="owner/name (default: GITHUB_REPOSITORY or the origin remote)")
    p.add_argument("--days", type=float, default=7.0, help="window length ending now (default 7)")
    p.add_argument("--since", help="window start, date or ISO time (overrides --days)")
    p.add_argument("--until", help="window end (default: now)")
    p.add_argument("--cache", type=Path, default=Path(".ci-minutes-audit"))
    p.add_argument("--offline", action="store_true", help="make no API call; use the cache")
    p.add_argument("--refresh", action="store_true", help="refetch cached run lists")
    p.add_argument("--no-files", action="store_true", help="skip changed paths (saves calls)")
    p.add_argument("--no-pulls", action="store_true", help="skip the pull-request count")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument(
        "--import-runs",
        type=Path,
        nargs="*",
        default=[],
        help="raw run-list JSON saved from an MCP tool",
    )
    p.add_argument(
        "--import-jobs",
        type=Path,
        nargs="*",
        default=[],
        help="raw job-list JSON saved from an MCP tool",
    )
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.import_runs or a.import_jobs:
        n_runs, n_jobs = import_mcp(a.cache, a.import_runs, a.import_jobs)
        print(f"imported {n_runs} runs and {n_jobs} jobs into {a.cache}", file=sys.stderr)
        if not a.offline:
            return 0
    repo = a.repo or detect_repo()
    if not repo:
        print("no repository: pass --repo owner/name", file=sys.stderr)
        return 2
    if not a.offline and shutil.which("gh") is None:
        print("`gh` not found: use the MCP fallback (SKILL.md) and --offline", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    until = parse_time(a.until) if a.until else now
    since = parse_time(a.since) if a.since else until - timedelta(days=a.days)
    collector = Collector(
        repo, a.cache, offline=a.offline, workers=a.workers, refresh=a.refresh, now=now
    )
    try:
        ds = collector.collect(since, until, with_files=not a.no_files, with_pulls=not a.no_pulls)
    except GitHubApiFailure as exc:
        print(f"collection failed: {exc}", file=sys.stderr)
        return 2
    n_jobs = sum(len(r["jobs"]) for r in ds["runs"])
    print(
        f"{repo}: {len(ds['runs'])} runs, {n_jobs} jobs, {collector.calls} API calls; "
        f"missing jobs for {len(ds['missing']['jobs'])} runs, paths for "
        f"{len(ds['missing']['files'])} runs -> {a.cache / 'dataset.json'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
