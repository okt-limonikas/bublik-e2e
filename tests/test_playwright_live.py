from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest
from typer.testing import CliRunner

from cli import app
from core.playwright_bundle import LiveInfo, convert, facts_from_env, write_bundle
from core.playwright_live import LiveStream, follow, run_live

# A real run: tests/alpha.spec.mjs and beta.spec.mjs under Playwright 1.58.2 in
# the chromium project, retries 2, with the reporter and the JSON reporter.
FIXTURE = Path(__file__).parent / "fixtures" / "playwright-live"
EVENTS = [
    json.loads(line)
    for line in (FIXTURE / "live-events.ndjson").read_text().splitlines()
    if line.strip()
]
REPORT = json.loads((FIXTURE / "results.json").read_text())
ENV = {
    "PREVIEW_NAME": "chat",
    "PREVIEW_FQDN": "https://chat.bublik.example",
    "PREVIEW_DEPLOYMENT": "42",
    "JOB_BROWSER": "chromium",
    "JOB_RUN_URL": "http://preview.local/check-runs/7",
    "PREVIEW_SOURCE_UI_SHA": "879fe337c333bd4c2c2794d8e59000e483eb499e",
    "PREVIEW_SOURCE_UI_REF": "refs/pull/648/head",
    "PREVIEW_SOURCE_UI_REPOSITORY": "https://github.com/ts-factory/bublik-ui.git",
}
FAILING = "test:2fdbc118590eb59e16aa-f4e27d125a401c04a9de"
NESTED = "test:2fdbc118590eb59e16aa-cc57dc1193ef6d0af70a"
NEVER_RAN = "test:0b19296fe8f3584e5e5f-3f21687e38716d4ad500"


class FakeLive:
    def __init__(self) -> None:
        self.init: dict[str, Any] | None = None
        self.feed: list[dict[str, Any]] = []
        self.finish: dict[str, Any] | None = None
        self.cookies: list[str] = []
        self.fail_init = False


@pytest.fixture
def live() -> Iterator[tuple[FakeLive, str]]:
    fake = FakeLive()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            if self.headers.get("Cookie"):
                fake.cookies.append(self.headers["Cookie"])
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = self.path.split("?")[0]
            status, answer = 204, None
            if path.endswith("/importruns/init/"):
                if fake.fail_init:
                    status, answer = 500, {"message": "processing metadata"}
                else:
                    fake.init, status, answer = body, 200, {"runid": 77}
            elif path.endswith("/importruns/feed/"):
                assert "run=77" in self.path
                fake.feed.extend(body)
            elif path.endswith("/importruns/finish/"):
                fake.finish = body
            self.send_response(status)
            if answer is not None:
                data = json.dumps(answer).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def facts():
    out = facts_from_env(ENV, "")
    out.ts_name = "bublik-ui-e2e"
    return out


