#!/usr/bin/env python3

"""Generate, publish, and import deterministic Bublik fixture runs.

Installed as the ``bublik-e2e`` command (``bublik-e2e <command> [options]``).
The tool is instance-agnostic: it targets any Bublik instance through ``--url``
and the admin credentials.

Bundled fixtures: basic, dpdk-ethdev-ts, net-drv-ts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Annotated, Callable, List, Optional

import typer
from rich.table import Table

from core.bundle_import import import_bundle
from core.classification import build_classification
from core.common import CliError, console
from core.playwright_bundle import (
    SCHEMA_DIR,
    LiveInfo,
    facts_from_env,
    revisions,
    write_bundle,
)
from core.playwright_live import LiveStream, run_live, write_reporter
from core.settings import Settings
from core.discovery import known_fixture_names, selected_fixtures
from core.importer import generate_and_import, import_manifest
from core.live import simulate_live_run
from core.manifest import generate_manifest
from core.manifest_models import manifest_json_schema
from core.plan_models import plan_json_schema
from core.plan_file import load_plan
from core.trackers import project_trackers
from core.planning import build_mixes, build_plan

app = typer.Typer(
    rich_markup_mode="rich",
    no_args_is_help=True,
    add_completion=False,
    help=__doc__,
)

# ---------------------------------------------------------------------------
# Option type aliases, grouped the same way as the old add_*_arguments helpers.
# Each command composes the groups it needs; defaults are set at the call site
# so a single alias can serve both required and optional positions.
# ---------------------------------------------------------------------------

# Connection
UrlOpt = Annotated[
    Optional[str],
    typer.Option(
        help="Base URL of the target Bublik instance "
        "(overrides BUBLIK_FQDN/BUBLIK_DOCKER_PROXY_PORT/URL_PREFIX).",
    ),
]
EnvFileOpt = Annotated[
    Optional[Path],
    typer.Option(help="Optional .env file to seed environment values from."),
]
ManifestOpt = Annotated[
    Optional[Path],
    typer.Option(help="Manifest file path (default: ./.e2e/e2e-manifest.json)."),
]

# Generation
FixtureOpt = Annotated[
    Optional[List[str]],
    typer.Option(
        metavar="DIR",
        help="Fixture provider directory (containing fixture.py). Repeatable. "
        "Omit to auto-discover all bundled providers.",
    ),
]
RunsOpt = Annotated[
    Optional[int],
    typer.Option(
        help="Total number of runs to produce. Optional with --day (derived "
        "from the day specs; if given, asserted to match). Required with --fill, "
        "where it drives the loop count.",
    ),
]
DayOpt = Annotated[
    Optional[List[str]],
    typer.Option(
        metavar="YYYY-MM-DD:SPEC",
        help="Runs for one date. SPEC is a comma list of "
        "\\[fixture.]conclusion\\[@mix]\\[+ui]=count. A trailing +ui marks the "
        "runs for import through the UI (Playwright) instead of the CLI API "
        "import. Conclusions: ok, nok-warning, "
        "nok-error, warning, error, running, busy, stopped, interrupted, "
        "compromised. An unprefixed conclusion applies to every discovered "
        "fixture; prefix with a fixture name (e.g. dpdk-ethdev-ts.ok) to scope "
        "it. @mix is either a named --mix or an inline definition "
        "key=val;key=val (e.g. @unexpectedFailed=20%;unexpectedSkipped=5%). "
        "Repeatable. Mutually exclusive with --fill/--dates.",
    ),
]
FillOpt = Annotated[
    Optional[str],
    typer.Option(
        metavar="CONCLUSION",
        help="Generate --runs runs of this conclusion, round-robining over "
        "fixtures and --dates. Requires --dates; excludes --day.",
    ),
]
DatesOpt = Annotated[
    Optional[str],
    typer.Option(
        metavar="YYYY-MM-DD[..YYYY-MM-DD]",
        help="Single date or inclusive range used by --fill.",
    ),
]
PlanOpt = Annotated[
    Optional[Path],
    typer.Option(
        metavar="FILE",
        help="JSON plan file holding the campaign: "
        '{"version": 1, "runs": N, "mixes": {...}, "days": {...}}. Equivalent to '
        "the matching --runs/--mix/--day options, and validated the same way. "
        "Day and mix specs may be one string or a list of strings. Mutually "
        "exclusive with --day/--fill; extra --mix options are merged in.",
    ),
]
MixOpt = Annotated[
    Optional[List[str]],
    typer.Option(
        metavar="NAME:k=v,...",
        help="Define a named result mix, e.g. "
        "'warning-mix:unexpectedFailed=20%,unexpectedSkipped=5%', then reference "
        "it from a --day spec with @warning-mix. Repeatable. (For a one-off, "
        "skip this and inline the mix on the --day spec.)",
    ),
]
PublishDirOpt = Annotated[
    Optional[Path],
    typer.Option(
        help="Full path bundles are written to; the instance must serve it at "
        "{url}/logs/<name>/ (its basename becomes the URL segment). Required "
        "unless BUBLIK_E2E_PUBLISH_DIR is set.",
    ),
]
PrettyOpt = Annotated[
    bool,
    typer.Option(help="Pretty-print generated JSON (indented, sorted keys)."),
]
RunLogSchemaOpt = Annotated[
    Optional[Path],
    typer.Option(
        help="Draft 7 JSON Schema used to validate each finalized bublik.json. "
        "Overrides BUBLIK_E2E_RUN_LOG_SCHEMA; otherwise derived from "
        "BUBLIK_DJANGO_ROOT."
    ),
]
MetaDataSchemaOpt = Annotated[
    Optional[Path],
    typer.Option(
        "--meta-data-schema",
        help="Draft 7 JSON Schema used to validate each finalized meta_data.json. "
        "Overrides BUBLIK_E2E_META_DATA_SCHEMA; otherwise derived from "
        "BUBLIK_DJANGO_ROOT.",
    ),
]

# Auth
EmailOpt = Annotated[
    Optional[str],
    typer.Option(help="Admin email for API login (overrides DJANGO_SUPERUSER_EMAIL)."),
]
PasswordOpt = Annotated[
    Optional[str],
    typer.Option(
        help="Admin password for API login (overrides DJANGO_SUPERUSER_PASSWORD).",
    ),
]
SetupProjectsOpt = Annotated[
    bool,
    typer.Option(
        help="Create any missing projects and the per-project 'references', "
        "'meta', and 'per_conf' configs before importing.",
    ),
]
SetupClassificationOpt = Annotated[
    bool,
    typer.Option(
        "--setup-classification",
        help="After importing, create the plan's classification issues and "
        "rules through the API, so a later import proves they apply to runs "
        "that did not exist when the rule was written. Off by default: the "
        "same triage can be done by hand in the UI, and the plan's pinned "
        "results are generated either way.",
    ),
]
TimeoutOpt = Annotated[
    int,
    typer.Option(
        help="Seconds to wait with no import progress before giving up. The "
        "deadline resets on each completed or advancing run, so large batches "
        "that keep moving never time out."
    ),
]
IncludeUiOpt = Annotated[
    bool,
    typer.Option(
        "--include-ui",
        help="Also import bundles marked importVia=ui (normally left for the "
        "Playwright suite to import through the UI).",
    ),
]


GENERATE_EPILOG = """[bold]Examples[/]

