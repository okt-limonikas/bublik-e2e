from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from typing import Any
import urllib.parse

import pytest
from typer.testing import CliRunner

from cli import app
from core import bundle_import
from core.common import CliError
from core.playwright_bundle import (
    Facts,
    RepoFacts,
    convert,
    facts_from_env,
    write_bundle,
)

FIXTURE = Path(__file__).parent / "fixtures" / "playwright"
REPORT = json.loads((FIXTURE / "results.json").read_text())
UI_SHA = "879fe337c333bd4c2c2794d8e59000e483eb499e"
ENV = {
    "SWEEP_PREVIEW_NAME": "chat",
    "SWEEP_PREVIEW_FQDN": "https://chat.bublik.example",
    "SWEEP_PREVIEW_DEPLOYMENT": "42",
    "SWEEP_PREVIEW_DEPLOYMENT_URL": "http://preview.local/deployments/42",
    "SWEEP_JOB_BROWSER": "chromium",
    "SWEEP_APP": "bublik",
    "SWEEP_TEST_RUN_URL": "http://preview.local/check-runs/7",
    "SWEEP_TEST_REPORT_URL": "http://preview.local/check-runs/7/artifacts/report/index.html",
    "SWEEP_PREVIEW_SOURCE_UI_SHA": UI_SHA,
    "SWEEP_PREVIEW_SOURCE_UI_REF": "refs/pull/648/head",
    "SWEEP_PREVIEW_SOURCE_UI_REPOSITORY": "https://github.com/ts-factory/bublik-ui.git",
    "SWEEP_PREVIEW_SOURCE_UI_BRANCH": "feat/chat",
    "SWEEP_PREVIEW_SOURCE_BACKEND_SHA": "4c798881496ee952b12c7cdb0d89f13744f9ea90",
    "SWEEP_PREVIEW_SOURCE_BACKEND_REF": "main",
    "SWEEP_PREVIEW_SOURCE_BACKEND_REPOSITORY": "git@github.com:ts-factory/bublik.git",
    "UNRELATED": "x",
}


def facts(browser: str = "chromium") -> Facts:
    out = facts_from_env(ENV, "SWEEP_")
    out.browser = browser
    return out


def walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node["iters"]:
        yield from walk(child)


def outline(node: dict[str, Any], depth: int = 0) -> list[str]:
    lines = [f"{'  ' * depth}{node['test_id']} {node['type']} {node['name']}"]
    for child in node["iters"]:
        lines.extend(outline(child, depth + 1))
    return lines


def by_name(root: dict[str, Any], name: str) -> dict[str, Any]:
    return next(node for node in walk(root) if node["name"] == name)


def result(node: dict[str, Any]) -> tuple[str, str, list[str]]:
    obtained = node["obtained"]["result"]
    return obtained["status"], node["err"], obtained.get("verdicts", [])


def test_tree_holds_the_browsers_tests_in_the_order_they_ran() -> None:
    conversion = convert(REPORT, facts())
    assert outline(conversion.root) == [
        "1 pkg playwright",
        "  2 pkg dashboard",
        "    3 test shows the latest runs",
        "    4 pkg Dashboard filters",
        "      5 test filters by date",
        "      6 test filters by project",
        "  7 pkg runs-page",
        "    8 test exports the runs as CSV",
        "    9 test lists the runs of a day",
    ]
    nodes = list(walk(conversion.root))
    assert [node["plan_id"] for node in nodes] == list(range(9))
    tests = [node for node in nodes if node["type"] == "test"]
    assert [node["tin"] for node in tests] == [0, 1, 2, 3, 4]
    assert all(node["params"] == {"browser": "chromium"} for node in tests)
    assert {node["hash"] for node in nodes if node["type"] == "pkg"} == {""}
    assert conversion.plan["children"][0]["children"][1]["children"] == [
        {"name": "filters by date", "type": "test"},
        {"name": "filters by project", "type": "test"},
    ]


