"""Import one published bundle into a Bublik instance through its API."""

from __future__ import annotations

import shutil
import tempfile
import time as time_module
from pathlib import Path
from typing import Any

from core.common import CliError, normalize_url
from core.importer import (
    curl_json,
    find_existing_run_id,
    login,
    meta_config,
    per_conf_config,
    schedule_import,
    upsert_config,
)
from core.settings import Settings

LOGS_BASE_NAME = "Published runs"


def ensure_project(base_url: str, cookie_jar: Path, name: str, create: bool) -> int:
    projects = curl_json(f"{base_url}/api/v2/projects/", cookie_jar=cookie_jar)
    for project in projects or []:
        if project.get("name") == name:
            return int(project["id"])
    if not create:
        raise CliError(
            f"project {name!r} does not exist in {base_url}; "
            "pass --setup-project to create it"
        )
    created = curl_json(
        f"{base_url}/api/v2/projects/",
        method="POST",
        payload={"name": name},
        cookie_jar=cookie_jar,
    )
    return int(created["id"])


def merged_references(
    current: dict[str, Any], logs_base: str, revisions: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """``current`` with the logs base and revision links added; nothing removed."""
    merged = dict(current)
    bases = [dict(base) for base in merged.get("LOGS_BASES") or []]
    if not any(logs_base in (base.get("uri") or []) for base in bases):
        bases.append({"name": LOGS_BASE_NAME, "uri": [logs_base]})
    merged["LOGS_BASES"] = bases
    if revisions:
        merged["REVISIONS"] = {**(merged.get("REVISIONS") or {}), **revisions}
    return merged


def ensure_project_configs(
    base_url: str,
    cookie_jar: Path,
    project_id: int,
    revisions: dict[str, dict[str, str]],
) -> None:
    """Give the project what an import into it needs, keeping what it has.

    ``references`` is merged (its issue trackers, other logs bases and revision
    links stay); ``per_conf`` and ``meta`` are written only when the project has
    none, since someone may have tuned them by hand.
    """
    configs = curl_json(f"{base_url}/api/v2/config/", cookie_jar=cookie_jar) or []

    def find(name: str) -> dict[str, Any] | None:
        return next(
            (
                config
                for config in configs
                if config.get("type") == "global"
                and config.get("name") == name
                and config.get("project") == project_id
            ),
            None,
        )

    logs_base = f"{base_url}/logs/"
    existing = find("references")
    current: dict[str, Any] = {}
    if existing is not None:
        detail = curl_json(
            f"{base_url}/api/v2/config/{existing['id']}/", cookie_jar=cookie_jar
        )
        current = (detail or {}).get("content") or {}
    merged = merged_references(current, logs_base, revisions)
    if existing is None or merged != current:
        upsert_config(
            base_url,
            cookie_jar,
            configs,
            config_type="global",
            name="references",
            description="Logs bases and revision links of published runs",
            content=merged,
            project_id=project_id,
        )
    if find("per_conf") is None:
        upsert_config(
            base_url,
            cookie_jar,
            configs,
            config_type="global",
            name="per_conf",
            description="Main project configuration",
            content=per_conf_config(),
            project_id=project_id,
        )
    if find("meta") is None:
        upsert_config(
            base_url,
            cookie_jar,
            configs,
            config_type="global",
            name="meta",
            description="Meta categorization configuration",
            content=meta_config(),
            project_id=project_id,
        )


def wait_for_import(
    base_url: str, job_id: int, import_url: str, cookie_jar: Path, timeout: int
) -> int:
    expected = normalize_url(import_url)
    deadline = time_module.monotonic() + timeout
    last: Any = None
    while time_module.monotonic() < deadline:
        try:
            last = curl_json(
                f"{base_url}/api/v2/session_import/{job_id}/", cookie_jar=cookie_jar
            )
        except CliError:
            last = None
        for task in last or []:
            if normalize_url(str(task.get("run_source_url", ""))) != expected:
                continue
            status = str(task.get("status", "")).upper()
            if status == "FAILURE":
                raise CliError(f"import of {import_url} failed: {task!r}")
            run_id = task.get("run_id")
            if status == "SUCCESS" and isinstance(run_id, int) and run_id > 0:
                return run_id
        time_module.sleep(2)
    raise CliError(
        f"import of {import_url} did not finish within {timeout}s; last: {last!r}"
    )


def import_bundle(
    settings: Settings,
    import_url: str,
    project: str,
    *,
    setup_project: bool,
    revisions: dict[str, dict[str, str]],
    timeout: int,
) -> int:
    """Import the bundle at ``import_url`` and return its run id.

    A bundle already imported from that URL is not imported again.
    """
    base_url = settings.base_url
    cookie_dir = Path(tempfile.mkdtemp(prefix="bublik-e2e-bundle-"))
    cookie_jar = cookie_dir / "cookies.txt"
    try:
        login(base_url, settings, cookie_jar)
        project_id = ensure_project(base_url, cookie_jar, project, setup_project)
        if setup_project:
            ensure_project_configs(base_url, cookie_jar, project_id, revisions)
        existing = find_existing_run_id(base_url, import_url, cookie_jar)
        if existing is not None:
            return existing
        job_id = schedule_import(base_url, import_url, cookie_jar, project_id)
        return wait_for_import(base_url, job_id, import_url, cookie_jar, timeout)
    finally:
        shutil.rmtree(cookie_dir, ignore_errors=True)
