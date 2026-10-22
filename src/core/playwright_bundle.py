"""Turn a Playwright JSON report into a Bublik run-log bundle.

One bundle is one Playwright run, in one browser or several: the Playwright
projects named after the browsers, every spec file a package, every
``describe`` a nested package, every test a test with the parameter
``browser``. A test run in several browsers is one Bublik test with an
iteration per browser: its runs sit side by side under its package, in the
order the browsers ran, since Bublik groups a test's iterations only from
adjacent results of one package. What the run was tested against (the
preview, its deployment, each repository's revision, branch and PR) comes
from facts in the environment, as bublik-preview hands them to a Job.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from core.bundle import format_bublik_timestamp, recompute_package_statuses
from core.common import CliError, console
from core.constants import RUN_COMPLETE_FILE
from core.log_json import Node, Row, status_level, text_row, write_json_logs
from core.run_log_schema import (
    load_meta_data_validator,
    load_run_log_validator,
    validate_meta_data,
    validate_run_log,
)

SCHEMA_DIR = Path(__file__).parent / "schemas"
UNEXPECTED = "Unexpected test result(s)"
RESULT_STATUS = {
    "passed": "PASSED",
    "failed": "FAILED",
    "timedOut": "FAILED",
    "skipped": "SKIPPED",
    "interrupted": "INCOMPLETE",
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PULL_REF = re.compile(r"^refs/pull/(\d+)/head$")
_SPEC_SUFFIX = re.compile(r"(\.(spec|test|setup))?\.(ts|tsx|js|jsx|mjs|cjs)$")


@dataclass
class RepoFacts:
    slot: str
    sha: str = ""
    ref: str = ""
    repository: str = ""
    repository_url: str = ""
    branch: str = ""
    pr_number: str = ""
    pr_url: str = ""
    pr_title: str = ""
    commit_url: str = ""
    web_url: str = ""

    @property
    def meta_key(self) -> str:
        return re.sub(r"[^A-Z0-9_]", "_", self.slot.upper())

    @property
    def web_repository(self) -> str:
        """The repository's web page, when its URL says where that is."""
        if self.repository_url.startswith(("http://", "https://")):
            return self.repository_url.rstrip("/")
        url = self.repository
        if url.startswith("git@") and ":" in url:
            host, path = url[len("git@") :].split(":", 1)
            url = f"https://{host}/{path}"
        if url.startswith(("http://", "https://")):
            return re.sub(r"\.git$", "", url.rstrip("/"))
        return ""

    def resolved(self) -> RepoFacts:
        """Fill what the explicit facts leave out but the others imply."""
        out = RepoFacts(**self.__dict__)
        web = out.web_repository
        if not out.repository_url:
            out.repository_url = web
        if not out.branch and out.ref and not out.ref.startswith("refs/"):
            if not _SHA.match(out.ref):
                out.branch = out.ref
        if not out.pr_number:
            match = _PULL_REF.match(out.ref)
            if match:
                out.pr_number = match.group(1)
        if not out.pr_url and out.pr_number and web.startswith("https://github.com/"):
            out.pr_url = f"{web}/pull/{out.pr_number}"
        if not out.commit_url and web and out.sha:
            out.commit_url = f"{web}/commit/{out.sha}"
        return out


@dataclass
class Facts:
    # browser is the Playwright projects to convert, comma-separated; empty
    # means every project in the report but the setup ones.
    browser: str = ""
    project: str = "bublik-preview"
    ts_name: str = "playwright"
    # cfg is the run's CFG; empty means the preview's name.
    cfg: str = ""
    app: str = ""
    preview: str = ""
    preview_url: str = ""
    preview_page_url: str = ""
    deployment: str = ""
    deployment_url: str = ""
    test_run_url: str = ""
    report_url: str = ""
    started_at: str = ""
    finished_at: str = ""
    repos: list[RepoFacts] = field(default_factory=list)


_REPO_FIELDS = {
    "SHA": "sha",
    "REF": "ref",
    "REPOSITORY": "repository",
    "REPOSITORY_URL": "repository_url",
    "BRANCH": "branch",
    "PR_NUMBER": "pr_number",
    "PR_URL": "pr_url",
    "PR_TITLE": "pr_title",
    "COMMIT_URL": "commit_url",
    "WEB_URL": "web_url",
}