def test_statuses_follow_what_playwright_expected() -> None:
    root = convert(REPORT, facts()).root
    assert result(by_name(root, "shows the latest runs")) == ("PASSED", "", [])
    assert result(by_name(root, "filters by date")) == (
        "FAILED",
        "Unexpected test result(s)",
        [
            "Test timeout of 60000ms exceeded.",
            "Error: expect(locator).toBeVisible() failed",
        ],
    )
    assert result(by_name(root, "filters by project")) == (
        "PASSED",
        "",
        ["flaky: passed on retry 1"],
    )
    skipped = by_name(root, "exports the runs as CSV")
    assert result(skipped) == ("SKIPPED", "", [])
    assert skipped["expected"] == {"results": [{"status": "SKIPPED"}]}
    assert (
        by_name(root, "Dashboard filters")["obtained"]["result"]["status"] == "FAILED"
    )
    assert by_name(root, "runs-page")["obtained"]["result"]["status"] == "PASSED"
    assert by_name(root, "filters by date")["reqs"] == ["dashboard", "url-params"]


def test_another_browser_and_the_setup_projects_are_left_out() -> None:
    root = convert(REPORT, facts("firefox")).root
    assert outline(root) == [
        "1 pkg playwright",
        "  2 pkg runs-page",
        "    3 test lists the runs of a day",
    ]
    assert (
        result(by_name(root, "lists the runs of a day"))[1]
        == "Unexpected test result(s)"
    )
    chromium = convert(REPORT, facts()).root
    assert all(node["name"] != "authenticate" for node in walk(chromium))


def test_a_title_longer_than_bublik_keeps_is_cut_short_and_stays_unique() -> None:
    report = json.loads(json.dumps(REPORT))
    long = "Scenario: " + "the issues page keeps its filters when " * 3
    specs = report["suites"][0]["specs"]
    specs[0]["title"], specs[1]["title"] = long + "one", long + "two"

    names = [n for n in walk(convert(report, facts()).root)]

    shortened = [n for n in names if n["objective"].startswith("Scenario:")]
    assert len(shortened) == 2
    assert all(len(n["name"]) <= 64 for n in names)
    assert shortened[0]["name"] != shortened[1]["name"]
    assert {n["objective"] for n in shortened} == {long + "one", long + "two"}


def walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node["iters"]:
        yield from walk(child)


def test_hashes_are_stable_and_differ_per_browser() -> None:
    first = by_name(convert(REPORT, facts()).root, "lists the runs of a day")
    again = by_name(convert(REPORT, facts()).root, "lists the runs of a day")
    firefox = by_name(convert(REPORT, facts("firefox")).root, "lists the runs of a day")
    assert first["hash"] == again["hash"]
    assert first["hash"] != firefox["hash"]
    assert re.fullmatch(r"[0-9a-f]{32}", first["hash"])


def test_siblings_never_overlap_and_parents_span_their_children() -> None:
    root = convert(REPORT, facts()).root
    for node in walk(root):
        assert node["start_ts_utc"] <= node["end_ts_utc"]
        assert round(node["start_ts_utc"] * 1000) == node["start_ts_utc"] * 1000
        children = node["iters"]
        for prev, nxt in zip(children, children[1:]):
            assert nxt["start_ts_utc"] >= prev["end_ts_utc"]
        if children:
            assert children[0]["start_ts_utc"] >= node["start_ts_utc"]
            assert node["end_ts_utc"] >= children[-1]["end_ts_utc"]
    assert root["start_ts"] == "2026.10.20 10:00:00.000"