[dim]These examples assume BUBLIK_DJANGO_ROOT points to the Bublik Django repository.[/]

[dim]Per-fixture day spec (run count is derived, no --runs needed):[/]

[cyan]bublik-e2e generate --day "2026-04-21:basic.ok=2,dpdk-ethdev-ts.nok-warning=1" --publish-dir /srv/logs/e2e[/]

[dim]Unprefixed conclusions apply to every discovered fixture:[/]

[cyan]bublik-e2e generate --day "2026-04-21:ok=1,warning=1,error=1" --publish-dir /srv/logs/e2e[/]

[dim]Inline mix on a day spec (no pre-defined --mix):[/]

[cyan]bublik-e2e generate --day "2026-04-21:net-drv-ts.nok-warning@unexpectedFailed=20%;unexpectedSkipped=5%=2" --publish-dir /srv/logs/e2e[/]

[dim]Named mix reused across day specs:[/]

[cyan]bublik-e2e generate --mix "warning-mix:unexpectedFailed=20%,unexpectedSkipped=5%" --day "2026-04-21:dpdk-ethdev-ts.nok-warning@warning-mix=2" --publish-dir /srv/logs/e2e[/]

[dim]Fill a whole month with one conclusion (--runs drives the loop):[/]

[cyan]bublik-e2e generate --runs 100 --fill ok --dates "2026-04-01..2026-04-30" --publish-dir /srv/logs/e2e[/]
"""

IMPORT_EPILOG = """[bold]Examples[/]