def stream(tmp_path: Path, url: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    path = tmp_path / "events.ndjson"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    out = tmp_path / "live.json"
    return run_live(
        path, out, LiveStream(url, facts(), flush_every=0), poll=0.01, idle_timeout=5
    )


def check_protocol(init: dict[str, Any], feed: list[dict[str, Any]]) -> None:
    """What Bublik's live context insists on: sequential ids, a start inside
    the node on top, an end of that node, and plan ids going forward."""
    plan_order: list[tuple[str, str]] = []

    def visit(item: dict[str, Any]) -> None:
        plan_order.append((item["type"], item["name"]))
        for child in item.get("children") or []:
            visit(child)

    visit(init["plan"])
    stack: list[int] = []
    expected_id = 1
    last_plan = -1
    last_ts = 0.0
    for event in feed:
        assert event["ts"] >= last_ts
        last_ts = event["ts"]
        if event["type"] == "test_start":
            assert event["id"] == expected_id
            expected_id += 1
            assert event["parent"] == (stack[-1] if stack else 0)
            assert event["plan_id"] > last_plan
            last_plan = event["plan_id"]
            assert plan_order[event["plan_id"]] == (event["node_type"], event["name"])
            stack.append(event["id"])
        else:
            assert event["type"] == "test_end"
            assert stack and stack[-1] == event["id"]
            stack.pop()
    assert stack == []


def test_a_run_streams_in_plan_order_and_records_its_numbering(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    record = stream(tmp_path, url, EVENTS)

    assert record["run"] == 77
    assert fake.cookies == []
    metas = {m["name"]: m for m in fake.init["meta_data"]["metas"]}
    assert metas["RUN_STATUS"]["value"] == "RUNNING"
    assert "FINISH_TIMESTAMP" not in metas
    assert metas["CFG"]["value"] == "chat" == record["cfg"]
    assert metas["START_TIMESTAMP"]["value"] == record["start_timestamp"]
    assert metas["TEST_RUN_URL"]["value"] == "http://preview.local/check-runs/7"
    assert {"name": "browser", "value": "chromium"} in fake.init["tags"]
    check_protocol(fake.init, fake.feed)
    assert fake.finish is not None
    tests = [
        e for e in fake.feed if e["type"] == "test_start" and e["node_type"] == "test"
    ]
    assert [e["tin"] for e in tests] == list(range(8))
    ends = {e["id"]: e for e in fake.feed if e["type"] == "test_end"}
    failing = ends[record["nodes"][FAILING]["id"]]
    assert failing["obtained"]["status"] == "FAILED"
    assert failing["error"] == "Unexpected test result(s)"
    assert failing["obtained"]["verdicts"][0].startswith("Error: one is two")


def test_a_result_that_comes_early_waits_for_the_ones_before_it(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    failing = next(e for e in EVENTS if e.get("id") == FAILING[len("test:") :])
    events = [e for e in EVENTS if e is not failing]
    nested_at = next(i for i, e in enumerate(events) if e.get("id") == NESTED[5:])
    events.insert(nested_at + 1, failing)

    record = stream(tmp_path, url, events)

    check_protocol(fake.init, fake.feed)
    assert record["nodes"][FAILING]["id"] < record["nodes"][NESTED]["id"]


def test_a_test_that_never_ran_is_skipped_and_the_rest_streams(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    events = [e for e in EVENTS if e.get("id") != NEVER_RAN[len("test:") :]]

    record = stream(tmp_path, url, events)

    check_protocol(fake.init, fake.feed)
    assert NEVER_RAN not in record["nodes"]
    assert fake.finish is not None


def test_the_bundle_merges_into_the_live_run(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    events = [e for e in EVENTS if e.get("id") != NEVER_RAN[len("test:") :]]
    stream(tmp_path, url, events)
    info = LiveInfo.load(tmp_path / "live.json")
    assert info is not None

    conversion = convert(REPORT, facts(), info)

    by_key = {}

    def walk(node: dict[str, Any], parent: int) -> None:
        by_key[node["_key"]] = (node, parent)
        for child in node["iters"]:
            walk(child, node["test_id"])

    walk(conversion.root, 0)
    for key, entry in info.nodes.items():
        node, parent = by_key[key]
        assert node["test_id"] == entry["id"], key
        assert parent == entry["parent"], key
        assert node["plan_id"] == entry["plan_id"], key
        if node["type"] == "test":
            assert node["tin"] == entry["tin"], key
    # What never streamed comes after it all.
    never, _ = by_key[NEVER_RAN]
    assert never["test_id"] > max(e["id"] for e in info.nodes.values())
    assert never["tin"] > max(e["tin"] for e in info.nodes.values())
    assert conversion.plan == info.plan

    written = write_bundle(REPORT, facts(), tmp_path / "bundle", live=info)
    metas = {
        m["name"]: m["value"]
        for m in json.loads((written.path / "meta_data.json").read_text())["metas"]
    }
    assert metas["START_TIMESTAMP"] == info.start_timestamp
    assert metas["CFG"] == info.cfg
    assert metas["RUN_STATUS"] == "ERROR"
    ids = sorted(
        n["test_id"]
        for n in _nodes(json.loads((written.path / "bublik.json").read_text()))
    )
    assert ids == list(range(1, len(ids) + 1))


def _nodes(run_log: dict[str, Any]) -> Iterator[dict[str, Any]]:
    stack = list(run_log["iters"])
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("iters") or [])


def test_a_stop_without_an_end_streams_what_was_written_and_finishes(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    path = tmp_path / "events.ndjson"
    # Killed after three tests: no end event.
    path.write_text("".join(json.dumps(e) + "\n" for e in EVENTS[:4]))
    live_stream = LiveStream(url, facts(), flush_every=0)

    follow(path, live_stream, poll=0.01, stopping=lambda: True)

    check_protocol(fake.init, fake.feed)
    assert len([e for e in fake.feed if e.get("node_type") == "test"]) == 3
    assert fake.finish is not None


runner = CliRunner()


def test_a_bublik_that_cannot_start_the_run_leaves_a_plain_import(
    tmp_path: Path, live: tuple[FakeLive, str]
) -> None:
    fake, url = live
    fake.fail_init = True
    events = tmp_path / "events.ndjson"
    events.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
    out = tmp_path / "live.json"

    outcome = runner.invoke(
        app,
        [
            "playwright-live",
            "--events",
            str(events),
            "--url",
            url,
            "--out",
            str(out),
            "--flush-every",
            "0",
            "--idle-timeout",
            "5",
        ],
        env=ENV,
    )

    assert outcome.exit_code == 0, outcome.output
    assert json.loads(out.read_text())["run"] is None
    assert fake.feed == [] and fake.finish is None
    assert LiveInfo.load(out) is None


def test_the_reporter_is_written_where_asked(tmp_path: Path) -> None:
    target = tmp_path / "work" / "bublik-live.mjs"

    outcome = runner.invoke(app, ["playwright-live", "--write-reporter", str(target)])

    assert outcome.exit_code == 0, outcome.output
    assert "export default class BublikLiveReporter" in target.read_text()
