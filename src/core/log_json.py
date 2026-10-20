"""The per-node log JSON (``json/``) Bublik's log viewer reads for a bundle.

Bublik builds a run's tree from the database, but each node's log pane fetches
``json/node_id<test_id>.json`` (``node_1_0.json`` for the run root) from the
bundle. Without these files every log pane of an imported run is an error card.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

Node = dict[str, Any]
Row = dict[str, Any]
RowsFor = Callable[[Node], "list[Row] | None"]


def node_status(node: Node) -> str:
    return node["obtained"]["result"]["status"]


def status_level(status: str) -> str:
    if status == "PASSED":
        return "RING"
    if status in {"FAILED", "KILLED", "CORED"}:
        return "ERROR"
    if status in {"SKIPPED", "FAKED", "INCOMPLETE"}:
        return "WARN"
    return "INFO"


def time_part(ts: str) -> str:
    return ts.split(" ", 1)[1]


def duration_str(seconds: float) -> str:
    total_ms = max(0, int(seconds * 1000))
    total_seconds, ms = divmod(total_ms, 1000)
    minutes, seconds_part = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes}:{seconds_part}.{ms:03}"


def text_content(content: str) -> list[dict[str, Any]]:
    return [{"type": "te-log-table-content-text", "content": content}]


def mi_content(content: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "te-log-table-content-mi", "content": content}]


def text_row(
    line_number: int,
    level: str,
    entity_name: str,
    user_name: str,
    ts: str,
    utc: float,
    content: str,
) -> Row:
    return {
        "line_number": line_number,
        "level": level,
        "entity_name": entity_name,
        "user_name": user_name,
        "timestamp": {"timestamp": utc, "formatted": time_part(ts)},
        "log_content": text_content(content),
    }


def entity_model(node: Node) -> dict[str, Any]:
    entity = "Package" if node["type"] == "pkg" else "Test"
    model: dict[str, Any] = {
        "id": str(node["test_id"]),
        "name": node["name"],
        "entity": entity,
        "result": node_status(node),
        "extended_properties": {"path": node["path_str"]},
    }
    if node.get("err"):
        model["error"] = node["err"]
    if node["type"] == "test":
        model["extended_properties"]["tin"] = str(node["tin"])
        model["extended_properties"]["hash"] = node["hash"]
    return model


def meta_for_node(node: Node) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start": time_part(node["start_ts"]),
        "end": time_part(node["end_ts"]),
        "duration": duration_str(node["end_ts_utc"] - node["start_ts_utc"]),
    }
    if node.get("objective"):
        payload["objective"] = node["objective"]
    if node.get("params"):
        payload["parameters"] = [
            {"name": name, "value": str(value)}
            for name, value in node["params"].items()
        ]
    verdicts = node["obtained"]["result"].get("verdicts")
    if verdicts:
        level = status_level(node_status(node))
        payload["verdicts"] = [
            {"verdict": verdict, "level": level} for verdict in verdicts
        ]
    if node.get("artifacts"):
        level = status_level(node_status(node))
        payload["artifacts"] = [
            {"artifact": artifact, "level": level} for artifact in node["artifacts"]
        ]
    return payload


def table_rows_for_node(node: Node) -> list[Row]:
    line = 0

    def next_line() -> int:
        nonlocal line
        line += 1
        return line

    children = node.get("iters") or []
    if children:
        rows = []
        for child in children:
            kind = "test" if child["type"] == "test" else "package"
            description = f"{child['name']} {kind} start"
            if child.get("objective"):
                description = f"{description}\n{child['objective']}"
            rows.append(
                text_row(
                    next_line(),
                    status_level(node_status(child)),
                    child["name"],
                    "Step",
                    child["start_ts"],
                    child["start_ts_utc"],
                    description,
                )
            )
        return rows

    main = node["name"]
    status = node_status(node)
    rows = [
        text_row(
            next_line(),
            "INFO",
            main,
            "TAPI Jumps",
            node["start_ts"],
            node["start_ts_utc"],
            "Main test entity",
        ),
        text_row(
            next_line(),
            status_level(status),
            main,
            "Step",
            node["start_ts"],
            node["start_ts_utc"],
            f"{node['name']} start"
            + (f"\n{node['objective']}" if node.get("objective") else ""),
        ),
    ]
    for measurement in node.get("measurements") or []:
        rows.append(
            {
                "line_number": next_line(),
                "level": "MI",
                "entity_name": main,
                "user_name": "Artifact",
                "timestamp": {
                    "timestamp": node["start_ts_utc"],
                    "formatted": time_part(node["start_ts"]),
                },
                "log_content": mi_content(measurement),
            }
        )
    rows.append(
        text_row(
            next_line(),
            status_level(status),
            "Tester",
            "Run",
            node["end_ts"],
            node["end_ts_utc"],
            f"Obtained result is:\n{status}",
        )
    )
    return rows


def log_json_for_node(node: Node, rows: list[Row] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "te-log-meta",
            "entity_model": entity_model(node),
            "meta": meta_for_node(node),
        }
    ]
    children = node.get("iters") or []
    if children:
        content.append(
            {
                "type": "te-log-entity-list",
                "items": [entity_model(child) for child in children],
            }
        )
    content.append(
        {
            "type": "te-log-table",
            "data": rows if rows is not None else table_rows_for_node(node),
        }
    )
    return {"version": "v1", "root": [{"type": "te-log", "content": content}]}


def tree_entry(node: Node, file_name: str, child_files: list[str]) -> dict[str, Any]:
    status = node_status(node)
    entry: dict[str, Any] = {
        "id": file_name,
        "name": node["name"],
        "has_error": status not in {"PASSED", "SKIPPED"},
        "skipped": status == "SKIPPED",
        "entity": node["type"],
    }
    if child_files:
        entry["children"] = child_files
    return entry


def write_json_logs(
    root: Node, json_dir: Path, *, pretty: bool = False, rows_for: RowsFor | None = None
) -> None:
    """Write ``tree.json`` and a log per node under ``json_dir``.

    ``rows_for`` may return a node's own table rows; ``None`` keeps the default.
    """
    json_dir.mkdir(parents=True, exist_ok=True)
    indent = 2 if pretty else None
    separators = None if pretty else (",", ":")
    tree: dict[str, dict[str, Any]] = {}

    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=indent, separators=separators) + "\n",
            encoding="utf-8",
        )

    def write_node(node: Node, is_root: bool = False) -> str:
        file_name = "node_1_0.json" if is_root else f"node_id{node['test_id']}.json"
        child_files = [write_node(child) for child in node.get("iters") or []]
        payload = log_json_for_node(node, rows_for(node) if rows_for else None)
        write_json(json_dir / file_name, payload)
        if is_root:
            write_json(json_dir / "node_id1.json", payload)
        tree[file_name] = tree_entry(node, file_name, child_files)
        return file_name

    write_node(root, is_root=True)
    write_json(json_dir / "tree.json", {"main_package": "node_1_0.json", "tree": tree})