[dim]Import the default manifest, creating any missing projects first:[/]

[cyan]bublik-e2e import --url http://localhost --setup-projects[/]

[dim]Import a specific manifest against a remote instance:[/]

[cyan]bublik-e2e import --manifest ./.e2e/e2e-manifest.json --url https://bublik.example.com --email admin@bublik.com --password admin[/]
"""

RUN_EPILOG = """[bold]Examples[/]

[dim]These examples assume BUBLIK_DJANGO_ROOT points to the Bublik Django repository.[/]

[dim]Per-fixture day spec (run count is derived, no --runs needed):[/]

[cyan]bublik-e2e run --day "2026-04-21:basic.ok=2,dpdk-ethdev-ts.nok-warning=1" --publish-dir /srv/logs/e2e --url http://localhost[/]

[dim]Inline mix on a day spec (no pre-defined --mix):[/]

[cyan]bublik-e2e run --day "2026-04-21:net-drv-ts.nok-warning@unexpectedFailed=20%;unexpectedSkipped=5%=2" --publish-dir /srv/logs/e2e --url http://localhost[/]

[dim]Multi-day campaign across fixtures, creating projects on the way in:[/]

[cyan]bublik-e2e run --setup-projects --day "2026-04-21:basic.ok=2,basic.nok-error=1" --day "2026-04-22:dpdk-ethdev-ts.ok=2,dpdk-ethdev-ts.nok-warning=1" --publish-dir /srv/logs/e2e --url http://localhost[/]

[dim]Fill a whole month with one conclusion (--runs drives the loop):[/]

