"""Parse the run plan (mixes, day specs, fills) and expand it into runs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.common import CliError, sanitize_path_part
from core.constants import (
    RESULT_PROPERTIES,
    RESULT_PROPERTY_ALIASES,
    RESULT_TYPES,
    RUN_STATUS_BY_CONCLUSION,
)
from core.fixture_api import FixtureProvider


@dataclass(frozen=True)
class MixValue:
    key: str
    value: float
    is_percent: bool


@dataclass
class PlannedRun:
    id: str
    fixture: FixtureProvider
    conclusion: str
    mix_name: str
    run_date: str
    ordinal: int
    day_index: int


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise CliError(f"invalid date {value!r}, expected YYYY-MM-DD") from exc


def parse_date_range(value: str) -> list[str]:
    if ".." not in value:
        return [parse_date(value).date().isoformat()]
    start_raw, end_raw = value.split("..", 1)
    start = parse_date(start_raw.strip()).date()
    end = parse_date(end_raw.strip()).date()
    if end < start:
        raise CliError("--dates range end cannot be before start")
    result: list[str] = []
    current = start
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def parse_mix_key(key: str) -> tuple[str, str]:
    for alias, replacement in RESULT_PROPERTY_ALIASES.items():
        if key.startswith(alias):
            key = replacement + key[len(alias) :]
            break
    for prop in sorted(RESULT_PROPERTIES, key=len, reverse=True):
        if key.startswith(prop):
            suffix = key[len(prop) :]
            normalized = suffix[:1].lower() + suffix[1:]
            if normalized in RESULT_TYPES:
                return prop, normalized
    raise CliError(
        f"unknown result mix key {key!r}; expected keys like unexpectedFailed=20%"
    )


def parse_mix_entry(entry: str) -> tuple[str, list[MixValue]]:
    parts = entry.split(None, 1)
    if len(parts) != 2:
        raise CliError("--mix must be NAME key=value[,key=value...]")
    name, values_raw = parts
    values: list[MixValue] = []
    for item in values_raw.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise CliError(f"invalid mix item {item!r}")
        key, raw_value = [part.strip() for part in item.split("=", 1)]
        parse_mix_key(key)
        is_percent = raw_value.endswith("%")
        number = raw_value[:-1] if is_percent else raw_value
        try:
            value = float(number)
        except ValueError as exc:
            raise CliError(f"invalid mix value {raw_value!r}") from exc
        if value < 0:
            raise CliError(f"mix value cannot be negative: {item!r}")
        values.append(MixValue(key=key, value=value, is_percent=is_percent))
    if not values:
        raise CliError(f"mix {name!r} does not contain any values")
    return name, values


def validate_conclusion(conclusion: str) -> None:
    if conclusion not in RUN_STATUS_BY_CONCLUSION:
        valid = ", ".join(sorted(RUN_STATUS_BY_CONCLUSION))
        raise CliError(f"unknown conclusion {conclusion!r}; expected one of: {valid}")


def parse_day_entry(
    entry: str,
) -> tuple[str, list[tuple[str | None, str, str | None, int]]]:
    if ":" not in entry:
        raise CliError("--day must be YYYY-MM-DD:spec")
    date_raw, spec_raw = entry.split(":", 1)
    run_date = parse_date(date_raw.strip()).date().isoformat()
    specs: list[tuple[str | None, str, str | None, int]] = []
    for item in spec_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise CliError(f"invalid --day item {item!r}")
        lhs, count_raw = [part.strip() for part in item.split("=", 1)]
        try:
            count = int(count_raw)
        except ValueError as exc:
            raise CliError(f"invalid run count {count_raw!r}") from exc
        if count < 0:
            raise CliError(f"run count cannot be negative: {item!r}")
        mix_name = None
        if "@" in lhs:
            lhs, mix_name = [part.strip() for part in lhs.split("@", 1)]
        fixture_name = None
        conclusion = lhs
        if "." in lhs:
            fixture_name, conclusion = [part.strip() for part in lhs.split(".", 1)]
        validate_conclusion(conclusion)
        specs.append((fixture_name, conclusion, mix_name, count))
    return run_date, specs


def build_mixes(args: argparse.Namespace) -> dict[str, list[MixValue]]:
    mixes: dict[str, list[MixValue]] = {
        "fixture-default": [],
        "all-ok": [],
    }
    for entry in args.mix or []:
        name, values = parse_mix_entry(entry)
        mixes[name] = values
    return mixes


def plan_from_days(
    args: argparse.Namespace,
    fixtures: dict[str, FixtureProvider],
) -> tuple[list[PlannedRun], list[str]]:
    planned: list[PlannedRun] = []
    empty_dates: list[str] = []
    day_index = 0
    for entry in args.day or []:
        run_date, specs = parse_day_entry(entry)
        if not specs:
            empty_dates.append(run_date)
            continue
        for fixture_name, conclusion, mix_name, count in specs:
            names = [fixture_name] if fixture_name else list(fixtures)
            for name in names:
                if name not in fixtures:
                    raise CliError(f"unknown fixture {name!r} in --day")
                for _ in range(count):
                    ordinal = len(planned) + 1
                    planned.append(
                        PlannedRun(
                            id=sanitize_path_part(
                                f"{name}-{run_date}-{conclusion}-{ordinal:03d}"
                            ),
                            fixture=fixtures[name],
                            conclusion=conclusion,
                            mix_name=mix_name or fixtures[name].default_mix,
                            run_date=run_date,
                            ordinal=ordinal,
                            day_index=day_index,
                        )
                    )
        day_index += 1
    return planned, empty_dates


def plan_from_fill(
    args: argparse.Namespace,
    fixtures: dict[str, FixtureProvider],
) -> tuple[list[PlannedRun], list[str]]:
    if not args.fill:
        return [], []
    if not args.dates:
        raise CliError("--fill requires --dates")
    validate_conclusion(args.fill)
    dates = parse_date_range(args.dates)
    if not dates:
        raise CliError("--dates did not resolve to any dates")
    names = list(fixtures)
    planned: list[PlannedRun] = []
    for index in range(args.runs):
        fixture = fixtures[names[index % len(names)]]
        run_date = dates[index % len(dates)]
        ordinal = index + 1
        planned.append(
            PlannedRun(
                id=sanitize_path_part(
                    f"{fixture.name}-{run_date}-{args.fill}-{ordinal:03d}"
                ),
                fixture=fixture,
                conclusion=args.fill,
                mix_name=fixture.default_mix,
                run_date=run_date,
                ordinal=ordinal,
                day_index=dates.index(run_date),
            )
        )
    return planned, []


def build_plan(
    args: argparse.Namespace,
    fixtures: dict[str, FixtureProvider],
) -> tuple[list[PlannedRun], list[str]]:
    if args.day and args.fill:
        raise CliError("--day and --fill are mutually exclusive")
    if not args.runs or args.runs < 1:
        raise CliError("--runs must be specified and greater than zero")
    planned, empty_dates = (
        plan_from_days(args, fixtures) if args.day else plan_from_fill(args, fixtures)
    )
    if not planned:
        raise CliError("no runs planned; use --day or --fill with --dates")
    if len(planned) != args.runs:
        raise CliError(
            f"--runs={args.runs} but fixture plan contains {len(planned)} runs"
        )
    return planned, empty_dates