def test_facts_come_from_the_prefixed_environment() -> None:
    read = facts_from_env(ENV, "SWEEP_")
    assert (read.preview, read.deployment, read.browser, read.app) == (
        "chat",
        "42",
        "chromium",
        "bublik",
    )
    assert read.preview_url == "https://chat.bublik.example"
    (ui,) = [repo.resolved() for repo in read.repos if repo.slot == "ui"]
    assert ui.pr_number == "648"
    assert ui.pr_url == "https://github.com/ts-factory/bublik-ui/pull/648"
    assert ui.commit_url == f"https://github.com/ts-factory/bublik-ui/commit/{UI_SHA}"
    assert ui.branch == "feat/chat"
    (backend,) = [repo.resolved() for repo in read.repos if repo.slot == "backend"]
    assert backend.branch == "main"
    assert backend.repository_url == "https://github.com/ts-factory/bublik"
    unprefixed = facts_from_env({"PREVIEW_NAME": "own", "JOB_BROWSER": "webkit"}, "")
    assert (unprefixed.preview, unprefixed.browser) == ("own", "webkit")


def test_a_sha_ref_is_no_branch_and_explicit_facts_win() -> None:
    repo = RepoFacts(
        slot="docker",
        sha="a" * 40,
        ref="a" * 40,
        repository="https://gitea.example/me/bublik-docker.git",
        commit_url="https://gitea.example/me/bublik-docker/commit/x",
    ).resolved()
    assert repo.branch == ""
    assert repo.pr_url == ""
    assert repo.commit_url == "https://gitea.example/me/bublik-docker/commit/x"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    the_facts = facts()
    the_facts.report_url = ENV["SWEEP_TEST_REPORT_URL"]
    write_bundle(
        REPORT,
        the_facts,
        tmp_path / "e2e-results" / "chat-42-chromium",
        attachments_dir=FIXTURE / "test-output",
    )
    return tmp_path / "e2e-results" / "chat-42-chromium"


def test_bundle_has_its_files_and_done_last(bundle: Path) -> None:
    assert (bundle / ".done").read_text() == ""
    for name in ["tree.json", "node_1_0.json", "node_id1.json"] + [
        f"node_id{n}.json" for n in range(2, 10)
    ]:
        assert (bundle / "json" / name).is_file(), name
    tree = json.loads((bundle / "json" / "tree.json").read_text())
    assert tree["main_package"] == "node_1_0.json"
    assert tree["tree"]["node_id5.json"]["has_error"] is True


def test_metas_say_what_was_tested(bundle: Path) -> None:
    metas = json.loads((bundle / "meta_data.json").read_text())["metas"]
    by = {meta["name"]: meta for meta in metas}
    assert by["PROJECT"]["value"] == "bublik-preview"
    assert by["CFG"]["value"] == "chromium"
    assert by["RUN_STATUS"]["value"] == "DONE"
    assert by["START_TIMESTAMP"]["value"] == "2026-10-20T10:00:00.000+00:00"
    assert by["FINISH_TIMESTAMP"]["type"] == "timestamp"
    assert by["CAMPAIGN_DATE"]["value"] == "2026-10-20"
    assert by["UI_REV"] == {"name": "UI_REV", "value": UI_SHA, "type": "revision"}
    assert by["UI_BRANCH"] == {
        "name": "UI_BRANCH",
        "value": "feat/chat",
        "type": "branch",
    }
    assert by["UI_PR"]["value"] == "https://github.com/ts-factory/bublik-ui/pull/648"
    assert "type" not in by["UI_COMMIT_URL"]
    assert by["BACKEND_REPOSITORY"]["value"] == "https://github.com/ts-factory/bublik"
    assert by["PREVIEW"]["value"] == "chat"
    assert by["DEPLOYMENT"]["value"] == "42"
    assert by["TEST_RUN_URL"]["value"] == "http://preview.local/check-runs/7"
    run_log = json.loads((bundle / "bublik.json").read_text())
    assert run_log["tags"] == {
        "browser": "chromium",
        "preview": "chat",
        "app": "bublik",
        "ui_pr": "648",
    }
    assert "_shift_ms" not in json.dumps(run_log)


