from __future__ import annotations

import calendar
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_IDENT = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
SQLITE_DIR = Path(os.getenv("SAMP_SQLITE_DIR", r"D:\codex\sqldb\realtime\samp"))


class Store:
    """采样定义与源表访问。

    `samp_define` 只保存源数据库路径和源表名。采样时通过 SQLite 元数据
    动态读取实际源表，因此程序不依赖固定的 source_rows 表。
    """

    def __init__(self, definition_db: str | Path = SQLITE_DIR / "samp_define.db", source_db: str | Path = SQLITE_DIR / "source.db", result_dir: str | Path = SQLITE_DIR):
        self.definition_db = str(definition_db)
        self.default_source_db = str(source_db)
        self.result_dir = Path(result_dir)
        Path(self.definition_db).parent.mkdir(parents=True, exist_ok=True)
        Path(self.default_source_db).parent.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @staticmethod
    def _connect(path: str | Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._connect(self.definition_db) as conn:
            conn.executescript(schema)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(samp_define)")}
            if "source_database" not in columns:
                conn.execute("ALTER TABLE samp_define ADD COLUMN source_database TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _public(row: sqlite3.Row, default_source_db: str = str(SQLITE_DIR / "source.db")) -> dict[str, Any]:
        data = dict(row)
        data["fields"] = [x for x in data.pop("field_name", "").split(",") if x]
        data["interval_seconds"] = data.pop("samp_time", 60)
        data["filter"] = data.pop("filter_value", "")
        data["source_database"] = data.get("source_database") or default_source_db
        for key in ("samp_type", "trigger_time", "created_at", "updated_at"):
            data.pop(key, None)
        return data

    def list_definitions(self) -> list[dict[str, Any]]:
        with self._connect(self.definition_db) as conn:
            return [self._public(row, self.default_source_db) for row in conn.execute("SELECT * FROM samp_define ORDER BY samp_name").fetchall()]

    def get_definition(self, samp_id: int) -> dict[str, Any] | None:
        with self._connect(self.definition_db) as conn:
            row = conn.execute("SELECT * FROM samp_define WHERE id=?", (samp_id,)).fetchone()
        return self._public(row, self.default_source_db) if row else None

    def upsert_definition(self, payload: dict[str, Any], samp_id: int | None = None) -> dict[str, Any]:
        name = str(payload.get("name") or payload.get("samp_name") or "").strip()
        source_database = str(payload.get("source_database") or self.default_source_db).strip()
        source_table = str(payload.get("source_table") or payload.get("table") or "").strip()
        fields = payload.get("fields", payload.get("field_name", []))
        if isinstance(fields, str):
            fields = [field.strip() for field in fields.split(",") if field.strip()]
        fields = list(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
        interval = int(payload.get("interval_seconds", payload.get("samp_time", 60)))
        if not name or not source_table or not fields:
            raise ValueError("name、source_database、source_table 和 fields 不能为空")
        if not _IDENT.match(name) or not _IDENT.match(source_table) or any(not _IDENT.match(field) for field in fields):
            raise ValueError("名称只能包含字母、数字、下划线或中文")
        if not source_database:
            raise ValueError("source_database 不能为空")
        if interval < 0:
            raise ValueError("interval_seconds 不能为负数")
        now = int(time.time())
        values = (name, source_database, source_table, ",".join(fields), 1, interval, str(payload.get("filter", payload.get("filter_value", ""))), int(bool(payload.get("enabled", True))), 0, now, now)
        with self._lock, self._connect(self.definition_db) as conn:
            if samp_id is None:
                samp_id = int(conn.execute("INSERT INTO samp_define(samp_name,source_database,source_table,field_name,samp_type,samp_time,filter_value,enabled,trigger_time,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", values).lastrowid)
            else:
                conn.execute("UPDATE samp_define SET samp_name=?,source_database=?,source_table=?,field_name=?,samp_type=?,samp_time=?,filter_value=?,enabled=?,trigger_time=?,updated_at=? WHERE id=?", (*values[:9], values[10], samp_id))
        return self.get_definition(samp_id)  # type: ignore[return-value]

    def delete_definition(self, samp_id: int) -> bool:
        with self._lock, self._connect(self.definition_db) as conn:
            return conn.execute("DELETE FROM samp_define WHERE id=?", (samp_id,)).rowcount > 0

    def set_enabled(self, samp_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._connect(self.definition_db) as conn:
            conn.execute("UPDATE samp_define SET enabled=?,updated_at=? WHERE id=?", (int(enabled), int(time.time()), samp_id))
        return self.get_definition(samp_id)

    @staticmethod
    def _quote(name: str) -> str:
        if not _IDENT.match(name):
            raise ValueError(f"非法标识符: {name}")
        return f'"{name}"'

    def _source_database(self, source_database: str | None) -> str:
        return str(source_database or self.default_source_db)

    def source_tables(self, source_database: str | None = None) -> list[str]:
        with self._connect(self._source_database(source_database)) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [str(row[0]) for row in rows]

    def source_fields(self, source_database: str | None, source_table: str) -> list[str]:
        with self._connect(self._source_database(source_database)) as conn:
            rows = conn.execute(f"PRAGMA table_info({self._quote(source_table)})").fetchall()
        return [str(row[1]) for row in rows]

    def inspect_source(self, source_database: str | None, source_table: str, limit: int = 20) -> dict[str, Any]:
        """Return source columns and a small preview for the definition page."""
        database = self._source_database(source_database)
        table = self._quote(source_table)
        with self._connect(database) as conn:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {database}:{source_table}")
            safe_limit = max(1, min(int(limit), 100))
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} LIMIT ?", (safe_limit,)).fetchall()]
        return {"source_database": database, "source_table": source_table, "fields": columns, "rows": rows}

    def replace_source_rows(self, source_table: str, rows: Iterable[dict[str, Any]], source_database: str | None = None) -> int:
        """测试/导入辅助：把数据写入调用方指定的真实表名。"""
        if not _IDENT.match(source_table):
            raise ValueError("源表名不合法")
        database = self._source_database(source_database)
        materialized = list(rows)
        if not materialized:
            return 0
        all_fields = sorted({str(key) for row in materialized for key in row.get("values", row.get("data", row)).keys()})
        if any(not _IDENT.match(field) for field in all_fields):
            raise ValueError("源字段名不合法")
        with self._lock, self._connect(database) as conn:
            table = self._quote(source_table)
            existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not existing:
                columns = ", ".join(["row_key TEXT PRIMARY KEY", *[f'{self._quote(field)}' + " REAL" for field in all_fields]])
                conn.execute(f"CREATE TABLE {table} ({columns})")
                existing = {"row_key", *all_fields}
            for field in all_fields:
                if field not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {self._quote(field)} REAL")
            columns = ["row_key", *all_fields]
            placeholders = ",".join("?" for _ in columns)
            for index, row in enumerate(materialized):
                key = str(row.get("row_key", row.get("key", index)))
                values = row.get("values", row.get("data", row))
                conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(self._quote(c) for c in columns)}) VALUES ({placeholders})", [key, *[values.get(field) for field in all_fields]])
        return len(materialized)

    @staticmethod
    def _matches_filter(values: dict[str, Any], expression: str) -> bool:
        for clause in re.split(r"\s+and\s+|,", expression or "", flags=re.I):
            if not clause.strip():
                continue
            if "=" not in clause:
                return False
            key, expected = clause.split("=", 1)
            if str(values.get(key.strip(), "")) != expected.strip().strip("'\""):
                return False
        return True

    def _read_source(self, source_database: str, source_table: str, expression: str) -> list[tuple[str, dict[str, Any]]]:
        with self._connect(self._source_database(source_database)) as conn:
            table = self._quote(source_table)
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {source_database}:{source_table}")
            rows = conn.execute(f"SELECT rowid, * FROM {table}").fetchall()
        key_column = "row_key" if "row_key" in columns else None
        result = []
        for row in rows:
            values = {column: row[column] for column in columns}
            key = str(values.pop(key_column)) if key_column else str(row[0])
            if self._matches_filter(values, expression):
                result.append((key, values))
        return result

    def _raw_definition(self, samp_id: int) -> sqlite3.Row:
        with self._connect(self.definition_db) as conn:
            row = conn.execute("SELECT * FROM samp_define WHERE id=?", (samp_id,)).fetchone()
        if not row:
            raise KeyError("采样定义不存在")
        return row

    @staticmethod
    def _result_name(name: str, sampled_at: int) -> str:
        value = f"samp_{name}_{time.gmtime(sampled_at).tm_year}"
        if not _IDENT.match(value):
            raise ValueError("采样点名生成的结果表名不合法")
        return value

    def run_sample(self, samp_id: int, sampled_at: int | None = None) -> dict[str, Any]:
        sampled_at = int(sampled_at or time.time())
        definition = self._raw_definition(samp_id)
        rows = self._read_source(definition["source_database"] or self.default_source_db, definition["source_table"], definition["filter_value"])
        fields = [field for field in definition["field_name"].split(",") if field]
        table = self._result_name(definition["samp_name"], sampled_at)
        db_path = self.result_dir / f"{table}.db"
        col_sql = ", ".join(["sample_time INTEGER NOT NULL", "row_key TEXT NOT NULL", *[f'{self._quote(field)} REAL' for field in fields]])
        with self._lock, self._connect(db_path) as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {self._quote(table)} ({col_sql}, PRIMARY KEY(sample_time,row_key))")
            columns = ["sample_time", "row_key", *fields]
            placeholders = ",".join("?" for _ in columns)
            for key, values in rows:
                conn.execute(f"INSERT OR REPLACE INTO {self._quote(table)} ({', '.join(self._quote(c) for c in columns)}) VALUES ({placeholders})", [sampled_at, key, *[values.get(field) for field in fields]])
        return {"samp_id": samp_id, "sampled_at": sampled_at, "rows_written": len(rows), "database": str(db_path), "table": table}

    def query_samples(self, samp_id: int, start: int, end: int, fields: list[str] | None = None) -> dict[str, Any]:
        raw, definition = self._raw_definition(samp_id), self.get_definition(samp_id)
        selected = fields or definition["fields"]  # type: ignore[index]
        selected = [field for field in selected if field in definition["fields"] and _IDENT.match(field)]  # type: ignore[index]
        rows: list[dict[str, Any]] = []
        tables: list[str] = []
        year, last_year = time.gmtime(start).tm_year, time.gmtime(end).tm_year
        while year <= last_year:
            probe = calendar.timegm((year, 1, 1, 0, 0, 0))
            table = self._result_name(raw["samp_name"], probe)
            db_path = self.result_dir / f"{table}.db"
            if db_path.exists():
                with self._connect(db_path) as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        field_sql = ", ".join(self._quote(field) for field in selected)
                        rows.extend(dict(row) for row in conn.execute(f"SELECT sample_time,row_key,{field_sql} FROM {self._quote(table)} WHERE sample_time BETWEEN ? AND ? ORDER BY sample_time,row_key", (start, end)).fetchall())
                        tables.append(table)
            year += 1
        rows.sort(key=lambda row: (row["sample_time"], row["row_key"]))
        return {"definition": definition, "columns": ["sample_time", "row_key", *selected], "rows": rows, "tables": tables}

    def status(self) -> dict[str, Any]:
        with self._connect(self.definition_db) as conn:
            definitions, enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM samp_define").fetchone()
        return {
            "definitions": definitions,
            "enabled": enabled,
            "result_databases": len(list(self.result_dir.glob("*.db"))),
            "items": [self.runtime_status(item["id"]) for item in self.list_definitions()],
        }

    def runtime_status(self, samp_id: int) -> dict[str, Any]:
        definition = self._raw_definition(samp_id)
        prefix = f"samp_{definition['samp_name']}_"
        files = sorted(self.result_dir.glob(f"{prefix}*.db"))
        years = []
        for path in files:
            suffix = path.stem[len(prefix):]
            if suffix.isdigit():
                years.append(int(suffix))
        return {
            "id": samp_id,
            "name": definition["samp_name"],
            "enabled": bool(definition["enabled"]),
            "interval_seconds": definition["samp_time"],
            "source_database": definition["source_database"] or self.default_source_db,
            "source_table": definition["source_table"],
            "result_years": years,
            "last_result_at": max((path.stat().st_mtime for path in files), default=None),
        }
