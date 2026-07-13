"""Canonical SQLite schema fingerprints for Hub and Edge continuity checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def schema_contract_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Describe user schema definitions, columns, FKs, and index mechanics."""

    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    objects: dict[str, dict[str, Any]] = {}
    for row in rows:
        object_type = str(row[0])
        name = str(row[1])
        record: dict[str, Any] = {
            "type": object_type,
            "name": name,
            "table": str(row[2]),
            "sql": _normalize_sql(row[3]),
        }
        if object_type == "table":
            quoted = _quote_identifier(name)
            record["columns"] = [
                [
                    int(value[0]),
                    str(value[1]),
                    str(value[2]),
                    int(value[3]),
                    value[4],
                    int(value[5]),
                    int(value[6]),
                ]
                for value in connection.execute(f"PRAGMA table_xinfo({quoted})")
            ]
            record["foreign_keys"] = [
                list(value)
                for value in connection.execute(f"PRAGMA foreign_key_list({quoted})")
            ]
        elif object_type == "index":
            quoted = _quote_identifier(name)
            record["index_columns"] = [
                list(value)
                for value in connection.execute(f"PRAGMA index_xinfo({quoted})")
            ]
        objects[f"{object_type}:{name}"] = record
    encoded = json.dumps(objects, sort_keys=True, separators=(",", ":"))
    return {
        "objects": objects,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def schema_contract_difference(
    actual: dict[str, Any], expected: dict[str, Any]
) -> dict[str, Any]:
    actual_objects = dict(actual.get("objects") or {})
    expected_objects = dict(expected.get("objects") or {})
    actual_names = set(actual_objects)
    expected_names = set(expected_objects)
    return {
        "missing": sorted(expected_names - actual_names),
        "unexpected": sorted(actual_names - expected_names),
        "changed": sorted(
            name
            for name in actual_names & expected_names
            if actual_objects[name] != expected_objects[name]
        ),
        "actual_sha256": str(actual.get("sha256") or ""),
        "expected_sha256": str(expected.get("sha256") or ""),
    }


def _normalize_sql(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'