[cyan]bublik-e2e run --runs 100 --fill ok --dates "2026-04-01..2026-04-30" --setup-projects --publish-dir /srv/logs/e2e --url http://localhost[/]
"""


def _dispatch(func: Callable[[argparse.Namespace], None], **params: object) -> None:
    """Hand a real argparse.Namespace to a core entry point and surface errors."""
    try:
        func(argparse.Namespace(**params))
    except CliError as exc:
        console.print(f"[bold red]error:[/] {exc}", soft_wrap=True)
        raise typer.Exit(code=1)


def _plan_or_exit(
    plan: Optional[Path],
    runs: Optional[int],
    day: Optional[List[str]],
    fill: Optional[str],
    mix: Optional[List[str]],
) -> tuple[Optional[int], List[str], List[str], Optional[dict], dict]:
    """Fold a --plan file into the (runs, day, mix, classification, trackers)
    the core expects."""
    if plan is None:
        return runs, day or [], mix or [], None, {}
    try:
        if day:
            raise CliError("--plan and --day are mutually exclusive")
        if fill:
            raise CliError("--plan and --fill are mutually exclusive")
        loaded = load_plan(plan)
    except CliError as exc:
        console.print(f"[bold red]error:[/] {exc}", soft_wrap=True)
        raise typer.Exit(code=1)
    # An explicit --runs still wins, so a plan can be spot-checked from the CLI;
    # extra --mix definitions are additive and may override a plan's mix.
    return (
        runs if runs is not None else loaded.runs,
        loaded.day_options(),
        loaded.mix_options() + (mix or []),
        loaded.classification_spec(),
        loaded.tracker_spec(),
    )


@app.command(epilog=GENERATE_EPILOG)
def generate(
    runs: RunsOpt = None,
    fixture: FixtureOpt = None,
    plan: PlanOpt = None,
    day: DayOpt = None,
    fill: FillOpt = None,
    dates: DatesOpt = None,
    mix: MixOpt = None,
    publish_dir: PublishDirOpt = None,
    pretty: PrettyOpt = False,
    run_log_schema: RunLogSchemaOpt = None,
    meta_data_schema: MetaDataSchemaOpt = None,
    url: UrlOpt = None,
    env_file: EnvFileOpt = None,
    manifest: ManifestOpt = None,
) -> None:
    """Generate bundles into --publish-dir and write the manifest. No import."""
    runs, day, mix, classification, trackers = _plan_or_exit(plan, runs, day, fill, mix)
    _dispatch(
        generate_manifest,
        url=url,
        env_file=env_file,
        manifest=manifest,
        fixture=fixture or [],
        runs=runs,
        day=day,
        fill=fill,
        dates=dates,
        mix=mix,
        publish_dir=publish_dir,
        pretty=pretty,
        run_log_schema=run_log_schema,
        meta_data_schema=meta_data_schema,
        classification=classification,
        trackers=trackers,
    )


@app.command(name="import", epilog=IMPORT_EPILOG)
def import_(
    url: UrlOpt = None,
    env_file: EnvFileOpt = None,
    manifest: ManifestOpt = None,
    email: EmailOpt = None,
    password: PasswordOpt = None,
    setup_projects: SetupProjectsOpt = False,
    setup_classification: SetupClassificationOpt = False,
    timeout: TimeoutOpt = 600,
    include_ui: IncludeUiOpt = False,
) -> None:
    """Import an existing manifest into the instance via the API."""
    _dispatch(
        import_manifest,
        url=url,
        env_file=env_file,
        manifest=manifest,
        email=email,
        password=password,
        setup_projects=setup_projects,
        setup_classification=setup_classification,
        timeout=timeout,
        include_ui=include_ui,
    )


@app.command(epilog=RUN_EPILOG)
def run(
    runs: RunsOpt = None,
    fixture: FixtureOpt = None,
    plan: PlanOpt = None,
    day: DayOpt = None,
    fill: FillOpt = None,
    dates: DatesOpt = None,
    mix: MixOpt = None,
    publish_dir: PublishDirOpt = None,
    pretty: PrettyOpt = False,
    run_log_schema: RunLogSchemaOpt = None,
    meta_data_schema: MetaDataSchemaOpt = None,
    url: UrlOpt = None,
    env_file: EnvFileOpt = None,
    manifest: ManifestOpt = None,
    email: EmailOpt = None,
    password: PasswordOpt = None,
    setup_projects: SetupProjectsOpt = False,
    setup_classification: SetupClassificationOpt = False,
    timeout: TimeoutOpt = 600,
    include_ui: IncludeUiOpt = False,
) -> None:
    """Generate bundles and import them in one shot (generate + import)."""
    runs, day, mix, classification, trackers = _plan_or_exit(plan, runs, day, fill, mix)
    _dispatch(
        generate_and_import,
        url=url,
        env_file=env_file,
        manifest=manifest,
        fixture=fixture or [],
        runs=runs,
        day=day,
        fill=fill,
        dates=dates,
        mix=mix,
        publish_dir=publish_dir,
        pretty=pretty,
        run_log_schema=run_log_schema,
        meta_data_schema=meta_data_schema,
        email=email,
        password=password,
        setup_projects=setup_projects,
        setup_classification=setup_classification,
        timeout=timeout,
        include_ui=include_ui,
        classification=classification,
        trackers=trackers,
    )


LIVE_EPILOG = """[bold]Examples[/]

[dim]Stream a run live at 5x speed and watch it fill in, creating the project first:[/]

[cyan]bublik-e2e live net-drv-ts --speed 5 --setup-projects --url http://localhost[/]

[dim]Crash mid-run: stop after 30% of the tests and leave the run RUNNING:[/]

[cyan]bublik-e2e live net-drv-ts --stop-after 30% --url http://localhost[/]

[dim]Lose 10% of the tests' events so Bublik records LOST results:[/]

[cyan]bublik-e2e live dpdk-ethdev-ts --drop-rate 10 --seed 1 --url http://localhost[/]

[dim]Stream live, then publish the bundle and source-import it (the real TE flow):[/]

[cyan]bublik-e2e live basic --then-import --publish-dir /srv/logs/e2e --url http://localhost[/]

[dim]Print the init/feed/finish payloads without contacting Bublik:[/]

