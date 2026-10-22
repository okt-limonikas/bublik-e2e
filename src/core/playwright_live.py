"""Stream a running Playwright suite into Bublik over its live-import API.

``playwright_live_reporter.mjs`` writes the run as JSON lines: each browser
project's suite tree when it begins, each test's final result, the end. The
browsers make one run, each test a test per browser side by side in the
plan, as the converter lays out the bundle (see ``core.playwright_bundle``).
This module follows that file and turns it into the live protocol of
``core.live``: ``init`` with the run's metas and plan, ``feed`` batches of
``test_start`` / ``test_end``, ``finish``.

Bublik follows a live run along its plan and cannot go back: a ``test_start``
must come in plan order (it may skip ahead over what never ran), inside its
parent, and every node gets the next sequence number. Playwright settles a test
that fails only after its retries, which run once the rest of its file did, so
a result that comes early waits for the ones before it. Nothing is streamed
until it is final, so what Bublik shows never has to change. A test's run in
the first browser waits for its runs in the others, so the stream moves as
the slowest browser goes through the suite: side by side when the suite
shares its workers out among the browser projects (bublik-ui's E2E_WORKERS),
else, Playwright running the browsers one after the other, only as the last
browser goes, showing what the earlier ones did then.

When the suite ends, the Sweep's publish Job imports the whole bundle, which
merges into the live run only if it carries the same START_TIMESTAMP and CFG,
and each node the same sequence number, parent and tin: the ``--out`` file
records them for ``bublik-e2e playwright --live``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time as time_module
from typing import Any
import urllib.parse

from core.common import CliError, console
from core.live import LIVE_API, post_live
from core.playwright_bundle import (
    Facts,
    _attempt,
    _hash,
    _outcome,
    _package_name,
    bublik_name,
    bublik_timestamp,
    package_key,
    run_cfg,
    run_metas,
    run_tags,
    test_key,
)

REPORTER = Path(__file__).parent / "playwright_live_reporter.mjs"


def write_reporter(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(REPORTER.read_text(encoding="utf-8"), encoding="utf-8")
    return path


@dataclass
class PlanNode:
    key: str
    name: str
    type: str
    path: list[str]
    parent: PlanNode | None = None
    children: list[PlanNode] = field(default_factory=list)
    plan_id: int = -1
    hash: str = ""
    params: dict[str, str] = field(default_factory=dict)
    # The last test under a package, in plan order: once it is streamed, so
    # is everything the package holds.
    last_test: PlanNode | None = None

    def plan(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.children:
            out["children"] = [child.plan() for child in self.children]
        return out

    def ancestors(self) -> list[PlanNode]:
        """From the root down to the parent."""
        chain = []
        node = self.parent
        while node is not None:
            chain.append(node)
            node = node.parent
        return list(reversed(chain))


@dataclass
class Project:
    """A browser's Playwright project as the reporter's begin event lists it."""

    name: str
    suites: list[dict[str, Any]]


def build_plan(
    projects: list[Project], ts_name: str
) -> tuple[PlanNode, dict[str, str]]:
    """The run's tree as the converter lays it out: files as listed, specs
    and describes by line and column, each spec a test per browser side by
    side in the order the browsers are listed. Also each Playwright test id's
    key."""
    keys: dict[str, str] = {}
    root = PlanNode(package_key([ts_name]), bublik_name(ts_name), "pkg", [ts_name])
    # Where a node sorts among its siblings: a file as listed, a describe or
    # a test by line and column, a test's browsers in their order.
    places: dict[int, tuple[int, int, int]] = {}

    def add_suite(
        parent: PlanNode, suite: dict[str, Any], name: str, rank: int, browser: str
    ) -> None:
        node_path = [*parent.path, name]
        node = next(
            (c for c in parent.children if c.type == "pkg" and c.path == node_path),
            None,
        )
        if node is None:
            node = PlanNode(package_key(node_path), bublik_name(name), "pkg", node_path)
            places[id(node)] = (
                (len(parent.children), 0, 0)
                if parent is root
                else (suite.get("line", 0), suite.get("column", 0), 0)
            )
            parent.children.append(node)
        params = {"browser": browser}
        for spec in suite.get("specs") or []:
            test_name = bublik_name(str(spec.get("title", "test")))
            test_path = [*node_path, test_name]
            key = test_key(browser, test_path, spec)
            keys[str(spec.get("id") or "")] = key
            test = PlanNode(
                key,
                test_name,
                "test",
                test_path,
                hash=_hash(test_path, params),
                params=params,
            )
            places[id(test)] = (spec.get("line", 0), spec.get("column", 0), rank)
            node.children.append(test)
        for nested in suite.get("suites") or []:
            add_suite(node, nested, str(nested.get("title", "suite")), rank, browser)

    for rank, project in enumerate(projects):
        for suite in project.suites:
            add_suite(root, suite, _package_name(suite), rank, project.name)

    def settle(node: PlanNode) -> None:
        for child in node.children:
            settle(child)
        node.children = sorted(
            (c for c in node.children if c.type == "test" or c.children),
            key=lambda child: places[id(child)],
        )

    settle(root)
    counter = 0

    def number(node: PlanNode, parent: PlanNode | None) -> None:
        nonlocal counter
        node.parent = parent
        node.plan_id = counter
        counter += 1
        for child in node.children:
            number(child, node)
        if node.type == "test":
            node.last_test = node
        elif node.children:
            node.last_test = node.children[-1].last_test

    number(root, None)
    return root, keys


def begin_projects(event: Mapping[str, Any], browsers: list[str]) -> list[Project]:
    """The projects a begin event lists, narrowed to ``browsers`` if any; an
    event of the reporter's first, one-browser, shape is one project."""
    if "projects" in event:
        listed = [
            Project(str(p.get("project") or ""), list(p.get("suites") or []))
            for p in event.get("projects") or []
        ]
    else:
        listed = [
            Project(str(event.get("browser") or ""), list(event.get("suites") or []))
        ]
    listed = [p for p in listed if p.name]
    if browsers:
        listed = [p for p in listed if p.name in browsers]
    return listed