def facts_from_env(env: Mapping[str, str], prefix: str) -> Facts:
    """Read the facts bublik-preview gives a Job, under ``prefix``.

    The test Job itself reads its own with an empty prefix; a Job handed another
    run's variables under a prefix (``TEST_RUN_URL``, ``TEST_REPORT_URL`` among
    them) names it.
    """

    def get(name: str) -> str:
        return env.get(prefix + name, "") or ""

    source = f"{prefix}PREVIEW_SOURCE_"
    slots = sorted(
        name[len(source) : -len("_SHA")]
        for name in env
        if name.startswith(source) and name.endswith("_SHA")
    )
    repos = []
    for slot in slots:
        repo = RepoFacts(slot=slot.lower())
        for suffix, attr in _REPO_FIELDS.items():
            setattr(repo, attr, env.get(f"{source}{slot}_{suffix}", "") or "")
        repos.append(repo)
    return Facts(
        browser=get("JOB_BROWSER") or get("E2E_BROWSERS"),
        app=get("APP"),
        preview=get("PREVIEW_NAME"),
        preview_url=get("PREVIEW_FQDN") or get("PREVIEW_URL"),
        preview_page_url=get("PREVIEW_PAGE_URL"),
        deployment=get("PREVIEW_DEPLOYMENT"),
        deployment_url=get("PREVIEW_DEPLOYMENT_URL"),
        # The test Job itself (no prefix) is the test run: its own page, and
        # Playwright's HTML report among its artifacts, where bublik-ui's e2e
        # script copies it.
        test_run_url=get("TEST_RUN_URL") or (get("JOB_RUN_URL") if not prefix else ""),
        report_url=get("TEST_REPORT_URL")
        or (
            f"{get('JOB_ARTIFACTS_URL').rstrip('/')}/playwright-report/index.html"
            if not prefix and get("JOB_ARTIFACTS_URL")
            else ""
        ),
        started_at=get("TEST_STARTED_AT"),
        finished_at=get("TEST_FINISHED_AT"),
        repos=repos,
    )


@dataclass
class Attachment:
    name: str
    content_type: str
    retry: int
    path: str = ""
    body: str = ""


@dataclass
class Attempt:
    status: str
    retry: int
    start_ms: int
    duration_ms: int
    errors: list[str]
    steps: list[tuple[int, str, bool]]
    attachments: list[Attachment]


@dataclass
class Leaf:
    """What a test node's log and attachments are made of."""

    attempts: list[Attempt]
    tags: list[str]
    raw_start_ms: int | None
    raw_end_ms: int | None