[cyan]bublik-e2e live basic --dry-run --output live.json[/]
"""


@app.command(epilog=LIVE_EPILOG)
def live(
    fixture: Annotated[
        str,
        typer.Argument(
            metavar="FIXTURE",
            help="Bundled fixture name (basic, dpdk-ethdev-ts, net-drv-ts) or a "
            "fixture provider directory (containing fixture.py).",
        ),
    ],
    conclusion: Annotated[
        str,
        typer.Option(help="Conclusion whose result mix the streamed run gets."),
    ] = "ok",
    mix: Annotated[
        Optional[str],
        typer.Option(
            metavar="k=v,...",
            help="Inline result mix, e.g. 'unexpectedFailed=20%,unexpectedSkipped=5%'.",
        ),
    ] = None,
    speed: Annotated[
        float,
        typer.Option(
            help="Replay speed relative to the fixture's own timeline "
            "(synthetic fixtures take about 3s per test; 10 is ten times faster).",
        ),
    ] = 1.0,
    batch_interval: Annotated[
        float,
        typer.Option(help="Seconds between feed requests (events are batched)."),
    ] = 1.0,
    stop_after: Annotated[
        Optional[str],
        typer.Option(
            metavar="N|N%",
            help="Stop after N tests (or N% of them) without finishing, leaving "
            "the run RUNNING like a crashed TE.",
        ),
    ] = None,
    drop_rate: Annotated[
        Optional[float],
        typer.Option(
            metavar="PERCENT",
            help="Drop the events of this percentage of tests; Bublik fills the "
            "gaps with LOST results.",
        ),
    ] = None,
    seed: Annotated[
        int, typer.Option(help="Seed choosing which tests --drop-rate drops.")
    ] = 0,
    then_import: Annotated[
        bool,
        typer.Option(
            help="After finishing, publish the bundle and source-import it, "
            "replacing the live data like TE publishing its logs. Needs "
            "--publish-dir.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Print the init/feed/finish payloads as JSON instead of streaming."
        ),
    ] = False,
    output: Annotated[
        Optional[Path],
        typer.Option(help="With --dry-run, write the payloads to this file."),
    ] = None,
    publish_dir: PublishDirOpt = None,
    pretty: PrettyOpt = False,
    run_log_schema: RunLogSchemaOpt = None,
    meta_data_schema: MetaDataSchemaOpt = None,
    url: UrlOpt = None,
    env_file: EnvFileOpt = None,
    email: EmailOpt = None,
    password: PasswordOpt = None,
    setup_projects: SetupProjectsOpt = False,
    timeout: TimeoutOpt = 600,
) -> None:
    """Stream one fixture run into the instance over the TE live-import API."""
    _dispatch(
        simulate_live_run,
        fixture=fixture,
        conclusion=conclusion,
        mix=mix,
        speed=speed,
        batch_interval=batch_interval,
        stop_after=stop_after,
        drop_rate=drop_rate,
        seed=seed,
        then_import=then_import,
        dry_run=dry_run,
        output=output,
        publish_dir=publish_dir,
        pretty=pretty,
        run_log_schema=run_log_schema,
        meta_data_schema=meta_data_schema,
        url=url,
        env_file=env_file,
        email=email,
        password=password,
        setup_projects=setup_projects,
        timeout=timeout,
    )


PLAN_EPILOG = """[bold]Examples[/]

[dim]Validate the versioned campaign and see what it expands to:[/]

[cyan]bublik-e2e plan --plan e2e/plan.json[/]

[dim]Break the summary down by fixture instead of by date:[/]

