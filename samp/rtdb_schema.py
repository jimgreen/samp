"""Write RTDB's embedded registry for SAMP's native SQLite tables."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

REGISTRY_TABLES = ("rtdb_schema_database", "rtdb_schema_tables", "rtdb_schema_columns")
IDENTIFIER = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")


def _type(declaration: str) -> tuple[str, str]:
    declaration = declaration.upper()
    if "INT" in declaration:
        return "int", "int"
    if any(token in declaration for token in ("CHAR", "CLOB", "TEXT", "BLOB")) or not declaration:
        return "str", "str"
    return "double", "double"


def _literal_default(sql: str | None, kind: str) -> str | int | float | None:
    if sql is None:
        return None
    if re.fullmatch(r"'(?:[^']|'')*'", sql):
        value = sql[1:-1].replace("''", "'")
        return value if kind == "str" else None
    if re.fullmatch(r"[+-]?\d+", sql) and kind in {"int", "double"}:
        value = int(sql)
        return value if -(2**63) <= value < 2**63 else None
    if kind == "double" and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", sql):
        value = float(sql)
        return value if abs(value) < float("inf") else None
    # Expressions such as unixepoch() remain SQLite defaults.
    return None


def _ensure_registry(connection: sqlite3.Connection, database: str | Path, table: str) -> None:
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_database'"
    ).fetchone()
    if existing:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(rtdb_schema_database)")}
        if "storage_format" not in columns:
            raise ValueError("不能通过 SAMP 修改由 RTDB 管理的数据库结构")
        marker = connection.execute(
            "SELECT storage_format FROM rtdb_schema_database WHERE version=1"
        ).fetchone()
        if marker is None or marker[0] != "native":
            raise ValueError("不能通过 SAMP 修改由 RTDB 管理的数据库结构")
        return

    connection.execute("""
        CREATE TABLE rtdb_schema_database (
            version INTEGER PRIMARY KEY CHECK(version=1),
            eng_name TEXT NOT NULL, chn_name TEXT NOT NULL,
            storage_format TEXT NOT NULL DEFAULT 'native')
    """)
    stem = Path(database).stem
    connection.execute(
        "INSERT INTO rtdb_schema_database VALUES (1, ?, ?, 'native')",
        (stem if IDENTIFIER.fullmatch(stem) else table, stem),
    )
    connection.execute("""
        CREATE TABLE rtdb_schema_tables (
            table_name TEXT PRIMARY KEY, chn_name TEXT NOT NULL,
            ordinal INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            indexes_json TEXT)
    """)
    connection.execute("""
        CREATE TABLE rtdb_schema_columns (
            table_name TEXT NOT NULL, ordinal INTEGER NOT NULL,
            eng_name TEXT NOT NULL, chn_name TEXT NOT NULL, data_type TEXT NOT NULL,
            primary_key INTEGER NOT NULL CHECK(primary_key IN (0,1)),
            disp_type TEXT NOT NULL, disp_menu TEXT NOT NULL,
            has_default INTEGER NOT NULL CHECK(has_default IN (0,1)), default_json TEXT,
            PRIMARY KEY(table_name, ordinal), UNIQUE(table_name, eng_name),
            FOREIGN KEY(table_name) REFERENCES rtdb_schema_tables(table_name) ON DELETE CASCADE)
    """)


def sync_table(
    connection: sqlite3.Connection, database: str | Path, table: str, columns: list[sqlite3.Row],
) -> None:
    """Reconcile the registry inside the same transaction as the table DDL."""
    if not connection.in_transaction:
        raise RuntimeError("structure sync requires a transaction")
    if not any(column[5] for column in columns):
        raise ValueError(f"表 {table} 缺少主键，无法生成 RTDB 结构定义")

    definitions = []
    for _, name, declaration, _, default_sql, key_position in columns:
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"字段 {name} 无法登记到 RTDB")
        kind, display = _type(declaration)
        definition = {
            "eng_name": name, "chn_name": name, "data_type": kind,
            "primary_key": bool(key_position), "disp_type": display, "disp_menu": "",
        }
        default = _literal_default(default_sql, kind)
        if default is not None:
            definition["default_value"] = default
        definitions.append(definition)
    mapping = {"head": {"eng_name": table, "chn_name": table}, "columns": definitions}
    fingerprint = hashlib.sha256(
        json.dumps(mapping, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    _ensure_registry(connection, database, table)
    existing = connection.execute(
        "SELECT fingerprint FROM rtdb_schema_tables WHERE table_name=?", (table,),
    ).fetchone()
    if existing is not None and existing[0] == fingerprint:
        return
    connection.execute(
        """
        INSERT INTO rtdb_schema_tables(table_name, chn_name, ordinal, fingerprint)
        VALUES (?, ?, (SELECT COALESCE(MAX(ordinal), -1) + 1 FROM rtdb_schema_tables), ?)
        ON CONFLICT(table_name) DO UPDATE SET
            chn_name=excluded.chn_name, fingerprint=excluded.fingerprint, indexes_json=NULL
        """,
        (table, table, fingerprint),
    )
    connection.execute("DELETE FROM rtdb_schema_columns WHERE table_name=?", (table,))
    connection.executemany(
        """
        INSERT INTO rtdb_schema_columns(table_name, ordinal, eng_name, chn_name, data_type,
            primary_key, disp_type, disp_menu, has_default, default_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                table, ordinal, column["eng_name"], column["chn_name"], column["data_type"],
                int(column["primary_key"]), column["disp_type"], column["disp_menu"],
                int("default_value" in column),
                json.dumps(column["default_value"], ensure_ascii=False) if "default_value" in column else None,
            )
            for ordinal, column in enumerate(definitions)
        ),
    )