@dataclass
class LiveInfo:
    """What ``bublik-e2e playwright-live`` streamed: the run's key metas, its
    plan, and the id, parent, tin and plan id each node got.

    A source import of the bundle merges into the live run only if it carries
    the same START_TIMESTAMP and CFG, and every node the same id (Bublik's
    exec_seqno), parent and tin.
    """

    run: int | None
    start_timestamp: str = ""
    cfg: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, dict[str, int]] = field(default_factory=dict)
    plan_ids: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> LiveInfo | None:
        """The live run ``path`` describes; None when there was none."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CliError(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CliError(f"{path} is not JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("run"), int):
            return None
        return cls(
            run=data["run"],
            start_timestamp=str(data.get("start_timestamp") or ""),
            cfg=str(data.get("cfg") or ""),
            plan=data.get("plan") or {},
            nodes={str(k): dict(v) for k, v in (data.get("nodes") or {}).items()},
            plan_ids={str(k): int(v) for k, v in (data.get("plan_ids") or {}).items()},
        )


@dataclass
class Conversion:
    root: Node
    leaves: dict[int, Leaf]
    plan: dict[str, Any]
    # browser is the browsers converted, comma-separated, in the order they ran.
    browser: str
    live: LiveInfo | None = None

    def tests(self) -> list[Node]:
        out: list[Node] = []

        def walk(node: Node) -> None:
            if node["type"] == "test":
                out.append(node)
            for child in node.get("iters") or []:
                walk(child)

        walk(self.root)
        return out


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _first_line(message: str) -> str:
    for line in strip_ansi(message).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _parse_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _package_name(suite: dict[str, Any]) -> str:
    name = suite.get("title") or suite.get("file") or "suite"
    return _SPEC_SUFFIX.sub("", Path(name).name) or name


def _steps(steps: list[dict[str, Any]], depth: int = 0) -> list[tuple[int, str, bool]]:
    out = []
    for step in steps or []:
        out.append((depth, str(step.get("title", "")), bool(step.get("error"))))
        out.extend(_steps(step.get("steps") or [], depth + 1))
    return out


def _attempt(result: dict[str, Any]) -> Attempt:
    messages = [
        str(error.get("message", ""))
        for error in result.get("errors") or []
        if error.get("message")
    ]
    if not messages and (result.get("error") or {}).get("message"):
        messages = [str(result["error"]["message"])]
    retry = int(result.get("retry", 0) or 0)
    return Attempt(
        status=str(result.get("status", "")),
        retry=retry,
        start_ms=_parse_ms(result.get("startTime")) or 0,
        duration_ms=max(0, int(result.get("duration", 0) or 0)),
        errors=messages,
        steps=_steps(result.get("steps") or []),
        attachments=[
            Attachment(
                name=str(item.get("name", "attachment")),
                content_type=str(item.get("contentType", "")),
                retry=retry,
                path=str(item.get("path") or ""),
                body=str(item.get("body") or ""),
            )
            for item in result.get("attachments") or []
            if item.get("path") or item.get("body")
        ],
    )


def _outcome(
    test: dict[str, Any], attempts: list[Attempt]
) -> tuple[str, str, str, list[str]]:
    """(obtained status, expected status, err, verdicts) of one Playwright test."""
    expected = RESULT_STATUS.get(str(test.get("expectedStatus", "passed")), "PASSED")
    last = attempts[-1] if attempts else None
    obtained = RESULT_STATUS.get(last.status, "INCOMPLETE") if last else expected
    status = str(test.get("status", ""))
    if status == "skipped":
        return "SKIPPED", expected, "", []
    if status == "flaky":
        retry = last.retry if last else 0
        return "PASSED", "PASSED", "", [f"flaky: passed on retry {retry}"]
    verdicts: list[str] = []
    for message in last.errors if last else []:
        line = _first_line(message)
        if line and line not in verdicts:
            verdicts.append(line)
    err = UNEXPECTED if status == "unexpected" else ""
    return obtained, expected, err, verdicts


def _hash(path: list[str], params: dict[str, str]) -> str:
    identity = json.dumps([path, params], sort_keys=True)
    return hashlib.md5(identity.encode(), usedforsecurity=False).hexdigest()


#: Projects that only prepare the others (log in, import fixtures), when the
#: report does not say which projects others depend on.
SETUP_PROJECTS = {"auth", "setup", "import", "teardown"}
_SETUP_FILE = re.compile(r"\.(setup|teardown)\.[cm]?[jt]sx?$")


def _report_specs(report: dict[str, Any]):
    """(file, spec) for every spec of the report."""

    def walk(suite: dict[str, Any], file: str):
        for spec in suite.get("specs") or []:
            yield str(spec.get("file") or file), spec
        for nested in suite.get("suites") or []:
            yield from walk(nested, file)

    for suite in report.get("suites") or []:
        yield from walk(suite, str(suite.get("file") or suite.get("title") or ""))


def setup_projects(report: dict[str, Any]) -> set[str]:
    """The projects others depend on, as the report's config says; else the
    ones named like setup projects or whose every test is in a setup file."""
    projects = (report.get("config") or {}).get("projects") or []
    named = {
        dependency
        for project in projects
        for dependency in list(project.get("dependencies") or [])
        + ([project["teardown"]] if project.get("teardown") else [])
    }
    if named:
        return named
    files: dict[str, set[bool]] = {}
    for file, spec in _report_specs(report):
        for test in spec.get("tests") or []:
            files.setdefault(str(test.get("projectName")), set()).add(
                bool(_SETUP_FILE.search(file))
            )
    return {
        name
        for name, setup in files.items()
        if name.lower() in SETUP_PROJECTS or setup == {True}
    }


def browsers_of(report: dict[str, Any], facts: Facts) -> list[str]:
    """The projects to convert, in the order they ran: the ones ``facts``
    names, else every project of the report but the setup ones."""
    order = {
        str(project.get("name")): index
        for index, project in enumerate(
            (report.get("config") or {}).get("projects") or []
        )
    }
    first: dict[str, float] = {}
    for _, spec in _report_specs(report):
        for test in spec.get("tests") or []:
            name = str(test.get("projectName"))
            starts = [_parse_ms(r.get("startTime")) for r in test.get("results") or []]
            earliest = min((s for s in starts if s), default=float("inf"))
            first[name] = min(first.get(name, float("inf")), earliest)
    named = [b.strip() for b in facts.browser.split(",") if b.strip()]
    if named:
        missing = [b for b in named if b not in first]
        if len(missing) == len(named):
            raise CliError(
                f"the report has no test of the Playwright project {', '.join(named)}"
            )
        for name in missing:
            console.print(f"[yellow]warning:[/] the report has no test of {name!r}")
        chosen = [b for b in named if b in first]
    else:
        setup = setup_projects(report)
        chosen = [name for name in first if name not in setup]
        if not chosen:
            raise CliError("the report has no test of a browser project")
    return sorted(
        chosen,
        key=lambda b: (
            first[b],
            order.get(b, len(order)),
            named.index(b) if b in named else 0,
        ),
    )


def convert(
    report: dict[str, Any], facts: Facts, live: LiveInfo | None = None
) -> Conversion:
    """Build the run's tree from ``report`` for the browsers ``facts`` names
    (every one but the setup projects, when it names none).

    One tree whatever the browsers: a test's run in each browser sits beside
    the others under its package, in the order the browsers ran. Siblings are
    ordered by when they ran (a test's runs by the first of them) and laid
    end to end: Bublik's timestamp repair on import shifts any sibling that
    starts before the previous one finished by a whole day, and refuses the
    run if that is not enough. With several browsers that gives up the wall
    clock from the second browser on: each run keeps its duration and its
    place in the order, not the time it ran. With ``live``, they are ordered
    and numbered as the live stream numbered them, and the plan is the one it
    sent.
    """
    if not isinstance(report, dict) or not isinstance(report.get("suites"), list):
        raise CliError("not a Playwright JSON report: it has no 'suites' list")
    browsers = browsers_of(report, facts)

    leaves_by_node: dict[int, Leaf] = {}

    def spec_node(spec: dict[str, Any], path: list[str], browser: str) -> Node | None:
        tests = [t for t in spec.get("tests") or [] if t.get("projectName") == browser]
        if not tests:
            return None
        test = tests[0]
        attempts = sorted(
            (_attempt(r) for r in test.get("results") or []), key=lambda a: a.retry
        )
        obtained, expected, err, verdicts = _outcome(test, attempts)
        title = str(spec.get("title", "test"))
        name = bublik_name(title)
        params = {"browser": browser}
        node_path = [*path, name]
        starts = [a.start_ms for a in attempts if a.start_ms]
        ends = [a.start_ms + a.duration_ms for a in attempts if a.start_ms]
        tags = [str(t).lstrip("@") for t in spec.get("tags") or []]
        node: Node = {
            "name": name,
            "type": "test",
            "params": params,
            "hash": _hash(node_path, params),
            "reqs": tags,
            "objective": title if name != title else "",
            "err": err,
            "obtained": {"result": {"status": obtained, "verdicts": verdicts}},
            "expected": {"results": [{"status": expected}]},
            "path": node_path,
            "path_str": "/".join(node_path),
            "iters": [],
            "_key": test_key(browser, node_path, spec),
        }
        leaves_by_node[id(node)] = Leaf(
            attempts=attempts,
            tags=tags,
            raw_start_ms=min(starts) if starts else None,
            raw_end_ms=max(ends) if ends else None,
        )
        return node

    def suite_node(suite: dict[str, Any], path: list[str], name: str) -> Node | None:
        node_path = [*path, name]
        entries: list[tuple[int, int, int, Node]] = []
        for spec in suite.get("specs") or []:
            for rank, browser in enumerate(browsers):
                child = spec_node(spec, node_path, browser)
                if child is not None:
                    child["_rank"] = rank
                    entries.append(
                        (spec.get("line", 0), spec.get("column", 0), rank, child)
                    )
        for nested in suite.get("suites") or []:
            child = suite_node(nested, node_path, str(nested.get("title", "suite")))
            if child is not None:
                entries.append(
                    (nested.get("line", 0), nested.get("column", 0), 0, child)
                )
        if not entries:
            return None
        entries.sort(key=lambda entry: entry[:3])
        return _package(name, node_path, [entry[3] for entry in entries])

    ts_name = facts.ts_name
    files = [
        node
        for suite in report["suites"]
        if (node := suite_node(suite, [ts_name], _package_name(suite))) is not None
    ]
    if not files:
        raise CliError(f"the report has no test of {', '.join(browsers)}")
    root = _package(ts_name, [ts_name], files)

    def earliest(node: Node) -> float:
        leaf = leaves_by_node.get(id(node))
        if leaf is not None:
            return leaf.raw_start_ms if leaf.raw_start_ms is not None else float("inf")
        return min((earliest(child) for child in node["iters"]), default=float("inf"))

    mapped = live.nodes if live is not None else {}

    def live_id(node: Node) -> float:
        entry = mapped.get(node["_key"])
        return entry["id"] if entry else float("inf")

    def order(node: Node) -> None:
        # A test's browser runs stay together, where the first of them ran,
        # in the order the browsers ran.
        first: dict[str, float] = {}
        for child in node["iters"]:
            if child["type"] == "test":
                first[child["name"]] = min(
                    first.get(child["name"], float("inf")), earliest(child)
                )

        def key(child: Node) -> tuple[float, float, int]:
            when = first[child["name"]] if child["type"] == "test" else earliest(child)
            return (live_id(child), when, child.get("_rank", 0))

        node["iters"].sort(key=key)
        for child in node["iters"]:
            order(child)

    order(root)

    first = earliest(root)
    if first == float("inf"):
        first = (
            _parse_ms(facts.started_at)
            or _parse_ms((report.get("stats") or {}).get("startTime"))
            or int(datetime.now(timezone.utc).timestamp() * 1000)
        )
    _place(root, int(first), leaves_by_node)

    leaves: dict[int, Leaf] = {}
    if live is None:
        test_id = 0
        tin = 0

        def number(node: Node) -> dict[str, Any]:
            nonlocal test_id, tin
            test_id += 1
            node["test_id"] = test_id
            node["plan_id"] = test_id - 1
            if node["type"] == "test":
                node["tin"] = tin
                tin += 1
            plan: dict[str, Any] = {"name": node["name"], "type": node["type"]}
            children = [number(child) for child in node["iters"]]
            if children:
                plan["children"] = children
            return plan

        plan = number(root)
    else:
        _number_from_live(root, live)
        plan = live.plan
    for node in _walk(root):
        if node["type"] == "test":
            leaves[node["test_id"]] = leaves_by_node[id(node)]
    recompute_package_statuses(root)
    mark_unexpected_packages(root)
    return Conversion(
        root=root, leaves=leaves, plan=plan, browser=",".join(browsers), live=live
    )


def test_key(browser: str, path: list[str], spec: dict[str, Any]) -> str:
    """How the live stream and the converter name a test: its project, path
    and place in its file. (Not its Playwright id: the JSON report keeps one
    spec per test with every project's run in it, and only the first
    project's id.)"""
    where = f"{spec.get('line', 0)}:{spec.get('column', 0)}"
    return f"test:{browser}/{'/'.join(path)}@{where}"


def package_key(path: list[str]) -> str:
    """A package's key: its path."""
    return "pkg:" + "/".join(path)


def _walk(node: Node):
    yield node
    for child in node.get("iters") or []:
        yield from _walk(child)


def _number_from_live(root: Node, live: LiveInfo) -> None:
    """Give each node the id, tin and plan id the live stream gave it; the
    nodes it never streamed (tests that never ran) come after, in order."""
    nodes = list(_walk(root))
    next_id = max((e["id"] for e in live.nodes.values()), default=0) + 1
    next_tin = max((e.get("tin", -1) for e in live.nodes.values()), default=-1) + 1
    next_plan = max(live.plan_ids.values(), default=-1) + 1
    for node in nodes:
        entry = live.nodes.get(node["_key"])
        if entry is not None:
            node["test_id"] = entry["id"]
            if node["type"] == "test":
                node["tin"] = entry["tin"]
        else:
            node["test_id"] = next_id
            next_id += 1
            if node["type"] == "test":
                node["tin"] = next_tin
                next_tin += 1
        plan_id = live.plan_ids.get(node["_key"])
        if plan_id is None:
            plan_id = next_plan
            next_plan += 1
        node["plan_id"] = plan_id


def mark_unexpected_packages(node: Node) -> bool:
    """Give each package holding an unexpected result an ``err`` of its own,
    up to the root, and say whether ``node`` holds one.

    Bublik's log tree marks a node by its ``err`` alone, so without this only
    the failing tests show; its NOK counts look at tests only.
    """
    if node["type"] != "pkg":
        return bool(node["err"])
    unexpected = [mark_unexpected_packages(child) for child in node["iters"]]
    if any(unexpected):
        node["err"] = UNEXPECTED
    return any(unexpected)


#: Bublik keeps a test's (and a package's) name in a varchar(64).
MAX_NAME = 64


def bublik_name(title: str) -> str:
    """A title as a Bublik test name: itself if it fits, else cut short
    with a hash of the whole, so two long titles never collide."""
    if len(title) <= MAX_NAME:
        return title
    digest = hashlib.md5(title.encode()).hexdigest()[:8]
    return title[: MAX_NAME - 10].rstrip() + "~" + digest


def _package(name: str, path: list[str], children: list[Node]) -> Node:
    title, name = name, bublik_name(name)
    return {
        "name": name,
        "type": "pkg",
        "params": {},
        "hash": "",
        "tin": -1,
        "reqs": [],
        "objective": title if name != title else "",
        "err": "",
        "obtained": {"result": {"status": "PASSED"}},
        "path": path,
        "path_str": "/".join(path),
        "iters": children,
        "_key": package_key(path),
    }


def _stamp(node: Node, start_ms: int, end_ms: int) -> None:
    for key, ms in (("start", start_ms), ("end", end_ms)):
        moment = datetime.fromtimestamp(ms / 1000, timezone.utc)
        node[f"{key}_ts"] = format_bublik_timestamp(moment)
        node[f"{key}_ts_utc"] = ms / 1000


def _place(node: Node, cursor: int, leaves: dict[int, Leaf]) -> int:
    leaf = leaves.get(id(node))
    if leaf is not None:
        start = max(cursor, leaf.raw_start_ms or cursor)
        length = 0
        if leaf.raw_start_ms is not None and leaf.raw_end_ms is not None:
            length = max(0, leaf.raw_end_ms - leaf.raw_start_ms)
        node["_shift_ms"] = start - (leaf.raw_start_ms or start)
        _stamp(node, start, start + length)
        return start + length
    start = None
    at = cursor
    for child in node["iters"]:
        at = _place(child, at, leaves)
        if start is None:
            start = int(child["start_ts_utc"] * 1000)
    _stamp(node, start if start is not None else cursor, at)
    return at


def _attachment_source(item: Attachment, attachments_dir: Path | None) -> Path | None:
    if item.path:
        marker = "test-output/"
        if attachments_dir is not None and marker in item.path:
            candidate = attachments_dir / item.path.split(marker, 1)[1]
            if candidate.is_file():
                return candidate
        if Path(item.path).is_file():
            return Path(item.path)
    return None


_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "video/webm": ".webm",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "application/json": ".json",
}


def write_attachments(
    conversion: Conversion,
    bundle_dir: Path,
    attachments_dir: Path | None,
    report_url: str,
) -> int:
    """Copy each test's attachments next to its ``attachments.json``.

    The last attempt comes first and keeps the files' own names; an earlier
    attempt's file of the same name is prefixed with its retry.
    """
    written = 0
    for test_id, leaf in conversion.leaves.items():
        entries: list[dict[str, Any]] = []
        used: set[str] = set()
        names = [item.name for attempt in leaf.attempts for item in attempt.attachments]
        node_dir = bundle_dir / "attachments" / f"node_id{test_id}"
        for attempt in reversed(leaf.attempts):
            for item in attempt.attachments:
                source = _attachment_source(item, attachments_dir)
                if source is None and not item.body:
                    console.print(
                        f"[yellow]warning:[/] attachment {item.path or item.name!r} "
                        f"of test {test_id} not found; skipped",
                        soft_wrap=True,
                    )
                    continue
                file_name = (
                    source.name
                    if source is not None
                    else f"{item.name}{_EXTENSIONS.get(item.content_type, '.bin')}"
                )
                if file_name in used:
                    file_name = f"retry{item.retry}-{file_name}"
                while file_name in used:
                    file_name = f"_{file_name}"
                used.add(file_name)
                node_dir.mkdir(parents=True, exist_ok=True)
                if source is not None:
                    shutil.copyfile(source, node_dir / file_name)
                else:
                    (node_dir / file_name).write_bytes(base64.b64decode(item.body))
                label = item.name
                if names.count(item.name) > 1:
                    label = f"{item.name} (retry {item.retry})"
                entries.append(
                    {
                        "type": "text",
                        "view_type": "inline",
                        "name": label,
                        "description": item.content_type,
                        "download_enabled": True,
                        "path": f"./{file_name}",
                    }
                )
        if entries:
            _write(
                node_dir / "attachments.json", {"version": 1, "attachments": entries}
            )
            written += len(entries)
    if report_url:
        _write(
            bundle_dir / "attachments" / "node_1_0" / "attachments.json",
            {
                "version": 1,
                "attachments": [
                    {
                        "type": "text",
                        "view_type": "inline",
                        "name": "Playwright report",
                        "download_enabled": False,
                        "uri": report_url,
                    }
                ],
            },
        )
    return written


def bublik_timestamp(moment: datetime) -> str:
    """START_TIMESTAMP's and FINISH_TIMESTAMP's spelling."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def run_cfg(facts: Facts, browser: str) -> str:
    return facts.cfg or facts.preview or browser


def metas(conversion: Conversion, facts: Facts) -> list[dict[str, str]]:
    root = conversion.root
    live = conversion.live
    start = bublik_timestamp(datetime.fromtimestamp(root["start_ts_utc"], timezone.utc))
    finish = datetime.fromtimestamp(root["end_ts_utc"], timezone.utc)
    return run_metas(
        facts,
        conversion.browser,
        start=live.start_timestamp if live else start,
        cfg=live.cfg if live else run_cfg(facts, conversion.browser),
        finish=bublik_timestamp(finish),
        # Bublik concludes run-error from RUN_STATUS ERROR whatever the share
        # of failures: its NOK borders round it to whole percents, so one
        # failure in a few hundred tests would pass for none.
        status="ERROR" if root["err"] else "DONE",
    )


def run_metas(
    facts: Facts,
    browser: str,
    *,
    start: str,
    cfg: str,
    status: str,
    finish: str | None = None,
) -> list[dict[str, str]]:
    """The run's metas; a live run in progress has no ``finish`` yet."""
    items: list[dict[str, str]] = [
        {"name": "PROJECT", "value": facts.project},
        {"name": "TS_NAME", "value": facts.ts_name},
        {"name": "CFG", "value": cfg},
        {"name": "START_TIMESTAMP", "value": start, "type": "timestamp"},
    ]
    if finish is not None:
        items.append({"name": "FINISH_TIMESTAMP", "value": finish, "type": "timestamp"})
    items += [
        {"name": "CAMPAIGN_DATE", "value": start[:10]},
        {"name": "RUN_STATUS", "value": status},
        {"name": _browser_key(browser).upper(), "value": browser},
    ]

    def label(name: str, value: str, type_: str | None = None) -> None:
        if value:
            item = {"name": name, "value": value}
            if type_:
                item["type"] = type_
            items.append(item)

    label("APP", facts.app)
    label("PREVIEW", facts.preview)
    label("PREVIEW_URL", facts.preview_url)
    label("PREVIEW_PAGE_URL", facts.preview_page_url)
    label("DEPLOYMENT", facts.deployment)
    label("DEPLOYMENT_URL", facts.deployment_url)
    label("TEST_RUN_URL", facts.test_run_url)
    for repo in (r.resolved() for r in facts.repos):
        key = repo.meta_key
        label(f"{key}_REV", repo.sha, "revision")
        label(f"{key}_BRANCH", repo.branch, "branch")
        label(f"{key}_COMMIT_URL", repo.commit_url)
        label(
            f"{key}_PR", repo.pr_url or (f"#{repo.pr_number}" if repo.pr_number else "")
        )
        label(f"{key}_REPOSITORY", repo.repository_url or repo.repository)
    return items


def tags(conversion: Conversion, facts: Facts) -> dict[str, str]:
    return run_tags(facts, conversion.browser)


def _browser_key(browser: str) -> str:
    """``browser`` for one browser, ``browsers`` for a comma-separated list."""
    return "browsers" if "," in browser else "browser"


def run_tags(facts: Facts, browser: str) -> dict[str, str]:
    out = {_browser_key(browser): browser}
    if facts.preview:
        out["preview"] = facts.preview
    if facts.app:
        out["app"] = facts.app
    for repo in (r.resolved() for r in facts.repos):
        if repo.pr_number:
            out[f"{repo.slot}_pr"] = repo.pr_number
    return out


def revisions(facts: Facts) -> dict[str, dict[str, str]]:
    """The project's REVISIONS entries that turn each ``<SLOT>_REV`` into a link."""
    out = {}
    for repo in (r.resolved() for r in facts.repos):
        web = repo.web_repository
        if repo.sha and web.startswith(("http://", "https://")):
            out[f"{repo.meta_key}_REV"] = {"name": repo.slot, "uri": web}
    return out


def _rows_for(conversion: Conversion):
    def rows_for(node: Node) -> list[Row] | None:
        if node["type"] != "test":
            return None
        leaf = conversion.leaves[node["test_id"]]
        status = node["obtained"]["result"]["status"]
        name = node["name"]
        rows: list[Row] = []
        start_ms = int(node["start_ts_utc"] * 1000)
        end_ms = int(node["end_ts_utc"] * 1000)
        shift = node.get("_shift_ms", 0)

        def at(ms: int) -> tuple[str, float]:
            ms = min(max(ms, start_ms), end_ms)
            moment = datetime.fromtimestamp(ms / 1000, timezone.utc)
            return format_bublik_timestamp(moment), ms / 1000

        def add(level: str, entity: str, user: str, ms: int, content: str) -> None:
            ts, utc = at(ms)
            rows.append(text_row(len(rows) + 1, level, entity, user, ts, utc, content))

        add("INFO", name, "TAPI Jumps", start_ms, "Main test entity")
        start_text = f"{name} start"
        if leaf.tags:
            start_text += "\nTags: " + " ".join(f"@{tag}" for tag in leaf.tags)
        add(status_level(status), name, "Step", start_ms, start_text)
        for attempt in leaf.attempts:
            ms = attempt.start_ms + shift if attempt.start_ms else start_ms
            attempt_status = RESULT_STATUS.get(attempt.status, "INCOMPLETE")
            add(
                status_level(attempt_status),
                name,
                "Self",
                ms,
                f"attempt {attempt.retry + 1}: {attempt.status} "
                f"({attempt.duration_ms / 1000:.1f} s)",
            )
            for depth, title, failed in attempt.steps:
                add(
                    "ERROR" if failed else "INFO",
                    name,
                    "Step",
                    ms,
                    "  " * depth + title,
                )
            for message in attempt.errors:
                add("ERROR", name, "Verdict", ms, strip_ansi(message))
            for item in attempt.attachments:
                add(
                    "INFO",
                    name,
                    "Artifact",
                    ms,
                    f"{item.name}: {Path(item.path).name or item.content_type}",
                )
        add(
            status_level(status),
            "Tester",
            "Run",
            end_ms,
            f"Obtained result is:\n{status}",
        )
        return rows

    return rows_for


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")


def _strip_private(node: Node) -> None:
    node.pop("_shift_ms", None)
    node.pop("_key", None)
    node.pop("_rank", None)
    for child in node.get("iters") or []:
        _strip_private(child)


@dataclass
class Written:
    path: Path
    tests: int
    unexpected: int
    attachments: int


def write_bundle(
    report: dict[str, Any],
    facts: Facts,
    bundle_dir: Path,
    *,
    attachments_dir: Path | None = None,
    schemas: Path | None = SCHEMA_DIR,
    live: LiveInfo | None = None,
) -> Written:
    """Write the bundle into ``bundle_dir`` (replacing it), validated, ``.done`` last."""
    conversion = convert(report, facts, live)
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    write_json_logs(
        conversion.root, bundle_dir / "json", rows_for=_rows_for(conversion)
    )
    attachments = write_attachments(
        conversion, bundle_dir, attachments_dir, facts.report_url
    )
    _strip_private(conversion.root)
    root = conversion.root
    _write(
        bundle_dir / "meta_data.json", {"version": 1, "metas": metas(conversion, facts)}
    )
    _write(
        bundle_dir / "bublik.json",
        {
            "plan": conversion.plan,
            "iters": [root],
            "tags": tags(conversion, facts),
            "start_ts": root["start_ts"],
            "start_ts_utc": root["start_ts_utc"],
            "end_ts": root["end_ts"],
            "end_ts_utc": root["end_ts_utc"],
        },
    )
    if schemas is not None:
        run_log_schema = schemas / "run_log.json"
        meta_data_schema = schemas / "meta_data.json"
        validate_run_log(
            bundle_dir / "bublik.json",
            run_log_schema,
            load_run_log_validator(run_log_schema),
        )
        validate_meta_data(
            bundle_dir / "meta_data.json",
            meta_data_schema,
            load_meta_data_validator(meta_data_schema),
        )
    (bundle_dir / RUN_COMPLETE_FILE).write_text("")
    tests = conversion.tests()
    return Written(
        path=bundle_dir,
        tests=len(tests),
        unexpected=sum(1 for node in tests if node["err"]),
        attachments=attachments,
    )