[cyan]bublik-e2e plan --plan e2e/plan.json --by fixture[/]
"""


@app.command(epilog=PLAN_EPILOG)
def plan(
    plan: PlanOpt = None,
    fixture: FixtureOpt = None,
    day: DayOpt = None,
    mix: MixOpt = None,
    runs: RunsOpt = None,
    by: Annotated[
        str,
        typer.Option(help="Group the summary by 'date', 'fixture' or 'conclusion'."),
    ] = "date",
) -> None:
    """Expand a plan and print what it would generate. Nothing is written."""
    groups = {"date", "fixture", "conclusion"}
    if by not in groups:
        console.print(
            f"[bold red]error:[/] --by must be one of: {', '.join(sorted(groups))}"
        )
        raise typer.Exit(code=1)
    runs, day, mix, classification, trackers = _plan_or_exit(plan, runs, day, None, mix)
    try:
        args = argparse.Namespace(
            fixture=fixture or [], runs=runs, day=day, fill=None, dates=None, mix=mix
        )
        fixtures = selected_fixtures(args)
        mixes = build_mixes(args)
        planned, empty_dates = build_plan(args, fixtures, mixes)
        build_classification(classification, known_fixture_names(fixtures))
        project_trackers(fixtures, trackers)
    except CliError as exc:
        console.print(f"[bold red]error:[/] {exc}", soft_wrap=True)
        raise typer.Exit(code=1)

    key = {
        "date": lambda run: run.run_date,
        "fixture": lambda run: run.fixture.name,
        "conclusion": lambda run: run.conclusion,
    }[by]
    table = Table(title=f"{len(planned)} runs by {by}", title_justify="left")
    table.add_column(by.capitalize())
    table.add_column("Runs", justify="right")
    table.add_column("Via UI", justify="right")
    for value in sorted({key(run) for run in planned}):
        rows = [run for run in planned if key(run) == value]
        via_ui = sum(1 for run in rows if run.import_via == "ui")
        table.add_row(value, str(len(rows)), str(via_ui) if via_ui else "-")
    console.print(table)
    console.print(
        f"{len(planned)} runs, "
        f"{len({run.run_date for run in planned})} dates with runs, "
        f"{len(empty_dates)} empty, "
        f"{sum(1 for run in planned if run.import_via == 'ui')} imported through the UI"
    )


PLAYWRIGHT_EPILOG = """[bold]Examples[/]

[dim]At the end of a bublik-preview check Job: the bundle in the tested preview's own log folder,
imported into the Bublik E2E_LIVE_URL names, merged into the run playwright-live streamed:[/]

[cyan]bublik-e2e playwright --results "$out/results.json" --attachments-dir "$out/test-output" \\
  --publish-dir "$PREVIEW_DIR/data/logs/logs/e2e-results" --bundle-url "$PREVIEW_FQDN/logs/e2e-results" \\
  --bundle-id "$PREVIEW_NAME-$PREVIEW_DEPLOYMENT-$CHECK_RUN" --live "$JOB_ARTIFACTS/live.json" \\
  --import --url "$E2E_LIVE_URL" --email "$E2E_LIVE_EMAIL" --password "$E2E_LIVE_PASSWORD" --setup-project[/]

[dim]Into the instance whose log folder the bundle is written to:[/]