def _walk(node: PlanNode) -> Iterable[PlanNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


Post = Callable[[str, Any], Any]

#: The RUN_STATUS a live run ends with, by Playwright's end status. A Bublik
#: before finish took a status ends every run DONE.
RUN_STATUS_AT_END = {
    "passed": "DONE",
    "failed": "ERROR",
    "timedout": "ERROR",
    "interrupted": "INTERRUPTED",
}


def run_status_at_end(status: str | None) -> str:
    """``status`` as a RUN_STATUS; none, a suite stopped before its end, is
    INTERRUPTED."""
    return RUN_STATUS_AT_END.get(status or "", "INTERRUPTED")


class LiveStream:
    """One run's live import, fed the reporter's events in order."""

    def __init__(
        self,
        base_url: str,
        facts: Facts,
        *,
        browser: str = "",
        interval: int = 1,
        flush_every: float = 2.0,
        post: Post = post_live,
        clock: Callable[[], float] = time_module.monotonic,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.facts = facts
        named = browser or facts.browser
        self.browsers = [b.strip() for b in named.split(",") if b.strip()]
        # browser is the browsers streamed, comma-separated, once begun.
        self.browser = ""
        self.keys: dict[str, str] = {}
        self.interval = interval
        self.flush_every = flush_every
        self.post = post
        self.clock = clock
        self.warn = warn or (
            lambda message: console.print(f"[yellow]warning:[/] {message}")
        )

        self.run: int | None = None
        self.broken = False
        self.ended = False
        self.root: PlanNode | None = None
        self.start_timestamp = ""
        self.cfg = ""
        self.tests: list[PlanNode] = []
        self.cursor = 0
        self.results: dict[str, dict[str, Any]] = {}
        self.stack: list[PlanNode] = []
        self.ids: dict[str, dict[str, int]] = {}
        self.next_id = 1
        self.next_tin = 0
        self.last_ts = 0.0
        self.batch: list[dict[str, Any]] = []
        self.last_flush = clock()

    # --- events -----------------------------------------------------------

    def handle(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "begin" and self.root is None and not self.broken:
            self._begin(event)
        elif kind == "test" and self.root is not None:
            key = self.keys.get(str(event.get("id") or ""))
            if key is not None:
                self.results[key] = dict(event.get("test") or {})
                self._advance(at_end=False)
        elif kind == "end":
            self.end(_epoch(event.get("time")), status=str(event.get("status") or ""))
        if self.batch and self.clock() - self.last_flush >= self.flush_every:
            self.flush()

    def _begin(self, event: Mapping[str, Any]) -> None:
        projects = begin_projects(event, self.browsers)
        if not projects:
            among = f" among {', '.join(self.browsers)}" if self.browsers else ""
            self.warn(
                f"live import did not start: the run has no browser project{among}"
            )
            self.broken = True
            return
        self.browser = ",".join(p.name for p in projects)
        self.root, self.keys = build_plan(projects, self.facts.ts_name)
        self.tests = [node for node in _walk(self.root) if node.type == "test"]
        begun = _epoch(event.get("time")) or time_module.time()
        self.last_ts = begun
        self.start_timestamp = bublik_timestamp(
            datetime.fromtimestamp(begun, timezone.utc)
        )
        self.cfg = run_cfg(self.facts, self.browser)
        init = {
            "ts": begun,
            "interval": self.interval,
            "meta_data": {
                "version": 1,
                "metas": run_metas(
                    self.facts,
                    self.browser,
                    start=self.start_timestamp,
                    cfg=self.cfg,
                    status="RUNNING",
                ),
            },
            "tags": [
                {"name": name, "value": value}
                for name, value in sorted(run_tags(self.facts, self.browser).items())
            ],
            "plan": self.root.plan(),
        }
        try:
            response = self.post(f"{self.base_url}{LIVE_API}/init/", init)
        except CliError as exc:
            self.warn(
                f"live import did not start, the run is imported when it ends: {exc}"
            )
            self.broken = True
            return
        run = response.get("runid") if isinstance(response, dict) else None
        if not isinstance(run, int):
            self.warn(f"live import did not start: init answered {response!r}")
            self.broken = True
            return
        self.run = run
        self._start(self.root, begun)

    # --- the stream ---------------------------------------------------------

    def _ts(self, value: float | None) -> float:
        # Never back in time: Bublik takes each event's time as the latest.
        self.last_ts = max(self.last_ts, value or self.last_ts)
        return self.last_ts

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.run is not None and not self.broken:
            self.batch.append(payload)

    def _start(self, node: PlanNode, ts: float | None) -> None:
        parent = self.ids[node.parent.key]["id"] if node.parent else 0
        entry = {
            "id": self.next_id,
            "parent": parent,
            "tin": -1,
            "plan_id": node.plan_id,
        }
        if node.type == "test":
            entry["tin"] = self.next_tin
            self.next_tin += 1
        self.next_id += 1
        self.ids[node.key] = entry
        self._emit(
            {
                "type": "test_start",
                "id": entry["id"],
                "parent": parent,
                "plan_id": node.plan_id,
                "ts": self._ts(ts),
                "node_type": node.type,
                "name": node.name,
                "params": [[k, v] for k, v in node.params.items()],
                "hash": node.hash,
                "tin": entry["tin"],
            }
        )
        if node.type == "pkg":
            self.stack.append(node)

    def _end_package(self, ts: float | None) -> None:
        node = self.stack.pop()
        self._emit(
            {
                "type": "test_end",
                "id": self.ids[node.key]["id"],
                "parent": self.ids[node.key]["parent"],
                "plan_id": node.plan_id,
                "ts": self._ts(ts),
            }
        )

    def _stream_test(self, node: PlanNode, test: dict[str, Any]) -> None:
        attempts = sorted(
            (_attempt(r) for r in test.get("results") or []), key=lambda a: a.retry
        )
        starts = [a.start_ms / 1000 for a in attempts if a.start_ms]
        ends = [(a.start_ms + a.duration_ms) / 1000 for a in attempts if a.start_ms]
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        ancestors = node.ancestors()
        while self.stack and self.stack[-1] not in ancestors:
            self._end_package(start)
        for package in ancestors:
            if package not in self.stack:
                self._start(package, start)
        self._start(node, start)
        obtained, expected, err, verdicts = _outcome(test, attempts)
        end_event: dict[str, Any] = {
            "type": "test_end",
            "id": self.ids[node.key]["id"],
            "parent": self.ids[node.key]["parent"],
            "plan_id": node.plan_id,
            "ts": self._ts(end),
            "obtained": {"status": obtained, "verdicts": verdicts},
            "expected": [{"status": expected, "verdicts": []}],
        }
        if err:
            end_event["error"] = err
        self._emit(end_event)
        # A package whose last test this was is over: close it now, so the
        # run page shows it finished rather than when the next file starts.
        while len(self.stack) > 1 and self.stack[-1].last_test is node:
            self._end_package(end)

    def _advance(self, *, at_end: bool) -> None:
        if self.run is None or self.broken:
            return
        while self.cursor < len(self.tests):
            node = self.tests[self.cursor]
            test = self.results.get(node.key)
            if test is None:
                if not at_end:
                    return
                # Never ran: Bublik skips ahead over it.
                self.cursor += 1
                continue
            self._stream_test(node, test)
            self.cursor += 1

    def flush(self) -> None:
        self.last_flush = self.clock()
        if not self.batch or self.run is None or self.broken:
            self.batch.clear()
            return
        url = f"{self.base_url}{LIVE_API}/feed/?{urllib.parse.urlencode({'run': self.run})}"
        try:
            self.post(url, list(self.batch))
        except CliError as exc:
            self.warn(f"live import stopped, the run is imported when it ends: {exc}")
            self.broken = True
        self.batch.clear()

    def end(self, ts: float | None = None, status: str | None = None) -> None:
        """End the run: as Playwright's end ``status`` says, or interrupted
        when the suite was stopped before its end (no status)."""
        if self.ended:
            return
        self.ended = True
        if self.root is None:
            return
        self._advance(at_end=True)
        ts = ts or time_module.time()
        while self.stack:
            self._end_package(ts)
        self.flush()
        if self.run is None or self.broken:
            return
        url = f"{self.base_url}{LIVE_API}/finish/?{urllib.parse.urlencode({'run': self.run})}"
        try:
            self.post(url, {"ts": self._ts(ts), "status": run_status_at_end(status)})
        except CliError as exc:
            # Every event got there: the publish Job's import still merges.
            self.warn(f"live import could not finish the run: {exc}")

    def record(self) -> dict[str, Any]:
        """What ``--out`` holds; a null run makes the converter import afresh."""
        if self.run is None or self.broken or self.root is None:
            return {"run": None, "broken_run": self.run}
        return {
            "run": self.run,
            "url": self.base_url,
            "start_timestamp": self.start_timestamp,
            "cfg": self.cfg,
            "plan": self.root.plan(),
            "plan_ids": {node.key: node.plan_id for node in _walk(self.root)},
            "nodes": self.ids,
        }


def follow(
    path: Path,
    stream: LiveStream,
    *,
    poll: float = 0.5,
    idle_timeout: float = 0.0,
    stopping: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time_module.sleep,
    clock: Callable[[], float] = time_module.monotonic,
) -> None:
    """Feed ``stream`` the events ``path`` gets until the end event, a stop
    (after what is already written), or ``idle_timeout`` seconds of silence."""
    offset = 0
    pending = ""
    last_news = clock()
    while not stream.ended:
        stop = stopping()
        chunk = ""
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        if chunk:
            last_news = clock()
            pending += chunk
            *lines, pending = pending.split("\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stream.warn(f"skipped a line of {path} that is not JSON")
                    continue
                if isinstance(event, dict):
                    stream.handle(event)
        if stream.ended:
            break
        if stop or (idle_timeout and clock() - last_news >= idle_timeout):
            break
        if stream.batch and clock() - stream.last_flush >= stream.flush_every:
            stream.flush()
        sleep(poll)
    stream.end()


def run_live(
    events: Path,
    out: Path,
    stream: LiveStream,
    *,
    poll: float = 0.5,
    idle_timeout: float = 0.0,
) -> dict[str, Any]:
    """Follow ``events`` into ``stream`` until it ends or SIGTERM, then write
    ``out``; a failure of the live import never fails the caller."""
    stop = {"asked": False}

    def ask_to_stop(signum: int, frame: Any) -> None:
        stop["asked"] = True

    previous = signal.signal(signal.SIGTERM, ask_to_stop)
    try:
        follow(
            events,
            stream,
            poll=poll,
            idle_timeout=idle_timeout,
            stopping=lambda: stop["asked"],
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
        record = stream.record()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
