from __future__ import annotations

import calendar
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .rtdb_schema import REGISTRY_TABLES, sync_table

_IDENT = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
_RESULT_FILE = re.compile(r"^samp_([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)_(\d{4})\.db$")
_CLAUSE_IN = re.compile(
    r"^\s*([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s+(not\s+in|in)\s*\((.*)\)\s*$",
    flags=re.I,
)
_CLAUSE_CMP = re.compile(
    r"^\s*([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s*(!=|<>|>=|<=|=|>|<)\s*(.+?)\s*$"
)
_RESULT_KEYS = {"sample_time", "row_key"}
_ALLOWED_AGGS = {"raw", "avg", "min", "max", "last"}
SQLITE_DIR = Path(os.getenv("SAMP_SQLITE_DIR", r"D:\codex\sqldb\realtime\samp"))
LOGGER = logging.getLogger(__name__)


def _normalize_sql_type(declaration: str | None) -> str:
    """将 SQLite 列声明归一化为结果表与 RTDB 兼容的标准类型。"""
    text = (declaration or "").upper()
    if "INT" in text:
        return "INTEGER"
    if any(token in text for token in ("CHAR", "CLOB", "TEXT", "BLOB")):
        return "TEXT"
    return "REAL"


def _coerce_param(raw: str, sql_type: str) -> Any:
    """按目标列声明类型转换过滤条件绑定参数，避免浮点/整数与字符串误判。"""
    cleaned = raw.strip()
    if len(cleaned) >= 2 and ((cleaned[0] == cleaned[-1] == "'") or (cleaned[0] == cleaned[-1] == '"')):
        cleaned = cleaned[1:-1]
    if sql_type == "INTEGER":
        try:
            return int(cleaned)
        except ValueError:
            try:
                num = float(cleaned)
                if num.is_integer():
                    return int(num)
            except ValueError:
                pass
            return cleaned
    if sql_type == "REAL":
        try:
            return float(cleaned)
        except ValueError:
            return cleaned
    return cleaned


def _split_filter_clauses(expression: str) -> list[str]:
    """按顶层 AND 或逗号拆分过滤条件，忽略括号与引号内部。"""
    text = (expression or "").strip()
    if not text:
        return []
    clauses: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            idx += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            idx += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            idx += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            idx += 1
            continue
        if depth == 0 and ch == ",":
            piece = "".join(buf).strip()
            if piece:
                clauses.append(piece)
            buf = []
            idx += 1
            continue
        if depth == 0 and ch.isspace():
            rest = text[idx:]
            match = re.match(r"^\s+and\s+", rest, flags=re.I)
            if match:
                piece = "".join(buf).strip()
                if piece:
                    clauses.append(piece)
                buf = []
                idx += match.end()
                continue
        buf.append(ch)
        idx += 1
    tail = "".join(buf).strip()
    if tail:
        clauses.append(tail)
    return clauses


def _split_csv_values(raw_list: str) -> list[str]:
    """拆分 IN (...) 中的参数列表。"""
    values: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in raw_list:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            item = "".join(buf).strip()
            if item:
                values.append(item)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        values.append(tail)
    return values