[cyan]bublik-e2e playwright --results results.json --publish-dir /srv/logs/e2e-results --bundle-id run-1 --import --url http://localhost[/]
"""


@app.command(epilog=PLAYWRIGHT_EPILOG)
def playwright(
    results: Annotated[
        Path, typer.Option(help="Playwright JSON report (the json reporter's file).")
    ],
    publish_dir: Annotated[
        Path,
        typer.Option(
            help="Where the bundle is written: <publish-dir>/<bundle-id>/. Without "
            "--bundle-url, the directory the instance serves at {url}/logs/<name>/."
        ),
    ],
    bundle_id: Annotated[str, typer.Option(help="The bundle's directory name.")],
    bundle_url: Annotated[
        Optional[str],
        typer.Option(
            help="Where the instance fetches the bundle from: <bundle-url>/<bundle-id>/ "
            "(default: {url}/logs/<publish-dir name>). A preview's own log folder, "
            "say, when the bundle lies there and the instance is another. With "
            "--setup-project it joins the project's logs bases."
        ),
    ] = None,
    attachments_dir: Annotated[
        Optional[Path],
        typer.Option(
            help="Where the report's test-output/ was copied to; attachment paths "
            "are looked up under it."
        ),
    ] = None,
    facts_env: Annotated[
        str,
        typer.Option(
            help="Prefix of the environment variables the facts are read from "
            "(PREVIEW_NAME, PREVIEW_SOURCE_<SLOT>_SHA, JOB_BROWSER, ...); the "
            "Job's own by default."
        ),
    ] = "",
    browser: Annotated[
        Optional[str],
        typer.Option(
            help="Playwright projects to convert, comma-separated, each a browser "
            "(default: JOB_BROWSER, else E2E_BROWSERS, else every project but the "
            "setup ones)."
        ),
    ] = None,
    project: Annotated[
        str, typer.Option(help="Bublik project the run belongs to.")
    ] = "bublik-preview",
    ts_name: Annotated[str, typer.Option(help="The run's TS_NAME.")] = "playwright",
    cfg: Annotated[
        Optional[str],
        typer.Option(help="The run's CFG (default: the preview's name)."),
    ] = None,
    app_name: Annotated[
        Optional[str], typer.Option(help="App name (default: the APP fact).")
    ] = None,
    do_import: Annotated[
        bool, typer.Option("--import", help="Import the bundle once written.")
    ] = False,
    url: UrlOpt = None,
    email: EmailOpt = None,
    password: PasswordOpt = None,
    setup_project: Annotated[
        bool,
        typer.Option(
            help="Create the project if missing, merge its logs base and revision "
            "links into 'references', and add 'per_conf' and 'meta' when absent."
        ),
    ] = False,
    timeout: Annotated[
        int, typer.Option(help="Seconds to wait for the import to finish.")
    ] = 600,
    summary: Annotated[
        Optional[Path],
        typer.Option(help='Write {"links": [...]} with the run (or bundle) URL here.'),
    ] = None,
    schemas: Annotated[
        Optional[Path],
        typer.Option(
            help="Directory with Bublik's run_log.json and meta_data.json "
            "(default: the copies bundled with this tool)."
        ),
    ] = None,
    no_validate: Annotated[
        bool, typer.Option("--no-validate", help="Skip schema validation.")
    ] = False,
    live: Annotated[
        Optional[Path],
        typer.Option(
            help="The file 'playwright-live --out' wrote: the bundle reuses the "
            "live run's key metas and numbering, so its import merges into it. "
            "A missing file, or one with no live run, imports afresh."
        ),
    ] = None,
) -> None:
    """Turn a Playwright JSON report into a Bublik run bundle, and import it."""
    try:
        live_info = LiveInfo.load(live) if live is not None and live.exists() else None
        if live_info is not None:
            console.print(f"merging into live run {live_info.run}", soft_wrap=True)
        facts = facts_from_env(os.environ, facts_env)
        facts.project = project
        facts.ts_name = ts_name
        if cfg:
            facts.cfg = cfg
        if browser:
            facts.browser = browser
        if app_name:
            facts.app = app_name
        try:
            report = json.loads(results.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CliError(f"cannot read {results}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CliError(f"{results} is not JSON: {exc}") from exc
        written = write_bundle(
            report,
            facts,
            publish_dir / bundle_id,
            attachments_dir=attachments_dir,
            schemas=None if no_validate else (schemas or SCHEMA_DIR),
            live=live_info,
        )
        console.print(
            f"[green]✓[/] bundle {written.path}: {written.tests} tests, "
            f"{written.unexpected} unexpected, {written.attachments} attachments",
            soft_wrap=True,
        )
        settings = Settings.from_args(
            argparse.Namespace(url=url, email=email, password=password)
        )
        base = (
            bundle_url.rstrip("/")
            if bundle_url
            else f"{settings.logs_base_url}/{publish_dir.name}"
        )
        import_url = f"{base}/{bundle_id}/"
        link = {"title": "Bundle", "url": import_url}
        if do_import:
            run_id = import_bundle(
                settings,
                import_url,
                project,
                setup_project=setup_project,
                revisions=revisions(facts),
                timeout=timeout,
                logs_bases=[base + "/"] if bundle_url else [],
            )
            run_url = f"{settings.ui_base_url}/runs/{run_id}"
            link = {"title": "Bublik run", "url": run_url}
            print(run_url)
        if summary is not None:
            summary.parent.mkdir(parents=True, exist_ok=True)
            summary.write_text(json.dumps({"links": [link]}) + "\n", encoding="utf-8")
    except CliError as exc:
        console.print(f"[bold red]error:[/] {exc}", soft_wrap=True)
        raise typer.Exit(code=1)


PLAYWRIGHT_LIVE_EPILOG = """[bold]Examples[/]

In the test Job: write the reporter, stream in the background, run the suite
with the reporter, then stop the stream (it reads what is left first):

[cyan]bublik-e2e playwright-live --write-reporter /work/bublik-live.mjs[/]
[cyan]BUBLIK_LIVE_EVENTS=$JOB_ARTIFACTS/live-events.ndjson; export BUBLIK_LIVE_EVENTS[/]
[cyan]bublik-e2e playwright-live --events "$BUBLIK_LIVE_EVENTS" --url "$E2E_LIVE_URL" \\
  --out "$JOB_ARTIFACTS/live.json" & live=$![/]