def test_attachments_of_the_last_attempt_keep_their_names(bundle: Path) -> None:
    node = bundle / "attachments" / "node_id5"
    listing = json.loads((node / "attachments.json").read_text())
    assert listing["version"] == 1
    entries = [(item["name"], item["path"]) for item in listing["attachments"]]
    assert entries == [
        ("screenshot (retry 2)", "./test-failed-1.png"),
        ("video (retry 2)", "./video.webm"),
        ("trace", "./trace.zip"),
        ("screenshot (retry 1)", "./retry1-test-failed-1.png"),
    ]
    assert all(
        item["type"] == "text"
        and item["view_type"] == "inline"
        and item["download_enabled"] is True
        for item in listing["attachments"]
    )
    assert (node / "test-failed-1.png").read_text() == "png-retry2"
    assert (node / "retry1-test-failed-1.png").read_text() == "png-retry1"
    run_level = json.loads(
        (bundle / "attachments" / "node_1_0" / "attachments.json").read_text()
    )
    assert run_level["attachments"][0]["uri"] == ENV["SWEEP_TEST_REPORT_URL"]
    assert sorted(p.name for p in (bundle / "attachments").iterdir()) == [
        "node_1_0",
        "node_id5",
    ]


def test_a_tests_log_tells_its_attempts_steps_and_errors(bundle: Path) -> None:
    log = json.loads((bundle / "json" / "node_id5.json").read_text())
    content = log["root"][0]["content"]
    assert content[0]["entity_model"]["error"] == "Unexpected test result(s)"
    rows = [
        (row["user_name"], row["log_content"][0]["content"])
        for row in content[-1]["data"]
    ]
    assert rows[0] == ("TAPI Jumps", "Main test entity")
    assert ("Self", "attempt 3: timedOut (60.0 s)") in rows
    assert ("Step", "Then the rows of the day") in rows
    assert ("Verdict", "Test timeout of 60000ms exceeded.") in rows
    assert ("Artifact", "trace: trace.zip") in rows
    assert not any("\x1b" in text for _, text in rows)
    numbers = [row["line_number"] for row in content[-1]["data"]]
    assert numbers == list(range(1, len(numbers) + 1))


def test_errors_are_refused_clearly(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="not a Playwright JSON report"):
        convert({"tests": []}, facts())
    with pytest.raises(CliError, match="no test of the Playwright project 'webkit'"):
        convert(REPORT, facts("webkit"))
    with pytest.raises(CliError, match="no browser"):
        convert(REPORT, facts(""))
    long = facts()
    long.repos.append(RepoFacts(slot="s" * 70, sha="b" * 40))
    with pytest.raises(CliError, match="schema validation"):
        write_bundle(REPORT, long, tmp_path / "b")
    assert not (tmp_path / "b" / ".done").exists()


runner = CliRunner()


def cli_env(**extra: str) -> dict[str, str]:
    return {**ENV, **extra}


