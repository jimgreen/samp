from __future__ import annotations

import calendar
import json
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
_SKIP_SCAN_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".playwright-cli", "tmp_results"}
_SOURCE_DB_SUFFIXES = {".db", ".sqlite3", ".sqlite"}
ROOT_DIR = Path(os.getenv("SAMP_ROOT_DIR", r"D:\codex\workspace"))
SQLITE_DIR = Path(os.getenv("SAMP_SQLITE_DIR", str(ROOT_DIR / "samp")))
DEFAULT_DEFINITION_DB: Path = (
    SQLITE_DIR / "samp_define.db" if "SAMP_SQLITE_DIR" in os.environ else Path("samp/samp_define.db")
)
DEFAULT_SOURCE_DB: Path = (
    SQLITE_DIR / "source.db" if "SAMP_SQLITE_DIR" in os.environ else Path("source.db")
)
DEFAULT_RESULT_DIR: Path = SQLITE_DIR if "SAMP_SQLITE_DIR" in os.environ else Path("samp")
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
        definition_db: str | Path = DEFAULT_DEFINITION_DB,
        source_db: str | Path = DEFAULT_SOURCE_DB,
        result_dir: str | Path = DEFAULT_RESULT_DIR,
        root_dir: str | Path | None = None,
    ):
        def_path = Path(definition_db)
        if root_dir is not None:
            self.root_dir = Path(root_dir).expanduser().resolve()
        elif def_path.is_absolute():
            self.root_dir = def_path.expanduser().resolve().parent
        else:
            self.root_dir = ROOT_DIR.expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

        self.definition_db = str(self.resolve_path(definition_db))
        self.default_source_db = str(self.resolve_path(source_db))
        self.result_dir = self.resolve_path(result_dir)
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
        self._synced_result_schemas: dict[tuple[str, str], dict[str, str]] = {}
        self._meta_cache_lock = threading.Lock()
        self._result_meta_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._scan_cache_lock = threading.Lock()
        self._dir_cache: tuple[float, list[str]] | None = None
        self._source_db_cache: tuple[float, list[str]] | None = None
        self._scan_ttl_seconds = 10.0

        self.initialize()
        self._sync_existing_results()

    def resolve_path(self, path: str | Path) -> Path:
        """将相对路径基于 root_dir 解析为绝对路径；绝对路径保持原样并规范化。"""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.root_dir / p
        return p.resolve()

    def to_relative_path(self, path: str | Path) -> str:
        """位于 root_dir 内的路径转为 POSIX 风格相对路径；外部路径保留 POSIX 绝对路径。"""
        resolved = self.resolve_path(path)
        try:
            rel = resolved.relative_to(self.root_dir)
            return rel.as_posix() if str(rel) != "." else "."
        except ValueError:
            return resolved.as_posix()

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
        with self._scan_cache_lock:
            self._source_db_cache = None

    def _definition_file_stamp(self) -> tuple[int, int, int]:
        try:
            st = os.stat(self.definition_db)
            return (self._def_version, st.st_mtime_ns, st.st_size)
        except OSError:
            return (self._def_version, 0, 0)

    _wal_initialized_dbs: set[str] = set()

    @classmethod
    @contextmanager
    def _connect(cls, path: str | Path) -> Iterator[sqlite3.Connection]:
        db_str = str(path)
        conn = sqlite3.connect(db_str, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if db_str not in cls._wal_initialized_dbs:
                conn.execute("PRAGMA journal_mode=WAL")
                cls._wal_initialized_dbs.add(db_str)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.Error:
            pass
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
            col_names = {row[1] for row in columns}
            if "source_database" not in col_names:
                conn.execute("ALTER TABLE samp_define ADD COLUMN source_database TEXT NOT NULL DEFAULT ''")
                col_names.add("source_database")
            # 仅对标准定义库自动补齐 target_dir/target_name/items_json，兼容旧版极简测试库
            if "source_table" in col_names:
                if "target_dir" not in col_names:
                    conn.execute("ALTER TABLE samp_define ADD COLUMN target_dir TEXT NOT NULL DEFAULT ''")
                if "target_name" not in col_names:
                    conn.execute("ALTER TABLE samp_define ADD COLUMN target_name TEXT NOT NULL DEFAULT ''")
                if "items_json" not in col_names:
                    conn.execute("ALTER TABLE samp_define ADD COLUMN items_json TEXT NOT NULL DEFAULT ''")
            columns = conn.execute("PRAGMA table_info(samp_define)").fetchall()
            sync_table(conn, self.definition_db, "samp_define", columns)
        self._invalidate_definition_cache()

    def _sync_existing_results(self) -> None:
        seen: set[str] = set()
        candidates: list[Path] = []
        for path in self.result_dir.glob("samp_*.db"):
            if _RESULT_FILE.fullmatch(path.name) and path.is_file():
                candidates.append(path)
        try:
            for item in self.list_definitions():
                spec = self._parse_target_spec(item["samp_name"], item.get("target_dir", ""), item.get("target_name", ""))
                for _, path in self._list_spec_result_files(spec):
                    candidates.append(path)
        except (sqlite3.Error, ValueError):
            pass

        for path in candidates:
            norm = os.path.normcase(str(path.resolve()))
            if norm in seen or not path.is_file():
                continue
            seen.add(norm)
            try:
                with self._db_lock(path), self._connect(path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    biz_tables = [
                        str(r[0])
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                        if str(r[0]).lower() not in REGISTRY_TABLES and _IDENT.match(str(r[0]))
                    ]
                    for table in biz_tables:
                        columns = conn.execute(f"PRAGMA table_info({self._quote(table)})").fetchall()
                        keys = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
                        if keys == ("sample_time", "row_key"):
                            sync_table(conn, path, table, columns)
            except (sqlite3.Error, ValueError) as exc:
                LOGGER.warning("无法同步历史结果库 %s 的 RTDB 结构：%s", path, exc)

    def _parse_target_spec(self, samp_name: str, target_dir: str = "", target_name: str = "") -> dict[str, Any]:
        """解析并校验目标库目录与命名规则。

        规则：
        - target_dir 为空 -> 默认 result_dir；非空 -> 相对 root_dir 解析；
        - target_name 为空 -> 按年分库 samp_{samp_name}_{year}.db；
        - target_name 含 {year} -> 按年分库模板（去掉可选 .db 后缀）；
        - target_name 以 .db 结尾且不含 {year} -> 单库模式 {stem}.db（不分年）；
        - 其他非空 target_name -> 按年分库前缀 {target_name}_{year}.db。
        """
        raw_dir = str(target_dir or "").strip()
        if raw_dir:
            dir_path = self.resolve_path(raw_dir)
            dir_rel = self.to_relative_path(dir_path)
        else:
            dir_path = self.result_dir
            dir_rel = self.to_relative_path(self.result_dir)

        raw_name = str(target_name or "").strip()
        if "/" in raw_name or "\\" in raw_name:
            raise ValueError("target_name 不能包含路径分隔符，请使用 target_dir 指定目录")

        if not raw_name:
            template = f"samp_{samp_name}_{{year}}"
            yearly = True
            sample_stem = template.replace("{year}", "2026")
            if not _IDENT.match(sample_stem):
                raise ValueError("采样点名生成的结果表名不合法")
            single_stem = ""
            pattern_re = re.compile(rf"^{re.escape(f'samp_{samp_name}_')}(\d{{4}})\.db$")
        elif "{year}" in raw_name.lower():
            normalized = re.sub(r"\{year\}", "{year}", raw_name, flags=re.I)
            if normalized.lower().endswith(".db"):
                normalized = normalized[:-3]
            sample_stem = normalized.replace("{year}", "2026")
            if not _IDENT.match(sample_stem):
                raise ValueError(f"目标库模板生成的结果表名不合法: {raw_name}")
            template = normalized
            yearly = True
            single_stem = ""
            parts = normalized.split("{year}")
            regex_str = "^" + r"(\d{4})".join(re.escape(p) for p in parts) + r"\.db$"
            pattern_re = re.compile(regex_str)
        elif raw_name.lower().endswith(".db"):
            stem = raw_name[:-3]
            if not _IDENT.match(stem):
                raise ValueError(f"目标库名不合法: {raw_name}")
            template = stem
            yearly = False
            single_stem = stem
            pattern_re = re.compile(rf"^{re.escape(stem)}\.db$")
        else:
            sample_stem = f"{raw_name}_2026"
            if not _IDENT.match(sample_stem):
                raise ValueError(f"目标库前缀不合法: {raw_name}")
            template = f"{raw_name}_{{year}}"
            yearly = True
            single_stem = ""
            pattern_re = re.compile(rf"^{re.escape(f'{raw_name}_')}(\d{{4}})\.db$")

        # 禁止与定义库或运行态库重名冲突
        def_norm = os.path.normcase(str(Path(self.definition_db).resolve()))
        rt_norm = os.path.normcase(str(Path(self.definition_db).resolve().with_name("samp_runtime.db")))
        probe_file = (dir_path / f"{(single_stem if not yearly else template.replace('{year}', '2026'))}.db").resolve()
        if os.path.normcase(str(probe_file)) in (def_norm, rt_norm):
            raise ValueError("目标库不能与 samp_define.db 或 samp_runtime.db 重名")

        conflict_key = (os.path.normcase(str(dir_path.resolve())), template.lower(), yearly)
        return {
            "dir_path": dir_path,
            "dir_rel": dir_rel,
            "raw_dir": raw_dir,
            "raw_name": raw_name,
            "yearly": yearly,
            "template": template,
            "single_stem": single_stem,
            "pattern_re": pattern_re,
            "conflict_key": conflict_key,
            "preview_rel": self.to_relative_path(dir_path / f"{template}.db"),
        }

    def _target_for_timestamp(self, spec: dict[str, Any], sampled_at: int) -> tuple[Path, str]:
        if spec["yearly"]:
            year = time.gmtime(sampled_at).tm_year
            table = spec["template"].replace("{year}", str(year))
        else:
            table = spec["single_stem"]
        if not _IDENT.match(table):
            raise ValueError(f"生成的结果表名不合法: {table}")
        return spec["dir_path"] / f"{table}.db", table

    def _spec_from_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        keys = row.keys() if isinstance(row, sqlite3.Row) else row
        target_dir = row["target_dir"] if "target_dir" in keys else ""
        target_name = row["target_name"] if "target_name" in keys else ""
        return self._parse_target_spec(str(row["samp_name"]), str(target_dir or ""), str(target_name or ""))

    def _drop_schema_cache_for_db(self, db_key: str) -> None:
        with self._schema_cache_lock:
            stale = [key for key in self._synced_result_schemas if key[0] == db_key]
            for key in stale:
                self._synced_result_schemas.pop(key, None)

    def _normalize_items(
        self,
        raw_items: Any,
        fallback: dict[str, Any] | None = None,
        single_stem: str = "",
    ) -> list[dict[str, Any]]:
        """校验并规范化单采样点下的一条或多条采集项（一库多表模型）。"""
        source_list: list[dict[str, Any]]
        if isinstance(raw_items, list) and len(raw_items) > 0:
            source_list = [item for item in raw_items if isinstance(item, dict)]
            if len(source_list) != len(raw_items):
                raise ValueError("items 中每一项都必须是对象")
        elif fallback is not None:
            source_list = [fallback]
        else:
            raise ValueError("采样点至少需要一条采集项")

        normalized: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        used_tables: set[str] = set()
        auto_seq = 1

        for idx, raw in enumerate(source_list):
            src_db = str(raw.get("source_database") or self.default_source_db).strip()
            src_table = str(raw.get("source_table") or raw.get("table") or "").strip()
            tgt_table = str(raw.get("target_table") or "").strip()
            fields_raw = raw.get("fields", raw.get("field_name", []))
            if isinstance(fields_raw, str):
                fields_list = [f.strip() for f in fields_raw.split(",") if f.strip()]
            elif isinstance(fields_raw, list):
                fields_list = [str(f).strip() for f in fields_raw if str(f).strip()]
            else:
                fields_list = []
            fields = list(dict.fromkeys(fields_list))
            filter_expr = str(raw.get("filter", raw.get("filter_value", "")) or "")

            if not src_table or not fields:
                raise ValueError(f"第 {idx + 1} 条采集项的 source_table 和 fields 不能为空")
            if not _IDENT.match(src_table):
                raise ValueError("表名只能包含字母、数字、下划线或中文")
            if src_table.lower() in REGISTRY_TABLES:
                raise ValueError("不能使用 RTDB 内置结构表作为数据源")
            if "*" in fields:
                # 整表模式：["*"] 表示自动跟随源表全部合法业务列
                fields = ["*"]
            else:
                if any(not _IDENT.match(f) for f in fields):
                    raise ValueError("字段名只能包含字母、数字、下划线或中文")
                if any(f.lower() in _RESULT_KEYS for f in fields):
                    raise ValueError("sample_time 和 row_key 是结果表保留字段，不能作为采样字段")
                if len({f.lower() for f in fields}) != len(fields):
                    raise ValueError("同一采集项的采样字段名不能仅大小写不同")
            if not src_db:
                raise ValueError("source_database 不能为空")
            self._validate_source_path(src_db)
            src_db_rel = self.to_relative_path(src_db)
            self._parse_filter(filter_expr)

            # 第 1 条留空 target_table 表示默认写入库同名表（兼容历史）；第 2 条起留空默认用 source_table
            if not tgt_table and idx > 0:
                tgt_table = src_table
            if tgt_table:
                if not _IDENT.match(tgt_table):
                    raise ValueError(f"目标表名不合法: {tgt_table}")
                if tgt_table.lower() in REGISTRY_TABLES:
                    raise ValueError(f"目标表名不能使用 RTDB 保留表名: {tgt_table}")

            table_key = tgt_table.lower() if tgt_table else "__default__"
            if single_stem and tgt_table and tgt_table.lower() == single_stem.lower() and "__default__" in used_tables:
                raise ValueError(f"目标表名“{tgt_table}”与默认表冲突，请指定不同的目标表名")
            if table_key in used_tables:
                label = tgt_table or "默认表"
                raise ValueError(f"同一采样点内目标表名重复（{label}），请为每条采集项指定唯一目标表名")
            used_tables.add(table_key)

            raw_id = str(raw.get("item_id") or "").strip()
            if raw_id and _IDENT.match(raw_id) and raw_id not in used_ids:
                item_id = raw_id
            else:
                while f"i{auto_seq}" in used_ids:
                    auto_seq += 1
                item_id = f"i{auto_seq}"
                auto_seq += 1
            used_ids.add(item_id)

            normalized.append(
                {
                    "item_id": item_id,
                    "source_database": src_db_rel,
                    "source_table": src_table,
                    "target_table": tgt_table,
                    "fields": fields,
                    "filter": filter_expr,
                }
            )
        return normalized

    def _items_from_row(
        self,
        row: sqlite3.Row | dict[str, Any],
        default_source_db: str | None = None,
    ) -> list[dict[str, Any]]:
        """从 samp_define 行读取多采集项配置；无 items_json 时自动回退为单条采集项。"""
        keys = row.keys() if isinstance(row, sqlite3.Row) else row
        raw_json = str(row["items_json"] or "").strip() if "items_json" in keys else ""
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, list) and parsed:
                    items_out: list[dict[str, Any]] = []
                    for idx, item in enumerate(parsed):
                        if not isinstance(item, dict):
                            continue
                        raw_src = item.get("source_database") or default_source_db or self.default_source_db
                        fields = item.get("fields") or []
                        if isinstance(fields, str):
                            fields = [f for f in fields.split(",") if f]
                        items_out.append(
                            {
                                "item_id": str(item.get("item_id") or f"i{idx + 1}"),
                                "source_database": self.to_relative_path(raw_src),
                                "source_table": str(item.get("source_table") or ""),
                                "target_table": str(item.get("target_table") or ""),
                                "fields": [str(f) for f in fields if str(f)],
                                "filter": str(item.get("filter") or ""),
                            }
                        )
                    if items_out:
                        return items_out
            except (ValueError, TypeError):
                pass
        raw_src = (row["source_database"] if "source_database" in keys else "") or default_source_db or self.default_source_db
        field_str = str(row["field_name"] or "") if "field_name" in keys else ""
        return [
            {
                "item_id": "i1",
                "source_database": self.to_relative_path(raw_src),
                "source_table": str(row["source_table"] or "") if "source_table" in keys else "",
                "target_table": "",
                "fields": [f for f in field_str.split(",") if f],
                "filter": str(row["filter_value"] or "") if "filter_value" in keys else "",
            }
        ]

    def _select_item(self, raw: sqlite3.Row, item_id: str | None = None) -> dict[str, Any]:
        """按 item_id（或 target_table）选定采样点内的一条采集项，默认返回首条。"""
        items = self._items_from_row(raw)
        if not item_id:
            return items[0]
        target = str(item_id).strip()
        for item in items:
            if item["item_id"] == target:
                return item
        for item in items:
            if item["target_table"] and item["target_table"].lower() == target.lower():
                return item
        raise ValueError(f"采集项不存在: {item_id}")

    def _iter_targets_for_range(
        self,
        raw: sqlite3.Row,
        start: int,
        end: int,
        order: str = "asc",
        item_id: str | None = None,
    ) -> list[tuple[Path, str]]:
        spec = self._spec_from_row(raw)
        item = self._select_item(raw, item_id)
        custom_table = item["target_table"]
        if not spec["yearly"]:
            stem = spec["single_stem"]
            return [(spec["dir_path"] / f"{stem}.db", custom_table or stem)]
        targets: list[tuple[Path, str]] = []
        for year in self._year_sequence(start, end, order):
            stem = spec["template"].replace("{year}", str(year))
            targets.append((spec["dir_path"] / f"{stem}.db", custom_table or stem))
        return targets

    def _list_spec_result_files(self, spec: dict[str, Any]) -> list[tuple[int | None, Path]]:
        dir_path: Path = spec["dir_path"]
        if not dir_path.exists() or not dir_path.is_dir():
            return []
        if not spec["yearly"]:
            single = dir_path / f"{spec['single_stem']}.db"
            return [(None, single)] if single.is_file() else []
        found: list[tuple[int | None, Path]] = []
        regex: re.Pattern[str] = spec["pattern_re"]
        for path in sorted(dir_path.glob("*.db")):
            if not path.is_file():
                continue
            match = regex.fullmatch(path.name)
            if match:
                found.append((int(match.group(1)), path))
        return found

    def _public(self, row: sqlite3.Row, default_source_db: str | None = None) -> dict[str, Any]:
        data = dict(row)
        items = self._items_from_row(row, default_source_db)
        primary = items[0]
        data.pop("field_name", None)
        data.pop("filter_value", None)
        data.pop("items_json", None)
        data["interval_seconds"] = data.pop("samp_time", 60)
        data["source_database"] = primary["source_database"]
        data["source_table"] = primary["source_table"]
        data["fields"] = list(primary["fields"])
        data["filter"] = primary["filter"]
        data["items"] = items
        data["item_count"] = len(items)
        target_dir = str(data.get("target_dir") or "")
        target_name = str(data.get("target_name") or "")
        data["target_dir"] = self.to_relative_path(target_dir) if target_dir else ""
        data["target_name"] = target_name
        try:
            spec = self._parse_target_spec(str(data.get("samp_name") or ""), target_dir, target_name)
            data["target_preview"] = spec["preview_rel"]
        except ValueError:
            data["target_preview"] = ""
        for key in ("samp_type", "trigger_time", "created_at", "updated_at"):
            data.pop(key, None)
        return data

    @staticmethod
    def _clone_definition(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **item,
            "fields": list(item["fields"]),
            "items": [{**sub, "fields": list(sub["fields"])} for sub in item.get("items", [])],
        }

    def list_definitions(self) -> list[dict[str, Any]]:
        """获取全部采样定义（带文件 mtime 与版本号缓存）。"""
        with self._def_cache_lock:
            stamp = self._definition_file_stamp()
            if self._def_cache_rows is not None and self._def_cache_key == stamp:
                return [self._clone_definition(item) for item in self._def_cache_rows]
        with self._connect(self.definition_db) as conn:
            rows = [
                self._public(row, self.default_source_db)
                for row in conn.execute("SELECT * FROM samp_define ORDER BY samp_name").fetchall()
            ]
        with self._def_cache_lock:
            self._def_cache_rows = rows
            self._def_cache_key = self._definition_file_stamp()
        return [self._clone_definition(item) for item in rows]

    def get_definition(self, samp_id: int) -> dict[str, Any] | None:
        with self._connect(self.definition_db) as conn:
            row = conn.execute("SELECT * FROM samp_define WHERE id=?", (samp_id,)).fetchone()
        return self._public(row, self.default_source_db) if row else None

    def _migrate_result_databases(self, old_spec: dict[str, Any], new_spec: dict[str, Any]) -> list[str]:
        """将旧目标规格下的全部结果库迁移至新目录/新库名并同步更新 RTDB 结构登记。"""
        if old_spec["conflict_key"] == new_spec["conflict_key"]:
            return []
        existing_files = self._list_spec_result_files(old_spec)
        if not existing_files:
            return []
        if old_spec["yearly"] != new_spec["yearly"]:
            raise ValueError("不支持在按年分库模式与单库模式之间自动迁移历史库，请取消勾选同步迁移")

        pairs: list[tuple[Path, Path, str, str]] = []
        for year, src_path in existing_files:
            old_table = src_path.stem
            if new_spec["yearly"]:
                new_table = new_spec["template"].replace("{year}", str(year))
            else:
                new_table = new_spec["single_stem"]
            if not _IDENT.match(new_table):
                raise ValueError(f"新目标生成的结果表名不合法: {new_table}")
            dst_path = new_spec["dir_path"] / f"{new_table}.db"
            if dst_path.exists() and dst_path.resolve() != src_path.resolve():
                raise ValueError(f"目标结果库已存在，无法迁移: {self.to_relative_path(dst_path)}")
            pairs.append((src_path, dst_path, old_table, new_table))

        migrated: list[str] = []
        new_spec["dir_path"].mkdir(parents=True, exist_ok=True)
        for src_path, dst_path, old_table, new_table in pairs:
            src_key = os.path.normcase(str(src_path.resolve()))
            dst_key = os.path.normcase(str(dst_path.resolve()))
            with self._db_lock(src_path), self._db_lock(dst_path):
                if old_table != new_table:
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
                if src_path.resolve() != dst_path.resolve():
                    # 迁移前刷盘 WAL 确保所有事务完整合入主库文件
                    try:
                        with self._connect(src_path) as ck_conn:
                            ck_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            ck_conn.execute("PRAGMA journal_mode=DELETE")
                    except sqlite3.Error:
                        pass
                    src_path.rename(dst_path)
                    for suffix in ("-wal", "-shm", "-journal"):
                        extra = Path(f"{src_path}{suffix}")
                        if extra.exists():
                            extra.unlink(missing_ok=True)
            self._drop_schema_cache_for_db(src_key)
            self._drop_schema_cache_for_db(dst_key)
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
        raw_target_dir = str(payload.get("target_dir") or "").strip()
        raw_target_name = str(payload.get("target_name") or "").strip()
        interval = int(payload.get("interval_seconds", payload.get("samp_time", 60)))
        do_rename = bool(rename_results or payload.get("rename_results", False))
        if not name:
            raise ValueError("name 不能为空")
        if not _IDENT.match(name):
            raise ValueError("采样点名称只能包含字母、数字、下划线或中文")
        if interval < 0:
            raise ValueError("interval_seconds 不能为负数")

        new_spec = self._parse_target_spec(name, raw_target_dir, raw_target_name)
        target_dir_stored = self.to_relative_path(new_spec["dir_path"]) if raw_target_dir else ""
        # 校验目标库不与其他采样定义冲突
        for other in self.list_definitions():
            if samp_id is not None and int(other["id"]) == int(samp_id):
                continue
            other_spec = self._parse_target_spec(
                other["samp_name"], other.get("target_dir", ""), other.get("target_name", "")
            )
            if other_spec["conflict_key"] == new_spec["conflict_key"]:
                raise ValueError(f"目标库与采样点“{other['samp_name']}”冲突: {new_spec['preview_rel']}")

        raw_items = payload.get("items")
        fallback_item = {
            "item_id": "i1",
            "source_database": payload.get("source_database") or self.default_source_db,
            "source_table": payload.get("source_table") or payload.get("table") or "",
            "target_table": str(payload.get("target_table") or ""),
            "fields": payload.get("fields", payload.get("field_name", [])),
            "filter": payload.get("filter", payload.get("filter_value", "")),
        }
        items = self._normalize_items(raw_items, fallback_item, single_stem=new_spec["single_stem"])
        primary = items[0]
        items_json = json.dumps(items, ensure_ascii=False)

        now = int(time.time())
        values = (
            name,
            primary["source_database"],
            primary["source_table"],
            target_dir_stored,
            raw_target_name,
            items_json,
            ",".join(primary["fields"]),
            1,
            interval,
            primary["filter"],
            int(bool(payload.get("enabled", True))),
            0,
            now,
            now,
        )
        migrated_files: list[str] = []
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            if samp_id is None:
                samp_id = int(
                    conn.execute(
                        "INSERT INTO samp_define(samp_name,source_database,source_table,target_dir,target_name,items_json,field_name,samp_type,samp_time,filter_value,enabled,trigger_time,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    ).lastrowid
                )
            else:
                prev = conn.execute("SELECT * FROM samp_define WHERE id=?", (samp_id,)).fetchone()
                if prev is not None and do_rename:
                    old_spec = self._spec_from_row(prev)
                    if old_spec["conflict_key"] != new_spec["conflict_key"]:
                        migrated_files = self._migrate_result_databases(old_spec, new_spec)
                conn.execute(
                    "UPDATE samp_define SET samp_name=?,source_database=?,source_table=?,target_dir=?,target_name=?,items_json=?,field_name=?,samp_type=?,samp_time=?,filter_value=?,enabled=?,trigger_time=?,updated_at=? WHERE id=?",
                    (*values[:12], values[13], samp_id),
                )
        self._invalidate_definition_cache()
        result = self.get_definition(samp_id)  # type: ignore[return-value]
        if migrated_files:
            result["migrated_results"] = migrated_files
        return result

    def add_definition_item(self, samp_id: int, item_payload: dict[str, Any]) -> dict[str, Any]:
        """向已定义的采样点追加一条采集项（同库独立表）。"""
        current = self.get_definition(samp_id)
        if not current:
            raise KeyError("采样定义不存在")
        items = [{**it, "fields": list(it["fields"])} for it in current["items"]]
        new_item = dict(item_payload)
        if not str(new_item.get("target_table") or "").strip():
            new_item["target_table"] = str(new_item.get("source_table") or "").strip()
        items.append(new_item)
        return self.upsert_definition(
            {
                "name": current["samp_name"],
                "target_dir": current.get("target_dir", ""),
                "target_name": current.get("target_name", ""),
                "interval_seconds": current["interval_seconds"],
                "enabled": bool(current["enabled"]),
                "items": items,
            },
            samp_id=samp_id,
        )

    def update_definition_item(self, samp_id: int, item_id: str, item_payload: dict[str, Any]) -> dict[str, Any]:
        """修改指定采样点内某一条采集项（源库/源表/目标表/字段/过滤）。"""
        current = self.get_definition(samp_id)
        if not current:
            raise KeyError("采样定义不存在")
        items = [{**it, "fields": list(it["fields"])} for it in current["items"]]
        idx = next((i for i, it in enumerate(items) if it["item_id"] == item_id), -1)
        if idx < 0:
            raise KeyError(f"采集项不存在: {item_id}")
        merged = {**items[idx], **item_payload}
        merged["item_id"] = str(item_payload.get("item_id") or item_id)
        items[idx] = merged
        return self.upsert_definition(
            {
                "name": current["samp_name"],
                "target_dir": current.get("target_dir", ""),
                "target_name": current.get("target_name", ""),
                "interval_seconds": current["interval_seconds"],
                "enabled": bool(current["enabled"]),
                "items": items,
            },
            samp_id=samp_id,
        )

    def delete_definition_item(self, samp_id: int, item_id: str) -> dict[str, Any]:
        """从指定采样点移除一条采集项（至少保留一条）。"""
        current = self.get_definition(samp_id)
        if not current:
            raise KeyError("采样定义不存在")
        items = [{**it, "fields": list(it["fields"])} for it in current["items"]]
        idx = next((i for i, it in enumerate(items) if it["item_id"] == item_id), -1)
        if idx < 0:
            raise KeyError(f"采集项不存在: {item_id}")
        if len(items) <= 1:
            raise ValueError("采样点至少保留一条采集项，如需移除请删除整个采样点")
        items.pop(idx)
        return self.upsert_definition(
            {
                "name": current["samp_name"],
                "target_dir": current.get("target_dir", ""),
                "target_name": current.get("target_name", ""),
                "interval_seconds": current["interval_seconds"],
                "enabled": bool(current["enabled"]),
                "items": items,
            },
            samp_id=samp_id,
        )

    def export_definitions(self) -> dict[str, Any]:
        """导出全部采样定义为可迁移的 JSON 结构。"""
        items = [
            {
                "name": d["samp_name"],
                "source_database": d["source_database"],
                "source_table": d["source_table"],
                "target_dir": d.get("target_dir", ""),
                "target_name": d.get("target_name", ""),
                "fields": d["fields"],
                "items": d.get("items", []),
                "interval_seconds": d["interval_seconds"],
                "filter": d["filter"],
                "enabled": bool(d["enabled"]),
            }
            for d in self.list_definitions()
        ]
        return {"version": 2, "exported_at": int(time.time()), "count": len(items), "items": items}

    def list_directories(self, max_depth: int = 6, refresh: bool = False) -> list[str]:
        """扫描 root_dir 下所有多级目录（相对路径，含根目录 '.'），带 10s 内存 TTL 缓存。"""
        now = time.monotonic()
        if not refresh:
            with self._scan_cache_lock:
                if self._dir_cache is not None and (now - self._dir_cache[0]) < self._scan_ttl_seconds:
                    return list(self._dir_cache[1])
        dirs: list[str] = ["."]
        seen = {"."}

        def walk(current: Path, depth: int) -> None:
            if depth >= max_depth:
                return
            try:
                children = sorted(
                    (p for p in current.iterdir() if p.is_dir()),
                    key=lambda p: p.name.lower(),
                )
            except OSError:
                return
            for entry in children:
                name = entry.name
                if name.startswith(".") or name in _SKIP_SCAN_DIRS:
                    continue
                rel = self.to_relative_path(entry)
                if rel not in seen:
                    seen.add(rel)
                    dirs.append(rel)
                walk(entry, depth + 1)

        if self.root_dir.exists() and self.root_dir.is_dir():
            walk(self.root_dir, 0)
        with self._scan_cache_lock:
            self._dir_cache = (time.monotonic(), list(dirs))
        return dirs

    def list_target_dirs(self, refresh: bool = False) -> list[str]:
        """返回默认结果目录、root_dir 全部多级子目录与定义中已用过的目标目录（相对路径）。"""
        default_rel = self.to_relative_path(self.result_dir)
        dirs: list[str] = [default_rel]
        seen = {default_rel}
        for rel in self.list_directories(refresh=refresh):
            if rel not in seen:
                seen.add(rel)
                dirs.append(rel)
        for item in self.list_definitions():
            td = item.get("target_dir") or ""
            if td and td not in seen:
                seen.add(td)
                dirs.append(td)
        return dirs

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

    def delete_definitions_batch(self, samp_ids: Iterable[int]) -> int:
        """批量删除采样定义（保留已有结果库文件）。"""
        ids = list(dict.fromkeys(int(item) for item in samp_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._db_lock(self.definition_db), self._connect(self.definition_db) as conn:
            deleted = conn.execute(f"DELETE FROM samp_define WHERE id IN ({placeholders})", ids).rowcount
        if deleted:
            self._invalidate_definition_cache()
        return int(deleted)

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
        resolved = self.resolve_path(source_database)
        allowed = self._allowed_source_dirs()
        if not allowed:
            return str(resolved)
        for base in allowed:
            try:
                resolved.relative_to(base)
                return str(resolved)
            except ValueError:
                continue
        raise ValueError("源数据库路径不在 SAMP_ALLOWED_SOURCE_DIRS 允许范围内")

    def _source_database(self, source_database: str | None) -> str:
        target = str(source_database or self.default_source_db)
        return self._validate_source_path(target)

    def _iter_db_files(self, base_dir: Path, max_depth: int = 6) -> Iterator[Path]:
        """在指定目录下按有限深度递归扫描 SQLite 源库文件（.db/.sqlite3/.sqlite），跳过缓存与版本库目录。"""
        if not base_dir.exists() or not base_dir.is_dir():
            return
        stack: list[tuple[Path, int]] = [(base_dir, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for entry in entries:
                if entry.is_file() and not entry.name.startswith(".") and entry.suffix.lower() in _SOURCE_DB_SUFFIXES:
                    yield entry
                elif entry.is_dir() and depth < max_depth:
                    name = entry.name
                    if name.startswith(".") or name in _SKIP_SCAN_DIRS:
                        continue
                    stack.append((entry, depth + 1))

    def list_source_databases(self, refresh: bool = False) -> list[str]:
        """扫描 root_dir 及允许目录下的可用源 SQLite 文件，统一返回相对路径，带 10s TTL 缓存。"""
        now = time.monotonic()
        if not refresh:
            with self._scan_cache_lock:
                if self._source_db_cache is not None and (now - self._source_db_cache[0]) < self._scan_ttl_seconds:
                    return list(self._source_db_cache[1])
        candidates: list[str] = []
        seen: set[str] = set()
        def_path = Path(self.definition_db).expanduser().resolve()
        excluded_norms = {
            os.path.normcase(str(def_path)),
            os.path.normcase(str(def_path.with_name("samp_runtime.db"))),
        }
        # 排除全部已声明的自定义目标结果库，防止误把结果库选作源库
        for item in self.list_definitions():
            try:
                spec = self._parse_target_spec(item["samp_name"], item.get("target_dir", ""), item.get("target_name", ""))
                for _, res_file in self._list_spec_result_files(spec):
                    excluded_norms.add(os.path.normcase(str(res_file.resolve())))
            except ValueError:
                pass

        def add_path(path: str | Path) -> None:
            resolved = self.resolve_path(path)
            norm = os.path.normcase(str(resolved))
            if norm in seen or norm in excluded_norms:
                return
            if _RESULT_FILE.fullmatch(resolved.name):
                return
            seen.add(norm)
            candidates.append(self.to_relative_path(resolved))

        # 优先收集磁盘上真实存在的 .db 文件（深度最深 6 层），再保底纳入默认源库与已引用源库
        scan_roots = [self.root_dir, self.result_dir, Path(self.default_source_db).parent, *self._allowed_source_dirs()]
        scanned_roots: set[str] = set()
        for directory in scan_roots:
            dir_norm = os.path.normcase(str(directory.expanduser().resolve()))
            if dir_norm in scanned_roots:
                continue
            scanned_roots.add(dir_norm)
            for db_file in self._iter_db_files(directory, max_depth=6):
                add_path(db_file)

        # 按相对路径稳定排序，再追加不存在的默认源库兜底（确保真实存在的源库永远排前面）
        candidates.sort(key=lambda s: s.lower())
        add_path(self.default_source_db)

        for definition in self.list_definitions():
            for sub in definition.get("items") or [{"source_database": definition.get("source_database")}]:
                src = sub.get("source_database")
                if src:
                    try:
                        self._validate_source_path(src)
                        add_path(src)
                    except ValueError:
                        pass
        with self._scan_cache_lock:
            self._source_db_cache = (time.monotonic(), list(candidates))
        return candidates

    def source_tables(self, source_database: str | None = None) -> list[str]:
        with self._connect_readonly(self._source_database(source_database)) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [row[0] for row in rows if row[0].lower() not in REGISTRY_TABLES]

    @staticmethod
    def _rtdb_labels(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """读取源库内置 RTDB 结构表中的中文表名与中文列名（存在时返回，不报错）。"""
        table_labels: dict[str, str] = {}
        col_labels: dict[str, dict[str, str]] = {}
        try:
            has_tables = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'"
            ).fetchone()
            if has_tables:
                for row in conn.execute("SELECT table_name, chn_name FROM rtdb_schema_tables").fetchall():
                    t_name, c_name = str(row[0]), str(row[1] or "")
                    if c_name and c_name != t_name:
                        table_labels[t_name] = c_name
            has_cols = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_columns'"
            ).fetchone()
            if has_cols:
                for row in conn.execute("SELECT table_name, eng_name, chn_name FROM rtdb_schema_columns").fetchall():
                    t_name, eng, chn = str(row[0]), str(row[1]), str(row[2] or "")
                    if chn and chn != eng:
                        col_labels.setdefault(t_name, {})[eng] = chn
        except sqlite3.Error:
            pass
        return table_labels, col_labels

    def source_tables_summary(self, source_database: str | None = None) -> dict[str, Any]:
        """一次返回源库全部业务表的行数、字段类型、数值字段、RTDB 中文名及已有采样点引用。"""
        database = self._source_database(source_database)
        db_norm = os.path.normcase(str(Path(database).expanduser().resolve()))
        existing_by_table: dict[str, list[str]] = {}
        for item in self.list_definitions():
            for sub in item.get("items") or [item]:
                src = sub.get("source_database") or self.default_source_db
                src_tbl = str(sub.get("source_table") or "")
                try:
                    if src_tbl and os.path.normcase(str(self.resolve_path(src))) == db_norm:
                        lst = existing_by_table.setdefault(src_tbl, [])
                        if str(item["samp_name"]) not in lst:
                            lst.append(str(item["samp_name"]))
                except OSError:
                    pass

        tables_out: list[dict[str, Any]] = []
        with self._connect_readonly(database) as conn:
            table_labels, col_labels = self._rtdb_labels(conn)
            raw_tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                if str(row[0]).lower() not in REGISTRY_TABLES
            ]
            for tbl in raw_tables:
                if not _IDENT.match(tbl):
                    continue
                quoted = self._quote(tbl)
                info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                all_cols = [str(row[1]) for row in info]
                col_types = {str(row[1]): _normalize_sql_type(str(row[2])) for row in info}
                biz_fields = [c for c in all_cols if c.lower() not in _RESULT_KEYS and _IDENT.match(c)]
                numeric_fields = [c for c in biz_fields if col_types.get(c) in ("INTEGER", "REAL")]
                try:
                    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
                except sqlite3.Error:
                    row_count = 0
                tables_out.append(
                    {
                        "table": tbl,
                        "chn_name": table_labels.get(tbl, ""),
                        "row_count": row_count,
                        "fields": biz_fields,
                        "numeric_fields": numeric_fields,
                        "field_types": {c: col_types[c] for c in biz_fields},
                        "field_labels": col_labels.get(tbl, {}),
                        "existing_definitions": existing_by_table.get(tbl, []),
                    }
                )
        return {"source_database": self.to_relative_path(database), "tables": tables_out}

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
        """读取源表列信息、RTDB 中文名、列去重样本值、总行数与过滤命中预览。"""
        database = self._source_database(source_database)
        table = self._source_table(source_table)
        with self._connect_readonly(database) as conn:
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {database}:{source_table}")
            col_types = {str(row[1]): _normalize_sql_type(str(row[2])) for row in info}
            _, col_labels_all = self._rtdb_labels(conn)
            field_labels = col_labels_all.get(source_table, {})
            numeric_fields = [
                c for c in columns if c.lower() not in _RESULT_KEYS and col_types.get(c) in ("INTEGER", "REAL")
            ]
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
            # 收集每列前 20 个非空去重样本值供可视化过滤下拉候选（限制最多前 40 列防超宽表耗时）
            sample_values: dict[str, list[Any]] = {}
            for col in columns[:40]:
                if not _IDENT.match(col):
                    continue
                try:
                    q_col = self._quote(col)
                    vals = [
                        r[0]
                        for r in conn.execute(
                            f"SELECT DISTINCT {q_col} FROM {table} WHERE {q_col} IS NOT NULL LIMIT 20"
                        ).fetchall()
                    ]
                    sample_values[col] = vals
                except sqlite3.Error:
                    pass
        return {
            "source_database": self.to_relative_path(database),
            "source_table": source_table,
            "fields": columns,
            "field_types": col_types,
            "field_labels": field_labels,
            "numeric_fields": numeric_fields,
            "sample_values": sample_values,
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
    ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str], list[str]]:
        """单次只读连接完成字段解析（兼容 '*' 全表跟随）、过滤执行与源列声明类型采集。"""
        with self._connect_readonly(self._source_database(source_database)) as conn:
            table = self._source_table(source_table)
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                raise ValueError(f"源表不存在或没有字段: {source_database}:{source_table}")
            col_types = {str(row[1]): _normalize_sql_type(str(row[2])) for row in info}
            if fields == ["*"]:
                effective_fields: list[str] = []
                seen_low: set[str] = set()
                for col in columns:
                    low = col.lower()
                    if low in _RESULT_KEYS or not _IDENT.match(col) or low in seen_low:
                        continue
                    seen_low.add(low)
                    effective_fields.append(col)
                if not effective_fields:
                    raise ValueError(f"源表没有可采样的业务字段: {source_database}:{source_table}")
            else:
                effective_fields = list(fields)

            where_sql, params, filter_fields = self._build_filter_sql(expression, col_types)
            missing = [field for field in [*effective_fields, *filter_fields] if field not in col_types]
            if missing:
                raise ValueError(f"源表缺少字段: {', '.join(dict.fromkeys(missing))}")
            primary_keys = [str(row[1]) for row in sorted(info, key=lambda item: item[5]) if row[5]]
            key_columns = ["row_key"] if "row_key" in columns else primary_keys
            selected = list(dict.fromkeys(effective_fields))
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
            values = {field: row[field] for field in effective_fields}
            key = "|".join(str(row[column]) for column in key_cols)
            result.append((key, values))
        field_types = {field: col_types[field] for field in effective_fields}
        return result, field_types, effective_fields

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

    def _resolve_source_fields(
        self,
        source_database: str,
        source_table: str,
        configured_fields: list[str],
    ) -> list[str]:
        """解析采集项本次实际采样字段：若为 ['*'] 则动态读取源表全部合法业务列。"""
        if configured_fields != ["*"]:
            return list(configured_fields)
        raw_cols = self.source_fields(source_database, source_table)
        resolved: list[str] = []
        seen_lower: set[str] = set()
        for col in raw_cols:
            low = col.lower()
            if low in _RESULT_KEYS or not _IDENT.match(col) or low in seen_lower:
                continue
            seen_lower.add(low)
            resolved.append(col)
        if not resolved:
            raise ValueError(f"源表没有可采样的业务字段: {source_database}:{source_table}")
        return resolved

    def _discover_item_fields(
        self,
        raw: sqlite3.Row,
        item: dict[str, Any],
        start: int | None = None,
        end: int | None = None,
    ) -> list[str]:
        """解析查询/展示可用的真实字段清单：对 ['*'] 合并已落盘结果表列与当前源表业务列。"""
        cols, _ = self._discover_item_schema(raw, item, start, end)
        return cols

    def _discover_item_schema(
        self,
        raw: sqlite3.Row,
        item: dict[str, Any],
        start: int | None = None,
        end: int | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """解析采集项字段列表与列类型（INTEGER/REAL/TEXT），兼容 '*' 通配符与普通字段定义。"""
        is_wild = item["fields"] == ["*"]
        cols: list[str] = []
        col_types: dict[str, str] = {}
        seen_lower: set[str] = set()

        def push(col: str, sql_type: str = "") -> None:
            low = col.lower()
            if low in _RESULT_KEYS or not _IDENT.match(col):
                return
            if low not in seen_lower:
                seen_lower.add(low)
                cols.append(col)
                if sql_type:
                    col_types[col] = sql_type
            elif sql_type and col in col_types and not col_types[col]:
                col_types[col] = sql_type

        if not is_wild:
            for f in item["fields"]:
                push(f)

        # 收集结果表列信息
        targets: list[tuple[Path, str]] = []
        if start is not None and end is not None:
            targets = self._iter_targets_for_range(raw, start, end, "asc", item["item_id"])
        else:
            spec = self._spec_from_row(raw)
            custom_table = item["target_table"]
            for _, db_path in self._list_spec_result_files(spec):
                targets.append((db_path, custom_table or db_path.stem))

        for db_path, table in targets:
            if not db_path.exists():
                continue
            try:
                with self._connect_readonly(db_path) as conn:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    for r in conn.execute(f"PRAGMA table_info({self._quote(table)})").fetchall():
                        name = str(r[1])
                        norm_type = _normalize_sql_type(str(r[2]))
                        if is_wild:
                            push(name, norm_type)
                        elif name in cols and name not in col_types:
                            col_types[name] = norm_type
            except (sqlite3.Error, ValueError):
                pass

        # 合并当前源表列信息（未采样前或源表新增列时立即可见，并补齐列类型）
        try:
            src_db = self._source_database(item["source_database"] or self.default_source_db)
            with self._connect_readonly(src_db) as conn:
                info = conn.execute(f"PRAGMA table_info({self._source_table(item['source_table'])})").fetchall()
                for r in info:
                    name = str(r[1])
                    norm_type = _normalize_sql_type(str(r[2]))
                    if is_wild:
                        push(name, norm_type)
                    elif name in cols and name not in col_types:
                        col_types[name] = norm_type
        except (sqlite3.Error, ValueError, OSError):
            pass

        return cols, col_types

    def resolve_definition_fields(self, samp_id: int, item_id: str | None = None) -> dict[str, Any]:
        """返回采样点指定采集项解析后的真实可查字段列表与列类型（供查询页展开 '*' 与一键仅选数值列）。"""
        raw = self._raw_definition(samp_id)
        item = self._select_item(raw, item_id)
        cols, col_types = self._discover_item_schema(raw, item)
        numeric_fields = [c for c in cols if col_types.get(c) in ("INTEGER", "REAL")]
        return {
            "samp_id": samp_id,
            "item_id": item["item_id"],
            "is_wildcard": item["fields"] == ["*"],
            "fields": cols,
            "field_types": col_types,
            "numeric_fields": numeric_fields,
        }

    def query_sample_times(
        self,
        samp_id: int,
        start: int,
        end: int,
        limit: int = 200,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """获取指定时间范围内已落盘的去重采样时刻（降序），供查询页按时刻查看整表快照。"""
        safe_limit = max(1, min(int(limit), 2000))
        raw = self._raw_definition(samp_id)
        selected_item = self._select_item(raw, item_id)
        found: set[int] = set()
        for db_path, table in self._iter_targets_for_range(raw, start, end, "desc", selected_item["item_id"]):
            if not db_path.exists():
                continue
            with self._connect_readonly(db_path) as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                for row in conn.execute(
                    f"SELECT DISTINCT sample_time FROM {self._quote(table)} WHERE sample_time BETWEEN ? AND ? ORDER BY sample_time DESC LIMIT ?",
                    (start, end, safe_limit),
                ).fetchall():
                    found.add(int(row[0]))
                    if len(found) >= safe_limit:
                        break
            if len(found) >= safe_limit:
                break
        times = sorted(found, reverse=True)[:safe_limit]
        return {"item_id": selected_item["item_id"], "times": times, "total": len(times)}

    def run_sample(self, samp_id: int, sampled_at: int | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        sampled_at = int(time.time() if sampled_at is None else sampled_at)
        definition = self._raw_definition(samp_id)
        items = self._items_from_row(definition)
        spec = self._spec_from_row(definition)
        db_path, default_table = self._target_for_timestamp(spec, sampled_at)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_key = os.path.normcase(str(db_path.resolve()))

        # 先全部只读采样源数据（单次连接完成 '*' 列发现与读取），减少目标结果库写锁持有时长
        prepared: list[tuple[dict[str, Any], list[str], list[tuple[str, dict[str, Any]]], dict[str, str]]] = []
        for item in items:
            src_db = item["source_database"] or self.default_source_db
            rows, field_types, fields = self._read_source(
                src_db,
                item["source_table"],
                item["filter"],
                list(item["fields"]),
            )
            prepared.append((item, fields, rows, field_types))

        total_written = 0
        tables_written: list[str] = []
        item_results: list[dict[str, Any]] = []

        with self._db_lock(db_path), self._connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item, fields, rows, field_types in prepared:
                table = item["target_table"] or default_table
                table_sql = self._quote(table)
                quoted_fields = [self._quote(field) for field in fields]
                schema_key = (db_key, table.lower())
                idx_name = self._quote(f"idx_{table}_rk_t")

                need_schema_sync = True
                with self._schema_cache_lock:
                    cached = self._synced_result_schemas.get(schema_key)
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
                        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_sql} (row_key, sample_time)")
                    else:
                        existing = {row[1].lower() for row in info}
                        for field, quoted in zip(fields, quoted_fields):
                            if field.lower() not in existing:
                                conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {quoted} {field_types[field]}")
                        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_sql} (row_key, sample_time)")
                    latest_info = conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
                    sync_table(conn, db_path, table, latest_info)
                    with self._schema_cache_lock:
                        self._synced_result_schemas[schema_key] = {
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
                        self._synced_result_schemas.pop(schema_key, None)
                    raise
                total_written += len(rows)
                tables_written.append(table)
                item_results.append(
                    {
                        "item_id": item["item_id"],
                        "source_table": item["source_table"],
                        "table": table,
                        "fields": fields,
                        "rows_written": len(rows),
                    }
                )

        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "samp_id": samp_id,
            "samp_name": definition["samp_name"],
            "sampled_at": sampled_at,
            "rows_written": total_written,
            "duration_ms": duration_ms,
            "database": str(db_path),
            "relative_path": self.to_relative_path(db_path),
            "table": tables_written[0] if tables_written else default_table,
            "tables": tables_written,
            "item_results": item_results,
        }

    def _resolve_query_fields(self, allowed_fields: list[str], fields: list[str] | None) -> list[str]:
        if fields is None:
            return list(allowed_fields)
        invalid = [field for field in fields if field not in allowed_fields or not _IDENT.match(field)]
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
        item_id: str | None = None,
    ) -> dict[str, Any]:
        order_norm = order.lower()
        if order_norm not in ("asc", "desc"):
            raise ValueError("order 仅支持 asc 或 desc")
        limit = max(1, min(int(limit), 10000))
        offset = max(0, int(offset))
        requested_offset = offset
        raw = self._raw_definition(samp_id)
        definition = self._public(raw, self.default_source_db)
        selected_item = self._select_item(raw, item_id)
        allowed_fields = self._discover_item_fields(raw, selected_item, start, end)
        selected = self._resolve_query_fields(allowed_fields, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]

        rows: list[dict[str, Any]] = []
        tables: list[str] = []
        total_rows = 0
        dir_sql = "DESC" if order_norm == "desc" else "ASC"

        for db_path, table in self._iter_targets_for_range(raw, start, end, order_norm, selected_item["item_id"]):
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
                # 快速路径：当 offset==0 且本表未触发分页截断时，单次查询即可得到精确行数，免一次 COUNT(*)
                if offset == 0 and len(rows) < limit:
                    existing = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({self._quote(table)})")}
                    field_sql = ", ".join(
                        f"{self._quote(field) if field.lower() in existing else 'NULL'} AS {self._quote(field)}"
                        for field in selected
                    )
                    page_size = limit - len(rows)
                    fetched = conn.execute(
                        f"SELECT sample_time,row_key,{field_sql} FROM {self._quote(table)} "
                        f"WHERE {where} ORDER BY sample_time {dir_sql}, row_key {dir_sql} LIMIT ?",
                        [*params, page_size + 1],
                    ).fetchall()
                    if len(fetched) <= page_size:
                        rows.extend(dict(row) for row in fetched)
                        total_rows += len(fetched)
                        continue
                    rows.extend(dict(row) for row in fetched[:page_size])
                    total_rows += int(conn.execute(f"SELECT COUNT(*) FROM {self._quote(table)} WHERE {where}", params).fetchone()[0])
                    continue

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
            "item_id": selected_item["item_id"],
            "item": selected_item,
            "columns": ["sample_time", "row_key", *selected],
            "rows": rows,
            "tables": tables,
            "total_rows": total_rows,
            "limit": limit,
            "offset": requested_offset,
            "order": order_norm,
            "row_keys": clean_keys,
        }

    def query_keys(
        self,
        samp_id: int,
        start: int,
        end: int,
        limit: int = 500,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """获取指定时间范围内出现过的去重 row_key 列表。"""
        safe_limit = max(1, min(int(limit), 5000))
        raw = self._raw_definition(samp_id)
        selected_item = self._select_item(raw, item_id)
        found: set[str] = set()
        for db_path, table in self._iter_targets_for_range(raw, start, end, "asc", selected_item["item_id"]):
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
        return {"item_id": selected_item["item_id"], "keys": keys, "total": len(keys)}

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
        item_id: str | None = None,
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
        selected_item = self._select_item(raw, item_id)
        allowed_fields = self._discover_item_fields(raw, selected_item, start, end)
        selected = self._resolve_query_fields(allowed_fields, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]

        # 统计命中点数，自动决定是否需要分桶降采样
        tables: list[str] = []
        total_raw_rows = 0
        key_set: set[str] = set()
        target_list = self._iter_targets_for_range(raw, start, end, "asc", selected_item["item_id"])
        for db_path, table in target_list:
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

        for db_path, table in target_list:
            if effective_bucket == 0 and raw_loaded >= max_raw:
                truncated = True
                break
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
            "item_id": selected_item["item_id"],
            "item": selected_item,
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
        item_id: str | None = None,
    ) -> tuple[dict[str, Any], list[str], Iterator[dict[str, Any]]]:
        """流式遍历满足查询条件的全部行，供全量 CSV 导出使用。"""
        order_norm = order.lower()
        if order_norm not in ("asc", "desc"):
            raise ValueError("order 仅支持 asc 或 desc")
        raw = self._raw_definition(samp_id)
        definition = self._public(raw, self.default_source_db)
        selected_item = self._select_item(raw, item_id)
        allowed_fields = self._discover_item_fields(raw, selected_item, start, end)
        selected = self._resolve_query_fields(allowed_fields, fields)
        clean_keys = [str(k) for k in (row_keys or []) if str(k) != ""]
        columns = ["sample_time", "row_key", *selected]
        dir_sql = "DESC" if order_norm == "desc" else "ASC"

        def generator() -> Iterator[dict[str, Any]]:
            for db_path, table in self._iter_targets_for_range(raw, start, end, order_norm, selected_item["item_id"]):
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
        result_databases = len(self.list_result_databases())
        return {"definitions": definitions, "enabled": enabled, "result_databases": result_databases}

    def status(self) -> dict[str, Any]:
        summary = self.summary()
        return {
            **summary,
            "items": [self.runtime_status(item["id"]) for item in self.list_definitions()],
        }

    def _inspect_result_db_meta(self, path: Path) -> dict[str, Any]:
        """按文件 (mtime_ns, size) 缓存结果库全部业务表的统计元数据，避免轮询反复全表 COUNT(*)。"""
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cache_key = os.path.normcase(str(path.resolve()))
        with self._meta_cache_lock:
            cached = self._result_meta_cache.get(cache_key)
            if cached is not None and cached[0] == stamp:
                out = dict(cached[1])
                out["tables"] = list(out.get("tables", []))
                return out

        meta: dict[str, Any] = {
            "size_bytes": stat.st_size,
            "modified_at": int(stat.st_mtime),
            "row_count": 0,
            "min_sample_time": None,
            "max_sample_time": None,
            "rtdb_registered": False,
            "table_count": 0,
            "tables": [],
        }
        try:
            with self._connect_readonly(path) as conn:
                biz_tables = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                    if str(r[0]).lower() not in REGISTRY_TABLES and _IDENT.match(str(r[0]))
                ]
                total_rows = 0
                min_ts: int | None = None
                max_ts: int | None = None
                valid_tables: list[str] = []
                for table in biz_tables:
                    cols = {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({self._quote(table)})")}
                    if "sample_time" not in cols:
                        continue
                    valid_tables.append(table)
                    cnt, mn, mx = conn.execute(
                        f"SELECT COUNT(*), MIN(sample_time), MAX(sample_time) FROM {self._quote(table)}"
                    ).fetchone()
                    total_rows += int(cnt or 0)
                    if mn is not None:
                        val_mn = int(mn)
                        min_ts = val_mn if min_ts is None else min(min_ts, val_mn)
                    if mx is not None:
                        val_mx = int(mx)
                        max_ts = val_mx if max_ts is None else max(max_ts, val_mx)
                meta["row_count"] = total_rows
                meta["min_sample_time"] = min_ts
                meta["max_sample_time"] = max_ts
                meta["tables"] = valid_tables
                meta["table_count"] = len(valid_tables)
                meta["rtdb_registered"] = (
                    conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'").fetchone()
                    is not None
                )
        except sqlite3.Error:
            pass
        with self._meta_cache_lock:
            self._result_meta_cache[cache_key] = (stamp, {**meta, "tables": list(meta["tables"])})
        return meta

    def runtime_status(self, samp_id: int) -> dict[str, Any]:
        definition = self._raw_definition(samp_id)
        items = self._items_from_row(definition)
        spec = self._spec_from_row(definition)
        files = self._list_spec_result_files(spec)
        years = [year for year, _ in files if year is not None]
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
            "source_database": self.to_relative_path(items[0]["source_database"] or self.default_source_db),
            "source_table": items[0]["source_table"],
            "item_count": len(items),
            "target_preview": spec["preview_rel"],
            "single_file": not spec["yearly"],
            "result_years": years,
            "result_size_bytes": total_size,
            "total_rows": total_rows,
            "latest_sample_time": latest_sample_time,
            "last_result_at": latest_sample_time if latest_sample_time is not None else last_mtime,
        }

    def list_result_databases(self) -> list[dict[str, Any]]:
        """汇总默认目录与所有自定义目标目录下的结果库、元数据、RTDB 状态及孤儿标记。"""
        defs = self.list_definitions()
        defined_names = {item["samp_name"] for item in defs}
        items: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 1. 先纳入所有现存定义对应的目标结果库（支持自定义目录、模板及单库模式）
        for item in defs:
            try:
                spec = self._parse_target_spec(item["samp_name"], item.get("target_dir", ""), item.get("target_name", ""))
            except ValueError:
                continue
            for year, path in self._list_spec_result_files(spec):
                norm = os.path.normcase(str(path.resolve()))
                if norm in seen:
                    continue
                seen.add(norm)
                meta = self._inspect_result_db_meta(path)
                items.append(
                    {
                        "filename": path.name,
                        "path": self.to_relative_path(path),
                        "samp_name": item["samp_name"],
                        "year": year,
                        "size_bytes": meta["size_bytes"],
                        "modified_at": meta["modified_at"],
                        "row_count": meta["row_count"],
                        "table_count": meta["table_count"],
                        "tables": meta["tables"],
                        "min_sample_time": meta["min_sample_time"],
                        "max_sample_time": meta["max_sample_time"],
                        "rtdb_registered": meta["rtdb_registered"],
                        "is_orphan": False,
                    }
                )

        # 2. 再扫描默认结果目录及各自定义目录中的默认命名孤儿库（samp_<name>_<YYYY>.db）
        scan_dirs: list[Path] = [self.result_dir]
        for item in defs:
            td = item.get("target_dir") or ""
            if td:
                scan_dirs.append(self.resolve_path(td))
        scanned_dir_keys: set[str] = set()
        for directory in scan_dirs:
            d_key = os.path.normcase(str(directory.resolve()))
            if d_key in scanned_dir_keys or not directory.exists() or not directory.is_dir():
                continue
            scanned_dir_keys.add(d_key)
            for path in sorted(directory.glob("samp_*.db")):
                if not path.is_file():
                    continue
                norm = os.path.normcase(str(path.resolve()))
                if norm in seen:
                    continue
                match = _RESULT_FILE.fullmatch(path.name)
                if not match:
                    continue
                seen.add(norm)
                samp_name, year_str = match.group(1), match.group(2)
                meta = self._inspect_result_db_meta(path)
                items.append(
                    {
                        "filename": path.name,
                        "path": self.to_relative_path(path),
                        "samp_name": samp_name,
                        "year": int(year_str),
                        "size_bytes": meta["size_bytes"],
                        "modified_at": meta["modified_at"],
                        "row_count": meta["row_count"],
                        "table_count": meta["table_count"],
                        "tables": meta["tables"],
                        "min_sample_time": meta["min_sample_time"],
                        "max_sample_time": meta["max_sample_time"],
                        "rtdb_registered": meta["rtdb_registered"],
                        "is_orphan": samp_name not in defined_names,
                    }
                )
        items.sort(key=lambda x: x["path"])
        return items

    def _resolve_known_result_db(self, target_ref: str) -> Path | None:
        """解析并白名单校验已登记的结果库路径（兼容文件名与相对路径）。"""
        ref = str(target_ref or "").strip().replace("\\", "/")
        if not ref or ".." in Path(ref).parts or Path(ref).is_absolute():
            raise ValueError("非法的结果库路径")
        all_dbs = self.list_result_databases()
        known_by_rel = {item["path"]: self.resolve_path(item["path"]) for item in all_dbs}
        known_by_name: dict[str, Path] = {}
        for item in all_dbs:
            p = self.resolve_path(item["path"])
            if p.parent.resolve() == self.result_dir.resolve():
                known_by_name[item["filename"]] = p
        target = known_by_rel.get(ref) or known_by_name.get(ref)
        if target is None:
            if "/" not in ref and not _RESULT_FILE.fullmatch(ref):
                raise ValueError("非法的结果库文件名")
            return None
        return target

    def inspect_result_database(
        self,
        target_ref: str,
        table: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """在线透视结果库内部全部业务表、列类型、RTDB 登记及最新样本行。"""
        target = self._resolve_known_result_db(target_ref)
        if target is None or not target.is_file():
            raise KeyError("结果库文件不存在")
        safe_limit = max(1, min(int(limit), 100))
        tables_info: list[dict[str, Any]] = []
        with self._connect_readonly(target) as conn:
            rtdb_registered = (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rtdb_schema_tables'").fetchone()
                is not None
            )
            biz_tables = [
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                if str(r[0]).lower() not in REGISTRY_TABLES and _IDENT.match(str(r[0]))
            ]
            for tbl in biz_tables:
                info = conn.execute(f"PRAGMA table_info({self._quote(tbl)})").fetchall()
                cols = [{"name": str(r[1]), "type": _normalize_sql_type(str(r[2])), "pk": int(r[5])} for r in info]
                col_names = [c["name"] for c in cols]
                if "sample_time" in {c.lower() for c in col_names}:
                    cnt, mn, mx = conn.execute(
                        f"SELECT COUNT(*), MIN(sample_time), MAX(sample_time) FROM {self._quote(tbl)}"
                    ).fetchone()
                else:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM {self._quote(tbl)}").fetchone()[0]
                    mn, mx = None, None
                tables_info.append(
                    {
                        "name": tbl,
                        "row_count": int(cnt or 0),
                        "min_sample_time": int(mn) if mn is not None else None,
                        "max_sample_time": int(mx) if mx is not None else None,
                        "columns": cols,
                    }
                )
            selected_table = table if (table and any(t["name"] == table for t in tables_info)) else (tables_info[0]["name"] if tables_info else "")
            preview_columns: list[str] = []
            preview_rows: list[dict[str, Any]] = []
            if selected_table:
                tbl_meta = next(t for t in tables_info if t["name"] == selected_table)
                preview_columns = [c["name"] for c in tbl_meta["columns"]]
                order_sql = "ORDER BY sample_time DESC, row_key ASC" if "sample_time" in {c.lower() for c in preview_columns} else ""
                preview_rows = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT * FROM {self._quote(selected_table)} {order_sql} LIMIT ?",
                        (safe_limit,),
                    ).fetchall()
                ]
        return {
            "path": self.to_relative_path(target),
            "filename": target.name,
            "size_bytes": target.stat().st_size,
            "rtdb_registered": rtdb_registered,
            "tables": tables_info,
            "selected_table": selected_table,
            "columns": preview_columns,
            "rows": preview_rows,
        }

    def vacuum_result_database(self, target_ref: str) -> dict[str, Any]:
        """对指定结果库执行 WAL checkpoint + VACUUM 回收磁盘碎片空间。"""
        target = self._resolve_known_result_db(target_ref)
        if target is None or not target.is_file():
            raise KeyError("结果库文件不存在")
        db_key = os.path.normcase(str(target.resolve()))
        with self._db_lock(target):
            before = target.stat().st_size
            wal_path = Path(f"{target}-wal")
            if wal_path.exists():
                before += wal_path.stat().st_size
            conn = sqlite3.connect(str(target), timeout=30, isolation_level=None, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            after = target.stat().st_size
            if wal_path.exists():
                after += wal_path.stat().st_size
        with self._meta_cache_lock:
            self._result_meta_cache.pop(db_key, None)
        return {
            "ok": True,
            "path": self.to_relative_path(target),
            "before_bytes": before,
            "after_bytes": after,
            "saved_bytes": max(0, before - after),
        }

    def delete_result_database(self, target_ref: str) -> bool:
        """安全删除结果库（支持传默认目录文件名或相对根目录路径，严格限制仅允许删已登记结果库）。"""
        target = self._resolve_known_result_db(target_ref)
        if target is None:
            return False

        db_key = os.path.normcase(str(target.resolve()))
        with self._db_lock(target):
            target.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                extra = Path(f"{target}{suffix}")
                if extra.exists():
                    extra.unlink(missing_ok=True)
        self._drop_schema_cache_for_db(db_key)
        with self._meta_cache_lock:
            self._result_meta_cache.pop(db_key, None)
        return True