[cyan]pnpm exec playwright test --reporter=list,html,json,/work/bublik-live.mjs ...[/]
[cyan]kill -TERM $live; wait $live[/]
"""


@app.command("playwright-live", epilog=PLAYWRIGHT_LIVE_EPILOG)
def playwright_live(
    write_reporter_to: Annotated[
        Optional[Path],
        typer.Option(
            "--write-reporter",
            help="Write the Playwright reporter (an .mjs file) here and exit.",
        ),
    ] = None,
    events: Annotated[
        Optional[Path],
        typer.Option(help="The JSON lines the reporter writes (BUBLIK_LIVE_EVENTS)."),
    ] = None,
    url: Annotated[
        Optional[str],
        typer.Option(help="The Bublik to stream into, e.g. https://bublik.example."),
    ] = None,
    out: Annotated[
        Optional[Path],
        typer.Option(
            help="Where to write the live run's key metas and numbering, for "
            "'playwright --live'."
        ),
    ] = None,
    facts_env: Annotated[
        str,
        typer.Option(
            help="Prefix of the facts' environment variables (default: none, the "
            "test Job's own)."
        ),
    ] = "",
    browser: Annotated[
        Optional[str],
        typer.Option(
            help="Playwright projects streamed, comma-separated (default: JOB_BROWSER, "
            "else E2E_BROWSERS, else every project the reporter reports)."
        ),
    ] = None,
    project: Annotated[
        str, typer.Option(help="Bublik project the run belongs to.")
    ] = "bublik-preview",
    ts_name: Annotated[str, typer.Option(help="The run's TS_NAME.")] = "playwright",
    cfg: Annotated[
        Optional[str],
        typer.Option(help="The run's CFG (default: the preview's name)."),
    ] = None,
    app_name: Annotated[
        Optional[str], typer.Option(help="App name (default: the APP fact).")
    ] = None,
    interval: Annotated[
        int, typer.Option(help="Heartbeat interval announced to Bublik, seconds.")
    ] = 1,
    flush_every: Annotated[
        float, typer.Option(help="Seconds between feed posts at most.")
    ] = 2.0,
    idle_timeout: Annotated[
        float,
        typer.Option(help="Give up after this many seconds without an event; 0 never."),
    ] = 0.0,
) -> None:
    """Stream a running Playwright suite into Bublik as a live run.

    It never fails the job: when Bublik cannot be reached, it says so, keeps
    reading, and writes a file that makes 'playwright --live' import afresh.
    """
    if write_reporter_to is not None:
        print(str(write_reporter(write_reporter_to)))
        return
    if events is None or not url or out is None:
        console.print("[bold red]error:[/] pass --events, --url and --out")
        raise typer.Exit(code=1)
    facts = facts_from_env(os.environ, facts_env)
    facts.project = project
    facts.ts_name = ts_name
    if cfg:
        facts.cfg = cfg
    if browser:
        facts.browser = browser
    if app_name:
        facts.app = app_name
    stream = LiveStream(
        url, facts, browser=facts.browser, interval=interval, flush_every=flush_every
    )
    try:
        record = run_live(events, out, stream, idle_timeout=idle_timeout)
    except CliError as exc:
        console.print(f"[yellow]warning:[/] live import: {exc}", soft_wrap=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"run": None}) + "\n", encoding="utf-8")
        return
    if record.get("run") is not None:
        print(f"{url.rstrip('/')}/v2/runs/{record['run']}")


@app.command()
def schema(
    out: Annotated[
        Optional[Path],
        typer.Option(help="Write the schema to this file instead of stdout."),
    ] = None,
    kind: Annotated[
        str,
        typer.Option(
            help="Which schema to print: 'manifest' (default, drives the UI's "
            "TypeScript codegen) or 'plan' (drives editor completion and "
            "validation of plan.yaml)."
        ),
    ] = "manifest",
) -> None:
    """Print a JSON Schema (manifest by default; --kind plan for plan files)."""
    builders = {"manifest": manifest_json_schema, "plan": plan_json_schema}
    if kind not in builders:
        console.print(
            f"[bold red]error:[/] --kind must be one of: {', '.join(sorted(builders))}"
        )
        raise typer.Exit(code=1)
    rendered = json.dumps(builders[kind](), indent=2, sort_keys=True) + "\n"
    if out is None:
        print(rendered, end="")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(str(out))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