def test_cli_writes_the_bundle_and_a_summary(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    outcome = runner.invoke(
        app,
        [
            "playwright",
            "--results",
            str(FIXTURE / "results.json"),
            "--attachments-dir",
            str(FIXTURE / "test-output"),
            "--publish-dir",
            str(tmp_path / "e2e-results"),
            "--bundle-id",
            "one",
            "--ts-name",
            "bublik-ui-e2e",
            "--url",
            "https://staging.example",
            "--summary",
            str(summary),
        ],
        env=cli_env(),
    )
    assert outcome.exit_code == 0, outcome.output
    assert "5 tests, 1 unexpected, 4 attachments" in outcome.output
    metas = json.loads(
        (tmp_path / "e2e-results" / "one" / "meta_data.json").read_text()
    )
    assert {"name": "TS_NAME", "value": "bublik-ui-e2e"} in metas["metas"]
    assert json.loads(summary.read_text()) == {
        "links": [
            {"title": "Bundle", "url": "https://staging.example/logs/e2e-results/one/"}
        ]
    }


def test_cli_fails_with_the_reason(tmp_path: Path) -> None:
    missing = runner.invoke(
        app,
        [
            "playwright",
            "--results",
            str(tmp_path / "nope.json"),
            "--publish-dir",
            str(tmp_path),
            "--bundle-id",
            "x",
        ],
        env=cli_env(),
    )
    assert missing.exit_code == 1
    assert "cannot read" in missing.output


class FakeBublik:
    """Just enough of Bublik's API for one bundle import."""

    def __init__(self) -> None:
        self.projects: list[dict[str, Any]] = [{"id": 1, "name": "other"}]
        self.configs: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.done: dict[str, int] = {}
        self.polls = 0
        self.logins: list[dict[str, Any]] = []

    def add_config(self, name: str, project: int, content: Any) -> None:
        self.configs.append(
            {
                "id": len(self.configs) + 1,
                "type": "global",
                "name": name,
                "project": project,
                "content": content,
            }
        )

    def handle(self, method: str, path: str, query: dict[str, str], body: Any) -> Any:
        if path == "/auth/login/":
            self.logins.append(body)
            return {}
        if path == "/api/v2/projects/":
            if method == "POST":
                project = {"id": len(self.projects) + 1, "name": body["name"]}
                self.projects.append(project)
                return project
            return self.projects
        if path == "/api/v2/config/":
            if method == "POST":
                self.add_config(body["name"], body["project"], body["content"])
                return {"id": self.configs[-1]["id"]}
            return [
                {k: v for k, v in config.items() if k != "content"}
                for config in self.configs
            ]
        match = re.fullmatch(r"/api/v2/config/(\d+)/", path)
        if match:
            config = self.configs[int(match.group(1)) - 1]
            if method == "PATCH":
                config["content"] = body["content"]
            return config
        if path == "/api/v2/importruns/source/":
            self.imports.append(query)
            return {"job_id": len(self.imports), "job_tasks_data": []}
        if path == "/api/v2/session_import/":
            url = query["url"].rstrip("/")
            if url in self.done:
                return {
                    "results": [
                        {
                            "run_source_url": url + "/",
                            "status": "SUCCESS",
                            "run_id": self.done[url],
                        }
                    ]
                }
            raise LookupError
        match = re.fullmatch(r"/api/v2/session_import/(\d+)/", path)
        if match:
            self.polls += 1
            url = self.imports[int(match.group(1)) - 1]["url"]
            if self.polls < 2:
                return [{"run_source_url": url, "status": "STARTED", "run_id": None}]
            self.done[url.rstrip("/")] = 77
            return [{"run_source_url": url, "status": "SUCCESS", "run_id": 77}]
        raise LookupError


@pytest.fixture
def bublik(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeBublik, str]]:
    fake = FakeBublik()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _serve(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            try:
                payload = fake.handle(self.command, parsed.path, query, body)
                status = 200
            except LookupError:
                payload, status = {"detail": "not found"}, 404
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bundle_import.time_module, "sleep", lambda _: None)
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def import_args(tmp_path: Path, url: str, *extra: str) -> list[str]:
    return [
        "playwright",
        "--results",
        str(FIXTURE / "results.json"),
        "--publish-dir",
        str(tmp_path / "e2e-results"),
        "--bundle-id",
        "chat-42-chromium",
        "--import",
        "--url",
        url,
        "--email",
        "admin@example.com",
        "--password",
        "s3cret",
        "--summary",
        str(tmp_path / "summary.json"),
        *extra,
    ]


