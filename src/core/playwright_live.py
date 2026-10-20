"""Stream a running Playwright suite into Bublik over its live-import API.

``playwright_live_reporter.mjs`` writes the run as JSON lines: the browser's
suite tree when it begins, each test's final result, the end. This module
follows that file and turns it into the live protocol of ``core.live``:
``init`` with the run's metas and plan, ``feed`` batches of ``test_start`` /
``test_end``, ``finish``.

Bublik follows a live run along its plan and cannot go back: a ``test_start``
must come in plan order (it may skip ahead over what never ran), inside its
parent, and every node gets the next sequence number. Playwright settles a test
that fails only after its retries, which run once the rest of its file did, so
a result that comes early waits for the ones before it. Nothing is streamed
until it is final, so what Bublik shows never has to change.

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


def build_plan(suites: list[dict[str, Any]], browser: str, ts_name: str) -> PlanNode:
    """The run's tree as the converter names it, in the order Playwright runs
    it: files as listed, then specs and describes by line and column."""
    params = {"browser": browser}

    def package(name: str, path: list[str]) -> PlanNode:
        return PlanNode(package_key(path), bublik_name(name), "pkg", path)

    def suite_node(
        suite: dict[str, Any], path: list[str], name: str
    ) -> PlanNode | None:
        node_path = [*path, name]
        entries: list[tuple[int, int, PlanNode]] = []
        for spec in suite.get("specs") or []:
            test_name = bublik_name(str(spec.get("title", "test")))
            test_path = [*node_path, test_name]
            test = PlanNode(
                test_key(spec),
                test_name,
                "test",
                test_path,
                hash=_hash(test_path, params),
                params=params,
            )
            entries.append((spec.get("line", 0), spec.get("column", 0), test))
        for nested in suite.get("suites") or []:
            child = suite_node(nested, node_path, str(nested.get("title", "suite")))
            if child is not None:
                entries.append((nested.get("line", 0), nested.get("column", 0), child))
        if not entries:
            return None
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        node = package(name, node_path)
        node.children = [entry[2] for entry in entries]
        return node

    root = package(ts_name, [ts_name])
    root.children = [
        node
        for suite in suites
        if (node := suite_node(suite, [ts_name], _package_name(suite))) is not None
    ]
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
    return root


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
        self.browser = browser or facts.browser
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
        if kind == "begin" and self.root is None:
            self._begin(event)
        elif kind == "test" and self.root is not None:
            test = event.get("test") or {}
            self.results[f"test:{event.get('id') or ''}"] = dict(test)
            self._advance(at_end=False)
        elif kind == "end":
            self.end(_epoch(event.get("time")))
        if self.batch and self.clock() - self.last_flush >= self.flush_every:
            self.flush()

    def _begin(self, event: Mapping[str, Any]) -> None:
        self.browser = self.browser or str(event.get("browser") or "")
        if not self.browser:
            raise CliError("no browser: pass --browser or set JOB_BROWSER")
        self.root = build_plan(
            list(event.get("suites") or []), self.browser, self.facts.ts_name
        )
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

    def end(self, ts: float | None = None) -> None:
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
            self.post(url, {"ts": self._ts(ts)})
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
