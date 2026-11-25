"""跨进程运行态与执行日志持久化存储（samp_runtime.db）。

独立于 samp_define.db，避免污染 RTDB 业务结构表；
支持 Web（含 --no-scheduler）与独立 samp.sampler 共享单点运行态、日志与调度心跳。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class RuntimeStore:
    """基于独立 SQLite WAL 库的轻量跨进程状态与执行日志存储。"""

    def __init__(self, db_path: str | Path, max_logs: int = 500):
        self.db_path = str(db_path)
        self.max_logs = max(50, int(max_logs))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    pid INTEGER NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'web',
                    align INTEGER NOT NULL DEFAULT 1,
                    workers INTEGER NOT NULL DEFAULT 4,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_cycle_at INTEGER,
                    last_success_at INTEGER,
                    last_error TEXT,
                    sampled INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS definition_state (
                    samp_id INTEGER PRIMARY KEY,
                    last_run_at INTEGER,
                    last_success_at INTEGER,
                    last_status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT,
                    last_rows_written INTEGER,
                    last_duration_ms REAL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    next_run_at INTEGER,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time INTEGER NOT NULL,
                    samp_id INTEGER NOT NULL,
                    samp_name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rows_written INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_logs_time ON run_logs(id DESC)")

    def try_claim_scheduler(
        self,
        pid: int,
        mode: str,
        align: bool,
        workers: int,
        lease_seconds: int = 15,
    ) -> tuple[bool, int | None]:
        """尝试获取或续约全局调度器租约，避免 Web 与独立 sampler 双重采样。"""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT pid, enabled, updated_at FROM scheduler_heartbeat WHERE id=1").fetchone()
            if row is not None:
                holder_pid = int(row["pid"])
                active = bool(row["enabled"]) and (now - int(row["updated_at"])) < lease_seconds
                if holder_pid != pid and active:
                    return False, holder_pid
            conn.execute(
                """
                INSERT INTO scheduler_heartbeat(id, pid, mode, align, workers, enabled, last_cycle_at, sampled, failed, updated_at)
                VALUES (1, ?, ?, ?, ?, 1, ?, 0, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pid=excluded.pid,
                    mode=excluded.mode,
                    align=excluded.align,
                    workers=excluded.workers,
                    enabled=1,
                    last_cycle_at=excluded.last_cycle_at,
                    updated_at=excluded.updated_at
                """,
                (pid, mode, int(align), int(workers), now, now),
            )
        return True, pid

    def release_scheduler(self, pid: int) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scheduler_heartbeat SET enabled=0, updated_at=? WHERE id=1 AND pid=?",
                (now, pid),
            )

    def update_cycle_error(self, pid: int, error: str) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scheduler_heartbeat SET last_error=?, updated_at=? WHERE id=1 AND pid=?",
                (error, now, pid),
            )

    def record_execution(
        self,
        samp_id: int,
        samp_name: str,
        trigger: str,
        status: str,
        sampled_at: int,
        rows_written: int,
        duration_ms: float,
        message: str,
        error: str | None = None,
        next_run_at: int | None = None,
    ) -> None:
        """持久化单点执行快照、汇总计数与执行日志。"""
        now = int(time.time())
        is_ok = status == "ok"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO definition_state(
                    samp_id, last_run_at, last_success_at, last_status, last_error,
                    last_rows_written, last_duration_ms, success_count, fail_count, next_run_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(samp_id) DO UPDATE SET
                    last_run_at=excluded.last_run_at,
                    last_success_at=COALESCE(excluded.last_success_at, definition_state.last_success_at),
                    last_status=excluded.last_status,
                    last_error=excluded.last_error,
                    last_rows_written=CASE WHEN excluded.last_status='ok' THEN excluded.last_rows_written ELSE definition_state.last_rows_written END,
                    last_duration_ms=excluded.last_duration_ms,
                    success_count=definition_state.success_count + CASE WHEN excluded.last_status='ok' THEN 1 ELSE 0 END,
                    fail_count=definition_state.fail_count + CASE WHEN excluded.last_status='error' THEN 1 ELSE 0 END,
                    next_run_at=COALESCE(excluded.next_run_at, definition_state.next_run_at),
                    updated_at=excluded.updated_at
                """,
                (
                    samp_id,
                    sampled_at,
                    sampled_at if is_ok else None,
                    status,
                    error,
                    rows_written if is_ok else None,
                    duration_ms,
                    1 if is_ok else 0,
                    0 if is_ok else 1,
                    next_run_at,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO run_logs(time, samp_id, samp_name, trigger, status, rows_written, duration_ms, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sampled_at, samp_id, samp_name, trigger, status, rows_written, duration_ms, message),
            )
            # 定期裁剪日志表，维持上限
            conn.execute(
                "DELETE FROM run_logs WHERE id <= (SELECT id FROM run_logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self.max_logs,),
            )
            # 同步更新全局调度汇总计数
            hb = conn.execute("SELECT 1 FROM scheduler_heartbeat WHERE id=1").fetchone()
            if hb is None:
                conn.execute(
                    """
                    INSERT INTO scheduler_heartbeat(id, pid, mode, align, workers, enabled, last_cycle_at, last_success_at, last_error, sampled, failed, updated_at)
                    VALUES (1, ?, 'manual', 1, 1, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        os.getpid(),
                        now,
                        sampled_at if is_ok else None,
                        f"samp_id={samp_id}: {error}" if error else None,
                        1 if is_ok else 0,
                        0 if is_ok else 1,
                        now,
                    ),
                )
            else:
                if is_ok:
                    conn.execute(
                        "UPDATE scheduler_heartbeat SET sampled=sampled+1, last_success_at=?, updated_at=? WHERE id=1",
                        (sampled_at, now),
                    )
                else:
                    conn.execute(
                        "UPDATE scheduler_heartbeat SET failed=failed+1, last_error=?, updated_at=? WHERE id=1",
                        (f"samp_id={samp_id}: {error}", now),
                    )

    def update_next_runs(self, mapping: dict[int, int | None]) -> None:
        if not mapping:
            return
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO definition_state(samp_id, last_status, success_count, fail_count, next_run_at, updated_at)
                VALUES (?, 'idle', 0, 0, ?, ?)
                ON CONFLICT(samp_id) DO UPDATE SET next_run_at=excluded.next_run_at, updated_at=excluded.updated_at
                """,
                ((samp_id, nxt, now) for samp_id, nxt in mapping.items()),
            )

    def prune_definitions(self, active_ids: set[int]) -> None:
        with self._lock, self._connect() as conn:
            if not active_ids:
                conn.execute("DELETE FROM definition_state")
            else:
                placeholders = ",".join("?" for _ in active_ids)
                conn.execute(f"DELETE FROM definition_state WHERE samp_id NOT IN ({placeholders})", list(active_ids))

    def get_heartbeat(self, lease_seconds: int = 15) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduler_heartbeat WHERE id=1").fetchone()
        if row is None:
            return None
        data = dict(row)
        alive = bool(data["enabled"]) and (int(time.time()) - int(data["updated_at"])) < lease_seconds
        data["enabled"] = alive
        data["align"] = bool(data["align"])
        return data

    def get_definition_state(self, samp_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM definition_state WHERE samp_id=?", (samp_id,)).fetchone()
        return dict(row) if row else None

    def get_all_definition_states(self) -> dict[int, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM definition_state").fetchall()
        return {int(row["samp_id"]): dict(row) for row in rows}

    def get_logs(self, limit: int = 100, status: str = "all") -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), self.max_logs))
        with self._connect() as conn:
            if status in ("ok", "error"):
                rows = conn.execute(
                    "SELECT time, samp_id, samp_name, trigger, status, rows_written, duration_ms, message "
                    "FROM run_logs WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT time, samp_id, samp_name, trigger, status, rows_written, duration_ms, message "
                    "FROM run_logs ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
        return [dict(row) for row in rows]