def test_import_sets_the_project_up_and_waits_for_the_run(
    tmp_path: Path, bublik: tuple[FakeBublik, str]
) -> None:
    fake, url = bublik
    outcome = runner.invoke(
        app, import_args(tmp_path, url, "--setup-project"), env=cli_env()
    )
    assert outcome.exit_code == 0, outcome.output
    assert f"{url}/v2/runs/77" in outcome.stdout
    assert fake.logins == [{"email": "admin@example.com", "password": "s3cret"}]
    assert [p["name"] for p in fake.projects] == ["other", "bublik-preview"]
    assert fake.imports == [
        {"url": f"{url}/logs/e2e-results/chat-42-chromium/", "project": "2"}
    ]
    configs = {c["name"]: c for c in fake.configs}
    assert set(configs) == {"references", "per_conf", "meta"}
    references = configs["references"]["content"]
    assert references["LOGS_BASES"] == [
        {"name": "Published runs", "uri": [f"{url}/logs/"]}
    ]
    assert references["REVISIONS"]["UI_REV"] == {
        "name": "ui",
        "uri": "https://github.com/ts-factory/bublik-ui",
    }
    assert configs["per_conf"]["content"]["RUN_COMPLETE_FILE"] == ".done"
    assert json.loads((tmp_path / "summary.json").read_text()) == {
        "links": [{"title": "Bublik run", "url": f"{url}/v2/runs/77"}]
    }

    again = runner.invoke(
        app, import_args(tmp_path, url, "--setup-project"), env=cli_env()
    )
    assert again.exit_code == 0, again.output
    assert len(fake.imports) == 1
    assert [p["name"] for p in fake.projects] == ["other", "bublik-preview"]
    assert len(fake.configs) == 3


def test_import_merges_into_what_the_project_has(
    tmp_path: Path, bublik: tuple[FakeBublik, str]
) -> None:
    fake, url = bublik
    fake.projects.append({"id": 2, "name": "bublik-preview"})
    issues = {"E2E_BUGS": {"name": "E2E", "uri": "https://bugs.example/"}}
    fake.add_config(
        "references",
        2,
        {
            "ISSUES": issues,
            "LOGS_BASES": [{"name": "Seeded", "uri": [f"{url}/logs/"]}],
            "REVISIONS": {"TE_REV": {"name": "TE", "uri": "https://te.example"}},
        },
    )
    fake.add_config("per_conf", 2, {"RUN_COMPLETE_FILE": "custom"})
    outcome = runner.invoke(
        app, import_args(tmp_path, url, "--setup-project"), env=cli_env()
    )
    assert outcome.exit_code == 0, outcome.output
    references = fake.configs[0]["content"]
    assert references["ISSUES"] == issues
    assert references["LOGS_BASES"] == [{"name": "Seeded", "uri": [f"{url}/logs/"]}]
    assert set(references["REVISIONS"]) == {"TE_REV", "UI_REV", "BACKEND_REV"}
    assert fake.configs[1]["content"] == {"RUN_COMPLETE_FILE": "custom"}
    assert [c["name"] for c in fake.configs] == ["references", "per_conf", "meta"]


def test_import_without_setup_needs_the_project(
    tmp_path: Path, bublik: tuple[FakeBublik, str]
) -> None:
    fake, url = bublik
    outcome = runner.invoke(app, import_args(tmp_path, url), env=cli_env())
    assert outcome.exit_code == 1
    assert "project 'bublik-preview' does not exist" in outcome.output
    assert fake.imports == []


def test_a_failed_import_says_so(
    tmp_path: Path, bublik: tuple[FakeBublik, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, url = bublik
    original = fake.handle

    def failing(method: str, path: str, query: dict[str, str], body: Any) -> Any:
        if re.fullmatch(r"/api/v2/session_import/\d+/", path):
            return [
                {
                    "run_source_url": fake.imports[-1]["url"],
                    "status": "FAILURE",
                    "run_id": None,
                }
            ]
        return original(method, path, query, body)

    monkeypatch.setattr(fake, "handle", failing)
    outcome = runner.invoke(
        app, import_args(tmp_path, url, "--setup-project"), env=cli_env()
    )
    assert outcome.exit_code == 1
    assert "failed" in outcome.output