class Store:
    """采样定义、源表访问、年度结果库写入与查询存储层。

    - 按数据库路径分片加锁，不同年度结果库可并行写入；
    - 定义列表与结果表结构带轻量内存缓存，减少高频调度开销；
    - 结果表按源表列声明类型（INTEGER/REAL/TEXT）建列并同步更新 RTDB 内置结构表。
    """

    def __init__(
        self,
        definition_db: str | Path = SQLITE_DIR / "samp_define.db",
        source_db: str | Path = SQLITE_DIR / "source.db",
        result_dir: str | Path = SQLITE_DIR,
    ):
        self.definition_db = str(definition_db)
        self.default_source_db = str(source_db)
        self.result_dir = Path(result_dir)
        Path(self.definition_db).parent.mkdir(parents=True, exist_ok=True)
        Path(self.default_source_db).parent.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)

        self._locks_guard = threading.Lock()
        self._db_locks: dict[str, threading.RLock] = {}

        self._def_cache_lock = threading.RLock()
        self._def_version = 0
        self._def_cache_key: tuple[int, int, int] | None = None
        self._def_cache_rows: list[dict[str, Any]] | None = None

        self._schema_cache_lock = threading.Lock()
        self._synced_result_schemas: dict[str, dict[str, str]] = {}
        self._meta_cache_lock = threading.Lock()
        self._result_meta_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

        self.initialize()
        self._sync_existing_results()

    def _db_lock(self, path: str | Path) -> threading.RLock:
        """获取指定 SQLite 文件对应的分片锁，避免跨库写入互相阻塞。"""
        key = os.path.normcase(str(Path(path).expanduser().resolve()))
        with self._locks_guard:
            lock = self._db_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._db_locks[key] = lock
            return lock

    def _invalidate_definition_cache(self) -> None:
        with self._def_cache_lock:
            self._def_version += 1
            self._def_cache_key = None
            self._def_cache_rows = None

    def _definition_file_stamp(self) -> tuple[int, int, int]:
        try:
            st = os.stat(self.definition_db)
            return (self._def_version, st.st_mtime_ns, st.st_size)
        except OSError:
            return (self._def_version, 0, 0)

    @staticmethod
    @contextmanager
    def _connect(path: str | Path) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    @contextmanager
    def _connect_readonly(path: str | Path) -> Iterator[sqlite3.Connection]:
        resolved = Path(path).expanduser().resolve()
        uri = resolved.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(schema)
            columns = conn.execute("PRAGMA table_info(samp_define)").fetchall()
            if not any(row[1] == "source_database" for row in columns):
                conn.execute("ALTER TABLE samp_define ADD COLUMN source_database TEXT NOT NULL DEFAULT ''")
                columns = conn.execute("PRAGMA table_info(samp_define)").fetchall()
            sync_table(conn, self.definition_db, "samp_define", columns)
        self._invalidate_definition_cache()

    def _sync_existing_results(self) -> None:
        for path in self.result_dir.glob("samp_*.db"):
            if not _RESULT_FILE.fullmatch(path.name) or not path.is_file():
                continue
            table = path.stem
            try:
                with self._db_lock(path), self._connect(path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    columns = conn.execute(f"PRAGMA table_info({self._quote(table)})").fetchall()
                    keys = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
                    if keys == ("sample_time", "row_key"):
                        sync_table(conn, path, table, columns)
            except (sqlite3.Error, ValueError) as exc:
                LOGGER.warning("无法同步历史结果库 %s 的 RTDB 结构：%s", path, exc)

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
        """获取全部采样定义（带文件 mtime 与版本号缓存）。"""
        with self._def_cache_lock:
            stamp = self._definition_file_stamp()
            if self._def_cache_rows is not None and self._def_cache_key == stamp:
                return [
                    {**item, "fields": list(item["fields"])}
                    for item in self._def_cache_rows
                ]
        with self._connect(self.definition_db) as conn:
            rows = [
                self._public(row, self.default_source_db)
                for row in conn.execute("SELECT * FROM samp_define ORDER BY samp_name").fetchall()
            ]
        with self._def_cache_lock:
            self._def_cache_rows = rows
            self._def_cache_key = self._definition_file_stamp()
        return [{**item, "fields": list(item["fields"])} for item in rows]

    def get_definition(self, samp_id: int) -> dict[str, Any] | None:
        with self._connect(self.definition_db) as conn:
            row = conn.execute("SELECT * FROM samp_define WHERE id=?", (samp_id,)).fetchone()
        return self._public(row, self.default_source_db) if row else None

    def _rename_result_databases(self, old_name: str, new_name: str) -> list[str]:
        """将旧采样点名对应的全部年度结果库同步重命名为新名称并更新 RTDB 结构登记。"""
        prefix = f"samp_{old_name}_"
        pairs: list[tuple[Path, Path, str, str]] = []
        for src_path in sorted(self.result_dir.glob(f"{prefix}*.db")):
            if not src_path.is_file():
                continue
            suffix = src_path.stem[len(prefix):]
            if len(suffix) != 4 or not suffix.isdigit():
                continue
            old_table = src_path.stem
            new_table = f"samp_{new_name}_{suffix}"
            if not _IDENT.match(new_table):
                raise ValueError(f"新采样点名生成的结果表名不合法: {new_table}")
            dst_path = self.result_dir / f"{new_table}.db"
            if dst_path.exists():
                raise ValueError(f"目标结果库已存在，无法迁移: {dst_path.name}")
            pairs.append((src_path, dst_path, old_table, new_table))

        migrated: list[str] = []
        for src_path, dst_path, old_table, new_table in pairs:
            src_key = os.path.normcase(str(src_path.resolve()))
            dst_key = os.path.normcase(str(dst_path.resolve()))
            with self._db_lock(src_path), self._db_lock(dst_path):
                with self._connect(src_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old_table,)
                    ).fetchone()
                    if exists:
                        conn.execute(f"ALTER TABLE {self._quote(old_table)} RENAME TO {self._quote(new_table)}")
                        if conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'"
                        ).fetchone():
                            conn.execute("DELETE FROM rtdb_schema_columns WHERE table_name=?", (old_table,))
                            conn.execute("DELETE FROM rtdb_schema_tables WHERE table_name=?", (old_table,))
                        if conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_database'"
                        ).fetchone():
                            conn.execute(
                                "UPDATE rtdb_schema_database SET eng_name=?, chn_name=? WHERE version=1",
                                (new_table, new_table),
                            )
                        cols = conn.execute(f"PRAGMA table_info({self._quote(new_table)})").fetchall()
                        sync_table(conn, dst_path, new_table, cols)
                src_path.rename(dst_path)
                for suffix in ("-wal", "-shm", "-journal"):
                    extra = Path(f"{src_path}{suffix}")
                    if extra.exists():
                        extra.unlink(missing_ok=True)
            with self._schema_cache_lock:
                self._synced_result_schemas.pop(src_key, None)
                self._synced_result_schemas.pop(dst_key, None)
            with self._meta_cache_lock:
                self._result_meta_cache.pop(src_key, None)
                self._result_meta_cache.pop(dst_key, None)
            migrated.append(dst_path.name)
        return migrated

    def upsert_definition(
        self,
        payload: dict[str, Any],
        samp_id: int | None = None,
        rename_results: bool = False,
    ) -> dict[str, Any]:
        name = str(payload.get("name") or payload.get("samp_name") or "").strip()
        source_database = str(payload.get("source_database") or self.default_source_db).strip()
        source_table = str(payload.get("source_table") or payload.get("table") or "").strip()
        fields = payload.get("fields", payload.get("field_name", []))
        if isinstance(fields, str):
            fields = [field.strip() for field in fields.split(",") if field.strip()]
        fields = list(dict.fromkeys(str(field).strip() for field in fields if str(field).strip()))
        interval = int(payload.get("interval_seconds", payload.get("samp_time", 60)))
        do_rename = bool(rename_results or payload.get("rename_results", False))
        if not name or not source_table or not fields:
            raise ValueError("name、source_database、source_table 和 fields 不能为空")
        if not _IDENT.match(name) or not _IDENT.match(source_table) or any(not _IDENT.match(field) for field in fields):
            raise ValueError("名称只能包含字母、数字、下划线或中文")
        if any(field.lower() in _RESULT_KEYS for field in fields):
            raise ValueError("sample_time 和 row_key 是结果表保留字段，不能作为采样字段")
        if len({field.lower() for field in fields}) != len(fields):
            raise ValueError("采样字段名不能仅大小写不同")
        if not source_database:
            raise ValueError("source_database 不能为空")
        self._validate_source_path(source_database)
        if interval < 0:
            raise ValueError("interval_seconds 不能为负数")
        filter_expr = str(payload.get("filter", payload.get("filter_value", "")))
        self._parse_filter(filter_expr)
        now = int(time.time())
        values = (
            name,
            source_database,
            source_table,
            ",".join(fields),
            1,
            interval,
            filter_expr,
            int(bool(payload.get("enabled", True))),
            0,
            now,
            now,
        )
        old_name: str | None = None
        migrated_files: list[str] = []
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            if samp_id is None:
                samp_id = int(
                    conn.execute(
                        "INSERT INTO samp_define(samp_name,source_database,source_table,field_name,samp_type,samp_time,filter_value,enabled,trigger_time,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    ).lastrowid
                )
            else:
                prev = conn.execute("SELECT samp_name FROM samp_define WHERE id=?", (samp_id,)).fetchone()
                if prev is not None:
                    old_name = str(prev["samp_name"])
                if do_rename and old_name and old_name != name:
                    # 先校验目标结果库不冲突并迁移，若失败不更新定义名
                    migrated_files = self._rename_result_databases(old_name, name)
                conn.execute(
                    "UPDATE samp_define SET samp_name=?,source_database=?,source_table=?,field_name=?,samp_type=?,samp_time=?,filter_value=?,enabled=?,trigger_time=?,updated_at=? WHERE id=?",
                    (*values[:9], values[10], samp_id),
                )
        self._invalidate_definition_cache()
        result = self.get_definition(samp_id)  # type: ignore[return-value]
        if migrated_files:
            result["migrated_results"] = migrated_files
        return result

    def export_definitions(self) -> dict[str, Any]:
        """导出全部采样定义为可迁移的 JSON 结构。"""
        items = [
            {
                "name": d["samp_name"],
                "source_database": d["source_database"],
                "source_table": d["source_table"],
                "fields": d["fields"],
                "interval_seconds": d["interval_seconds"],
                "filter": d["filter"],
                "enabled": bool(d["enabled"]),
            }
            for d in self.list_definitions()
        ]
        return {"version": 1, "exported_at": int(time.time()), "count": len(items), "items": items}

    def import_definitions(self, items: Iterable[dict[str, Any]], mode: str = "skip") -> dict[str, Any]:
        """批量导入采样定义，支持 skip（跳过同名）或 overwrite（覆盖同名）。"""
        mode_norm = (mode or "skip").lower()
        if mode_norm not in ("skip", "overwrite"):
            raise ValueError("mode 仅支持 skip 或 overwrite")
        existing = {d["samp_name"]: d["id"] for d in self.list_definitions()}
        created, updated, skipped = 0, 0, 0
        errors: list[dict[str, Any]] = []
        for idx, raw in enumerate(items):
            if not isinstance(raw, dict):
                errors.append({"index": idx, "name": "", "error": "条目必须是 JSON 对象"})
                continue
            name = str(raw.get("name") or raw.get("samp_name") or "").strip()
            try:
                if name in existing:
                    if mode_norm == "skip":
                        skipped += 1
                        continue
                    self.upsert_definition(raw, existing[name])
                    updated += 1
                else:
                    saved = self.upsert_definition(raw)
                    existing[saved["samp_name"]] = saved["id"]
                    created += 1
            except (ValueError, sqlite3.Error) as exc:
                errors.append({"index": idx, "name": name, "error": str(exc)})
        return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}

    def delete_definition(self, samp_id: int) -> bool:
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            deleted = conn.execute("DELETE FROM samp_define WHERE id=?", (samp_id,)).rowcount > 0
        if deleted:
            self._invalidate_definition_cache()
        return deleted

    def set_enabled(self, samp_id: int, enabled: bool) -> dict[str, Any] | None:
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            conn.execute("UPDATE samp_define SET enabled=?,updated_at=? WHERE id=?", (int(enabled), int(time.time()), samp_id))
        self._invalidate_definition_cache()
        return self.get_definition(samp_id)

    def set_enabled_batch(self, samp_ids: Iterable[int], enabled: bool) -> int:
        """批量启用或停用采样定义。"""
        ids = list(dict.fromkeys(int(item) for item in samp_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = int(time.time())
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            updated = conn.execute(
                f"UPDATE samp_define SET enabled=?,updated_at=? WHERE id IN ({placeholders})",
                [int(enabled), now, *ids],
            ).rowcount
        if updated:
            self._invalidate_definition_cache()
        return int(updated)

    @staticmethod
    def _quote(name: str) -> str:
        if not _IDENT.match(name):
            raise ValueError(f"非法标识符: {name}")
        return f'"{name}"'

    @staticmethod
    def _allowed_source_dirs() -> list[Path]:
        raw = os.getenv("SAMP_ALLOWED_SOURCE_DIRS", "").strip()
        if not raw:
            return []
        parts = [item.strip() for item in re.split(r"[;,]", raw) if item.strip()]
        return [Path(part).expanduser().resolve() for part in parts]

    def _validate_source_path(self, source_database: str) -> str:
        allowed = self._allowed_source_dirs()
        if not allowed:
            return source_database
        resolved = Path(source_database).expanduser().resolve()
        for base in allowed:
            try:
                resolved.relative_to(base)
                return source_database
            except ValueError:
                continue
        raise ValueError("源数据库路径不在 SAMP_ALLOWED_SOURCE_DIRS 允许范围内")

    def _source_database(self, source_database: str | None) -> str:
        target = str(source_database or self.default_source_db)
        return self._validate_source_path(target)

    def list_source_databases(self) -> list[str]:
        """扫描可用源 SQLite 数据库文件（排除定义库与年度结果库）。"""
        candidates: list[str] = []
        seen: set[str] = set()
        def_norm = os.path.normcase(str(Path(self.definition_db).expanduser().resolve()))

        def add_path(path: Path) -> None:
            resolved = path.expanduser().resolve()
            norm = os.path.normcase(str(resolved))
            if norm in seen or norm == def_norm:
                return
            if _RESULT_FILE.fullmatch(resolved.name):
                return
            seen.add(norm)
            candidates.append(str(resolved).replace("\\", "/"))

        default_path = Path(self.default_source_db)
        add_path(default_path)

        scan_dirs = [self.result_dir, default_path.parent, *self._allowed_source_dirs()]
        for directory in scan_dirs:
            if not directory.exists() or not directory.is_dir():
                continue
            for db_file in sorted(directory.glob("*.db")):
                if db_file.is_file():
                    add_path(db_file)

        # 同时纳入已在采样定义中使用过的源库路径
        for definition in self.list_definitions():
            src = definition.get("source_database")
            if src:
                try:
                    self._validate_source_path(src)
                    add_path(Path(src))
                except ValueError:
                    pass
        return candidates

    def source_tables(self, source_database: str | None = None) -> list[str]:
        with self._connect_readonly(self._source_database(source_database)) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [row[0] for row in rows if row[0].lower() not in REGISTRY_TABLES]

    def _source_table(self, source_table: str) -> str:
        if source_table.lower() in REGISTRY_TABLES:
            raise ValueError("不能使用 RTDB 内置结构表作为数据源")
        return self._quote(source_table)

    def source_fields(self, source_database: str | None, source_table: str) -> list[str]:
        with self._connect_readonly(self._source_database(source_database)) as conn:
            rows = conn.execute(f"PRAGMA table_info({self._source_table(source_table)})").fetchall()
        return [str(row[1]) for row in rows]

    @classmethod
    def _parse_filter(cls, expression: str) -> list[tuple[str, str, list[str]]]:
        """解析白名单安全过滤条件。

        支持 `=`, `!=`, `<>`, `>`, `>=`, `<`, `<=`, `IN (...)`, `NOT IN (...)`，
        子句之间用 `AND` 或 `,` 连接。
        """
        clauses: list[tuple[str, str, list[str]]] = []
        for raw_clause in _split_filter_clauses(expression):
            in_match = _CLAUSE_IN.match(raw_clause)
            if in_match:
                field = in_match.group(1)
                op = "NOT IN" if "not" in in_match.group(2).lower() else "IN"
                vals = _split_csv_values(in_match.group(3))
                if not vals:
                    raise ValueError(f"过滤条件 {field} {op} 列表不能为空")
                clauses.append((field, op, vals))
                continue
            cmp_match = _CLAUSE_CMP.match(raw_clause)
            if cmp_match:
                field = cmp_match.group(1)
                op = "!=" if cmp_match.group(2) == "<>" else cmp_match.group(2)
                value = cmp_match.group(3).strip()
                clauses.append((field, op, [value]))
                continue
            raise ValueError("过滤条件仅支持 field (=|!=|>|>=|<|<=|in|not in) value，并用 and 或逗号连接")
        return clauses

    def _build_filter_sql(
        self, expression: str, col_types: dict[str, str]
    ) -> tuple[str, list[Any], list[str]]:
        """把已解析的过滤条件编译为参数化 SQL WHERE 子句。"""
        parsed = self._parse_filter(expression)
        if not parsed:
            return "", [], []
        missing = [field for field, _, _ in parsed if field not in col_types]
        if missing:
            raise ValueError(f"源表缺少过滤字段: {', '.join(dict.fromkeys(missing))}")
        parts: list[str] = []
        params: list[Any] = []
        fields: list[str] = []
        for field, op, raw_values in parsed:
            fields.append(field)
            sql_type = col_types[field]
            quoted = self._quote(field)
            if op in ("IN", "NOT IN"):
                placeholders = ", ".join("?" for _ in raw_values)
                parts.append(f"{quoted} {op} ({placeholders})")
                params.extend(_coerce_param(val, sql_type) for val in raw_values)
            else:
                parts.append(f"{quoted} {op} ?")
                params.append(_coerce_param(raw_values[0], sql_type))
        return " AND ".join(parts), params, list(dict.fromkeys(fields))

    def inspect_source(
        self,
        source_database: str | None,
        source_table: str,
        limit: int = 20,
        filter_expr: str = "",
    ) -> dict[str, Any]:
        """读取源表列信息、总行数与样例预览，支持实时测试过滤条件命中效果。"""
        database = self._source_database(source_database)
        table = self._source_table(source_table)
        with self._connect_readonly(database) as conn:
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {database}:{source_table}")
            col_types = {str(row[1]): _normalize_sql_type(str(row[2])) for row in info}
            total_rows = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            where_sql, params, _ = self._build_filter_sql(filter_expr, col_types)
            if where_sql:
                matched_rows = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params).fetchone()[0]
                )
            else:
                matched_rows = total_rows
            safe_limit = max(1, min(int(limit), 100))
            query_sql = f"SELECT * FROM {table}{(' WHERE ' + where_sql) if where_sql else ''} LIMIT ?"
            rows = [dict(row) for row in conn.execute(query_sql, [*params, safe_limit]).fetchall()]
        return {
            "source_database": database,
            "source_table": source_table,
            "fields": columns,
            "field_types": col_types,
            "total_rows": total_rows,
            "matched_rows": matched_rows,
            "rows": rows,
        }

    def replace_source_rows(
        self,
        source_table: str,
        rows: Iterable[dict[str, Any]],
        source_database: str | None = None,
    ) -> int:
        """测试/导入辅助：把数据写入调用方指定的真实表名。"""
        table = self._source_table(source_table)
        database = self._source_database(source_database)
        materialized = list(rows)
        if not materialized:
            return 0
        all_fields = sorted({str(key) for row in materialized for key in row.get("values", row.get("data", row)).keys()})
        if any(not _IDENT.match(field) for field in all_fields):
            raise ValueError("源字段名不合法")
        names = [field.lower() for field in all_fields]
        if "row_key" in names or len(set(names)) != len(names):
            raise ValueError("源字段不能重复或使用保留字段 row_key")
        with self._db_lock(database), self._connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            stored_name = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
                (source_table,),
            ).fetchone()
            if stored_name:
                source_table = stored_name[0]
                table = self._source_table(source_table)
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1].lower() for row in info}
            if not existing:
                columns = ", ".join(["row_key TEXT NOT NULL PRIMARY KEY", *[f"{self._quote(field)} REAL" for field in all_fields]])
                conn.execute(f"CREATE TABLE {table} ({columns})")
            else:
                for field in all_fields:
                    if field.lower() not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {self._quote(field)} REAL")
            sync_table(conn, database, source_table, conn.execute(f"PRAGMA table_info({table})").fetchall())
            columns = ["row_key", *all_fields]
            placeholders = ",".join("?" for _ in columns)
            for index, row in enumerate(materialized):
                key = str(row.get("row_key", row.get("key", index)))
                values = row.get("values", row.get("data", row))
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(self._quote(c) for c in columns)}) VALUES ({placeholders})",
                    [key, *[values.get(field) for field in all_fields]],
                )
        return len(materialized)

    def _read_source(
        self,
        source_database: str,
        source_table: str,
        expression: str,
        fields: list[str],
    ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
        """读取源表数据并同时返回每个采样字段的源列声明类型。"""
        with self._connect_readonly(self._source_database(source_database)) as conn:
            table = self._source_table(source_table)
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {source_database}:{source_table}")
            col_types = {str(row[1]): _normalize_sql_type(str(row[2])) for row in info}
            where_sql, params, filter_fields = self._build_filter_sql(expression, col_types)
            missing = [field for field in [*fields, *filter_fields] if field not in col_types]
            if missing:
                raise ValueError(f"源表缺少字段: {', '.join(dict.fromkeys(missing))}")
            primary_keys = [str(row[1]) for row in sorted(info, key=lambda item: item[5]) if row[5]]
            key_columns = ["row_key"] if "row_key" in columns else primary_keys
            selected = list(dict.fromkeys(fields))
            for key_column in key_columns:
                if key_column not in selected:
                    selected.append(key_column)
            select_columns = [self._quote(field) for field in selected]
            if not key_columns:
                # 普通 SQLite 表具备 rowid；WITHOUT ROWID 表则必然具备主键
                select_columns.append('rowid AS "__samp_rowid"')
            sql = f"SELECT {', '.join(select_columns)} FROM {table}{(' WHERE ' + where_sql) if where_sql else ''}"
            rows = conn.execute(sql, params).fetchall()
        result: list[tuple[str, dict[str, Any]]] = []
        key_cols = key_columns or ["__samp_rowid"]
        for row in rows:
            values = {field: row[field] for field in fields}
            key = "|".join(str(row[column]) for column in key_cols)
            result.append((key, values))
        field_types = {field: col_types[field] for field in fields}
        return result, field_types

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
        started = time.perf_counter()
        sampled_at = int(time.time() if sampled_at is None else sampled_at)
        definition = self._raw_definition(samp_id)
        fields = [field for field in definition["field_name"].split(",") if field]
        rows, field_types = self._read_source(
            definition["source_database"] or self.default_source_db,
            definition["source_table"],
            definition["filter_value"],
            fields,
        )
        table = self._result_name(definition["samp_name"], sampled_at)
        db_path = self.result_dir / f"{table}.db"
        table_sql = self._quote(table)
        quoted_fields = [self._quote(field) for field in fields]
        db_key = os.path.normcase(str(db_path.resolve()))

        with self._db_lock(db_path), self._connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            need_schema_sync = True
            with self._schema_cache_lock:
                cached = self._synced_result_schemas.get(db_key)
                if cached is not None and db_path.exists():
                    if all(field.lower() in cached for field in fields):
                        need_schema_sync = False

            if need_schema_sync:
                info = conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
                if not info:
                    col_sql = ", ".join(
                        [
                            "sample_time INTEGER NOT NULL",
                            "row_key TEXT NOT NULL",
                            *(f"{quoted} {field_types[field]}" for field, quoted in zip(fields, quoted_fields)),
                        ]
                    )
                    conn.execute(f"CREATE TABLE {table_sql} ({col_sql}, PRIMARY KEY(sample_time,row_key))")
                    conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_row_key_time" ON {table_sql} (row_key, sample_time)')
                else:
                    existing = {row[1].lower() for row in info}
                    for field, quoted in zip(fields, quoted_fields):
                        if field.lower() not in existing:
                            conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {quoted} {field_types[field]}")
                    conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_row_key_time" ON {table_sql} (row_key, sample_time)')
                latest_info = conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
                sync_table(conn, db_path, table, latest_info)
                with self._schema_cache_lock:
                    self._synced_result_schemas[db_key] = {
                        str(row[1]).lower(): _normalize_sql_type(str(row[2])) for row in latest_info
                    }

            columns = ["sample_time", "row_key", *quoted_fields]
            placeholders = ",".join("?" for _ in columns)
            updates = ", ".join(f"{field}=excluded.{field}" for field in quoted_fields)
            try:
                conn.executemany(
                    f"INSERT INTO {table_sql} ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(sample_time,row_key) DO UPDATE SET {updates}",
                    ((sampled_at, key, *(values[field] for field in fields)) for key, values in rows),
                )
            except sqlite3.Error:
                with self._schema_cache_lock:
                    self._synced_result_schemas.pop(db_key, None)
                raise

        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "samp_id": samp_id,
            "samp_name": definition["samp_name"],
            "sampled_at": sampled_at,
            "rows_written": len(rows),
            "duration_ms": duration_ms,
            "database": str(db_path),
            "table": table,
        }

    def _resolve_query_fields(self, definition: dict[str, Any], fields: list[str] | None) -> list[str]:
        if fields is None:
            return list(definition["fields"])
        invalid = [field for field in fields if field not in definition["fields"] or not _IDENT.match(field)]
        if invalid:
            raise ValueError(f"查询字段不在采样定义中: {', '.join(invalid)}")
        selected = list(dict.fromkeys(fields))
        if not selected:
            raise ValueError("至少选择一个查询字段")
        return selected

    def _year_sequence(self, start: int, end: int, order: str = "asc") -> list[int]:
        first_year = time.gmtime(start).tm_year
        last_year = time.gmtime(end).tm_year
        years = list(range(first_year, last_year + 1))
        if order == "desc":
            years.reverse()
        return years

    def query_samples(
        self,
        samp_id: int,
        start: int,
        end: int,
        fields: list[str] | None = None,
        limit: int = 1000,
        offset: int = 0,
        row_keys: list[str] | None = None,
        order: str = "asc",
    ) -> dict[str, Any]:
        order_norm = order.lower()
        if order_norm not in ("asc", "desc"):
            raise ValueError("order 仅支持 asc 或 desc")
        limit = max(1, min(int(limit), 10000))
        offset = max(0, int(offset))
        requested_offset = offset
        raw = self._raw_definition(samp_id)
        definition = self._public(raw, self.default_source_db)
        selected = self._resolve_query_fields(definition, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]

        rows: list[dict[str, Any]] = []
        tables: list[str] = []
        total_rows = 0
        dir_sql = "DESC" if order_norm == "desc" else "ASC"

        for year in self._year_sequence(start, end, order_norm):
            probe = calendar.timegm((year, 1, 1, 0, 0, 0))
            table = self._result_name(raw["samp_name"], probe)
            db_path = self.result_dir / f"{table}.db"
            if not db_path.exists():
                continue
            with self._connect_readonly(db_path) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                tables.append(table)
                where_parts = ["sample_time BETWEEN ? AND ?"]
                params: list[Any] = [start, end]
                if clean_keys:
                    placeholders = ",".join("?" for _ in clean_keys)
                    where_parts.append(f"row_key IN ({placeholders})")
                    params.extend(clean_keys)
                where = " AND ".join(where_parts)
                count = int(conn.execute(f"SELECT COUNT(*) FROM {self._quote(table)} WHERE {where}", params).fetchone()[0])
                total_rows += count
                if offset >= count:
                    offset -= count
                elif len(rows) < limit:
                    existing = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({self._quote(table)})")}
                    field_sql = ", ".join(
                        f"{self._quote(field) if field.lower() in existing else 'NULL'} AS {self._quote(field)}"
                        for field in selected
                    )
                    page_size = limit - len(rows)
                    rows.extend(
                        dict(row)
                        for row in conn.execute(
                            f"SELECT sample_time,row_key,{field_sql} FROM {self._quote(table)} "
                            f"WHERE {where} ORDER BY sample_time {dir_sql}, row_key {dir_sql} LIMIT ? OFFSET ?",
                            [*params, page_size, offset],
                        ).fetchall()
                    )
                    offset = 0

        rows.sort(key=lambda row: (row["sample_time"], row["row_key"]), reverse=(order_norm == "desc"))
        return {
            "definition": definition,
            "columns": ["sample_time", "row_key", *selected],
            "rows": rows,
            "tables": tables,
            "total_rows": total_rows,
            "limit": limit,
            "offset": requested_offset,
            "order": order_norm,
            "row_keys": clean_keys,
        }

    def query_keys(self, samp_id: int, start: int, end: int, limit: int = 500) -> dict[str, Any]:
        """获取指定时间范围内出现过的去重 row_key 列表。"""
        safe_limit = max(1, min(int(limit), 5000))
        raw = self._raw_definition(samp_id)
        found: set[str] = set()
        for year in self._year_sequence(start, end, "asc"):
            probe = calendar.timegm((year, 1, 1, 0, 0, 0))
            table = self._result_name(raw["samp_name"], probe)
            db_path = self.result_dir / f"{table}.db"
            if not db_path.exists():
                continue
            with self._connect_readonly(db_path) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                for row in conn.execute(
                    f"SELECT DISTINCT row_key FROM {self._quote(table)} WHERE sample_time BETWEEN ? AND ? ORDER BY row_key LIMIT ?",
                    (start, end, safe_limit),
                ).fetchall():
                    found.add(str(row[0]))
                    if len(found) >= safe_limit:
                        break
            if len(found) >= safe_limit:
                break
        keys = sorted(found)
        return {"keys": keys, "total": len(keys)}

    def query_series(
        self,
        samp_id: int,
        start: int,
        end: int,
        fields: list[str] | None = None,
        row_keys: list[str] | None = None,
        bucket_seconds: int = 0,
        agg: str = "avg",
        max_points: int = 600,
        max_raw_points: int = 20000,
    ) -> dict[str, Any]:
        """面向时序曲线图的聚合/降采样查询，按 row_key 返回多系列数据与分组统计。

        - avg/min/max/last 分桶聚合均在 SQLite 库内执行；
        - 原始点模式受 max_raw_points 上限保护并标记 truncated，避免百万行查询打爆内存。
        """
        agg_norm = (agg or "avg").lower()
        if agg_norm not in _ALLOWED_AGGS:
            raise ValueError(f"不支持的聚合方式: {agg}")
        max_points = max(50, min(int(max_points), 5000))
        max_raw = max(100, min(int(max_raw_points), 200000))
        bucket = max(0, int(bucket_seconds))

        raw = self._raw_definition(samp_id)
        definition = self._public(raw, self.default_source_db)
        selected = self._resolve_query_fields(definition, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]

        # 统计命中点数，自动决定是否需要分桶降采样
        tables: list[str] = []
        total_raw_rows = 0
        key_set: set[str] = set()
        for year in self._year_sequence(start, end, "asc"):
            probe = calendar.timegm((year, 1, 1, 0, 0, 0))
            table = self._result_name(raw["samp_name"], probe)
            db_path = self.result_dir / f"{table}.db"
            if not db_path.exists():
                continue
            with self._connect_readonly(db_path) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                tables.append(table)
                where_parts = ["sample_time BETWEEN ? AND ?"]
                params: list[Any] = [start, end]
                if clean_keys:
                    placeholders = ",".join("?" for _ in clean_keys)
                    where_parts.append(f"row_key IN ({placeholders})")
                    params.extend(clean_keys)
                where = " AND ".join(where_parts)
                total_raw_rows += int(conn.execute(f"SELECT COUNT(*) FROM {self._quote(table)} WHERE {where}", params).fetchone()[0])
                for row in conn.execute(
                    f"SELECT DISTINCT row_key FROM {self._quote(table)} WHERE {where} ORDER BY row_key LIMIT 200",
                    params,
                ).fetchall():
                    key_set.add(str(row[0]))

        effective_keys = clean_keys or sorted(key_set)
        span = max(1, end - start)
        per_key_estimate = total_raw_rows / max(1, len(effective_keys))
        effective_bucket = bucket
        if agg_norm != "raw" and effective_bucket == 0 and per_key_estimate > max_points:
            effective_bucket = max(1, int(span // max_points) + 1)
        if agg_norm == "raw":
            effective_bucket = 0

        # series_map[row_key] = dict[ts -> dict[field -> value]]
        series_map: dict[str, dict[int, dict[str, Any]]] = {}
        stats_acc: dict[str, dict[str, dict[str, Any]]] = {}
        raw_loaded = 0
        truncated = False

        def update_stats(rk: str, ts: int, field: str, value: Any) -> None:
            if value is None or value == "":
                return
            rk_stats = stats_acc.setdefault(rk, {})
            item = rk_stats.setdefault(
                field,
                {"count": 0, "num_count": 0, "sum": 0.0, "min": None, "max": None, "latest": None, "latest_time": -1},
            )
            item["count"] += 1
            if ts >= item["latest_time"]:
                item["latest_time"] = ts
                item["latest"] = value
            try:
                num = float(value)
                if num == num:  # 排除 NaN
                    item["num_count"] += 1
                    item["sum"] += num
                    item["min"] = num if item["min"] is None else min(item["min"], num)
                    item["max"] = num if item["max"] is None else max(item["max"], num)
            except (TypeError, ValueError):
                pass

        for year in self._year_sequence(start, end, "asc"):
            if effective_bucket == 0 and raw_loaded >= max_raw:
                truncated = True
                break
            probe = calendar.timegm((year, 1, 1, 0, 0, 0))
            table = self._result_name(raw["samp_name"], probe)
            db_path = self.result_dir / f"{table}.db"
            if not db_path.exists():
                continue
            with self._connect_readonly(db_path) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                existing = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({self._quote(table)})")}
                where_parts = ["sample_time BETWEEN ? AND ?"]
                params = [start, end]
                if clean_keys:
                    placeholders = ",".join("?" for _ in clean_keys)
                    where_parts.append(f"row_key IN ({placeholders})")
                    params.extend(clean_keys)
                where = " AND ".join(where_parts)

                if effective_bucket > 0 and agg_norm in ("avg", "min", "max"):
                    fn = agg_norm.upper()
                    agg_cols = ", ".join(
                        f"{fn}({self._quote(f)}) AS {self._quote(f)}" if f.lower() in existing else f"NULL AS {self._quote(f)}"
                        for f in selected
                    )
                    sql = (
                        f"SELECT (sample_time / ?) * ? AS bucket_ts, row_key, {agg_cols} "
                        f"FROM {self._quote(table)} WHERE {where} "
                        f"GROUP BY bucket_ts, row_key ORDER BY bucket_ts ASC, row_key ASC"
                    )
                    for row in conn.execute(sql, [effective_bucket, effective_bucket, *params]).fetchall():
                        rk = str(row["row_key"])
                        ts = int(row["bucket_ts"])
                        point = series_map.setdefault(rk, {}).setdefault(ts, {})
                        for field in selected:
                            val = row[field]
                            point[field] = val
                            update_stats(rk, ts, field, val)
                elif effective_bucket > 0 and agg_norm == "last":
                    # SQLite 保证 GROUP BY 中裸列取自匹配 MAX(sample_time) 的那一行
                    field_sql = ", ".join(
                        f"{self._quote(f) if f.lower() in existing else 'NULL'} AS {self._quote(f)}"
                        for f in selected
                    )
                    sql = (
                        f"SELECT (sample_time / ?) * ? AS bucket_ts, row_key, MAX(sample_time) AS last_ts, {field_sql} "
                        f"FROM {self._quote(table)} WHERE {where} "
                        f"GROUP BY bucket_ts, row_key ORDER BY bucket_ts ASC, row_key ASC"
                    )
                    for row in conn.execute(sql, [effective_bucket, effective_bucket, *params]).fetchall():
                        rk = str(row["row_key"])
                        ts = int(row["bucket_ts"])
                        point = series_map.setdefault(rk, {}).setdefault(ts, {})
                        for field in selected:
                            val = row[field]
                            point[field] = val
                            update_stats(rk, int(row["last_ts"]), field, val)
                else:
                    remaining = max_raw - raw_loaded
                    field_sql = ", ".join(
                        f"{self._quote(f) if f.lower() in existing else 'NULL'} AS {self._quote(f)}"
                        for f in selected
                    )
                    sql = (
                        f"SELECT sample_time, row_key, {field_sql} FROM {self._quote(table)} "
                        f"WHERE {where} ORDER BY sample_time ASC, row_key ASC LIMIT ?"
                    )
                    fetched = conn.execute(sql, [*params, remaining + 1]).fetchall()
                    if len(fetched) > remaining:
                        truncated = True
                        fetched = fetched[:remaining]
                    raw_loaded += len(fetched)
                    for row in fetched:
                        rk = str(row["row_key"])
                        ts = int(row["sample_time"])
                        point = series_map.setdefault(rk, {}).setdefault(ts, {})
                        for field in selected:
                            val = row[field]
                            point[field] = val
                            update_stats(rk, ts, field, val)

        series_out: list[dict[str, Any]] = []
        for rk in sorted(series_map):
            points = series_map[rk]
            times = sorted(points)
            values_by_field: dict[str, list[Any]] = {
                field: [points[ts].get(field) for ts in times] for field in selected
            }
            rk_stats_out: dict[str, dict[str, Any]] = {}
            for field in selected:
                acc = stats_acc.get(rk, {}).get(field)
                if not acc:
                    rk_stats_out[field] = {"count": 0, "min": None, "max": None, "avg": None, "latest": None}
                else:
                    avg_val = round(acc["sum"] / acc["num_count"], 6) if acc["num_count"] else None
                    rk_stats_out[field] = {
                        "count": acc["count"],
                        "min": acc["min"],
                        "max": acc["max"],
                        "avg": avg_val,
                        "latest": acc["latest"],
                        "latest_time": acc["latest_time"] if acc["latest_time"] >= 0 else None,
                    }
            series_out.append(
                {
                    "row_key": rk,
                    "times": times,
                    "values": values_by_field,
                    "stats": rk_stats_out,
                }
            )

        return {
            "definition": definition,
            "fields": selected,
            "tables": tables,
            "total_raw_rows": total_raw_rows,
            "bucket_seconds": effective_bucket,
            "agg": agg_norm,
            "truncated": truncated,
            "max_raw_points": max_raw,
            "row_keys": effective_keys,
            "series": series_out,
        }

    def iter_query_rows(
        self,
        samp_id: int,
        start: int,
        end: int,
        fields: list[str] | None = None,
        row_keys: list[str] | None = None,
        order: str = "asc",
    ) -> tuple[dict[str, Any], list[str], Iterator[dict[str, Any]]]:
        """流式遍历满足查询条件的全部行，供全量 CSV 导出使用。"""
        order_norm = order.lower()
        if order_norm not in ("asc", "desc"):
            raise ValueError("order 仅支持 asc 或 desc")
        raw = self._raw_definition(samp_id)
        definition = self._public(raw, self.default_source_db)
        selected = self._resolve_query_fields(definition, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]
        columns = ["sample_time", "row_key", *selected]
        dir_sql = "DESC" if order_norm == "desc" else "ASC"

        def generator() -> Iterator[dict[str, Any]]:
            for year in self._year_sequence(start, end, order_norm):
                probe = calendar.timegm((year, 1, 1, 0, 0, 0))
                table = self._result_name(raw["samp_name"], probe)
                db_path = self.result_dir / f"{table}.db"
                if not db_path.exists():
                    continue
                with self._connect_readonly(db_path) as conn:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    existing = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({self._quote(table)})")}
                    field_sql = ", ".join(
                        f"{self._quote(f) if f.lower() in existing else 'NULL'} AS {self._quote(f)}"
                        for f in selected
                    )
                    where_parts = ["sample_time BETWEEN ? AND ?"]
                    params: list[Any] = [start, end]
                    if clean_keys:
                        placeholders = ",".join("?" for _ in clean_keys)
                        where_parts.append(f"row_key IN ({placeholders})")
                        params.extend(clean_keys)
                    where = " AND ".join(where_parts)
                    cursor = conn.execute(
                        f"SELECT sample_time,row_key,{field_sql} FROM {self._quote(table)} "
                        f"WHERE {where} ORDER BY sample_time {dir_sql}, row_key {dir_sql}",
                        params,
                    )
                    while True:
                        batch = cursor.fetchmany(1000)
                        if not batch:
                            break
                        for row in batch:
                            yield dict(row)

        return definition, columns, generator()

    def summary(self) -> dict[str, int]:
        with self._connect(self.definition_db) as conn:
            definitions, enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM samp_define").fetchone()
        result_databases = sum(
            1
            for path in self.result_dir.glob("samp_*.db")
            if path.is_file() and _RESULT_FILE.fullmatch(path.name)
        )
        return {"definitions": definitions, "enabled": enabled, "result_databases": result_databases}

    def status(self) -> dict[str, Any]:
        summary = self.summary()
        return {
            **summary,
            "items": [self.runtime_status(item["id"]) for item in self.list_definitions()],
        }

    def _inspect_result_db_meta(self, path: Path) -> dict[str, Any]:
        """按文件 (mtime_ns, size) 缓存年度结果库统计元数据，避免轮询反复全表 COUNT(*)。"""
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cache_key = os.path.normcase(str(path.resolve()))
        with self._meta_cache_lock:
            cached = self._result_meta_cache.get(cache_key)
            if cached is not None and cached[0] == stamp:
                return dict(cached[1])

        table = path.stem
        meta = {
            "size_bytes": stat.st_size,
            "modified_at": int(stat.st_mtime),
            "row_count": 0,
            "min_sample_time": None,
            "max_sample_time": None,
            "rtdb_registered": False,
        }
        try:
            with self._connect_readonly(path) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    cnt, mn, mx = conn.execute(
                        f"SELECT COUNT(*), MIN(sample_time), MAX(sample_time) FROM {self._quote(table)}"
                    ).fetchone()
                    meta["row_count"] = int(cnt or 0)
                    meta["min_sample_time"] = int(mn) if mn is not None else None
                    meta["max_sample_time"] = int(mx) if mx is not None else None
                meta["rtdb_registered"] = (
                    conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'").fetchone()
                    is not None
                )
        except sqlite3.Error:
            pass
        with self._meta_cache_lock:
            self._result_meta_cache[cache_key] = (stamp, dict(meta))
        return meta

    def runtime_status(self, samp_id: int) -> dict[str, Any]:
        definition = self._raw_definition(samp_id)
        prefix = f"samp_{definition['samp_name']}_"
        files: list[tuple[int, Path]] = []
        for path in sorted(self.result_dir.glob(f"{prefix}*.db")):
            if not path.is_file():
                continue
            suffix = path.stem[len(prefix):]
            if len(suffix) == 4 and suffix.isdigit():
                files.append((int(suffix), path))
        years = [year for year, _ in files]
        total_size = 0
        last_mtime: int | None = None
        total_rows = 0
        latest_sample_time: int | None = None
        for _, path in files:
            meta = self._inspect_result_db_meta(path)
            total_size += int(meta["size_bytes"])
            total_rows += int(meta["row_count"])
            mtime = int(meta["modified_at"])
            last_mtime = mtime if last_mtime is None else max(last_mtime, mtime)
            mx = meta["max_sample_time"]
            if mx is not None:
                latest_sample_time = mx if latest_sample_time is None else max(latest_sample_time, mx)

        return {
            "id": samp_id,
            "name": definition["samp_name"],
            "enabled": bool(definition["enabled"]),
            "interval_seconds": definition["samp_time"],
            "source_database": definition["source_database"] or self.default_source_db,
            "source_table": definition["source_table"],
            "result_years": years,
            "result_size_bytes": total_size,
            "total_rows": total_rows,
            "latest_sample_time": latest_sample_time,
            "last_result_at": latest_sample_time if latest_sample_time is not None else last_mtime,
        }

    def list_result_databases(self) -> list[dict[str, Any]]:
        """列出结果目录下所有年度结果库及其元数据、RTDB 登记状态与孤儿标记。"""
        defined_names = {item["samp_name"] for item in self.list_definitions()}
        items: list[dict[str, Any]] = []
        for path in sorted(self.result_dir.glob("samp_*.db")):
            if not path.is_file():
                continue
            match = _RESULT_FILE.fullmatch(path.name)
            if not match:
                continue
            samp_name, year_str = match.group(1), match.group(2)
            meta = self._inspect_result_db_meta(path)
            items.append(
                {
                    "filename": path.name,
                    "path": str(path).replace("\\", "/"),
                    "samp_name": samp_name,
                    "year": int(year_str),
                    "size_bytes": meta["size_bytes"],
                    "modified_at": meta["modified_at"],
                    "row_count": meta["row_count"],
                    "min_sample_time": meta["min_sample_time"],
                    "max_sample_time": meta["max_sample_time"],
                    "rtdb_registered": meta["rtdb_registered"],
                    "is_orphan": samp_name not in defined_names,
                }
            )
        return items

    def delete_result_database(self, filename: str) -> bool:
        """安全删除指定的年度结果库文件（仅允许删除匹配结果库命名规范的文件）。"""
        if "/" in filename or "\\" in filename or not _RESULT_FILE.fullmatch(filename):
            raise ValueError("非法的结果库文件名")
        target = (self.result_dir / filename).resolve()
        root = self.result_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("非法的结果库路径") from exc
        if not target.exists() or not target.is_file():
            return False
        db_key = os.path.normcase(str(target))
        with self._db_lock(target):
            target.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                extra = Path(f"{target}{suffix}")
                if extra.exists():
                    extra.unlink(missing_ok=True)
        with self._schema_cache_lock:
            self._synced_result_schemas.pop(db_key, None)
        with self._meta_cache_lock:
            self._result_meta_cache.pop(db_key, None)
        return True
