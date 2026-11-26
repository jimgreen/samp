"""统一自动采样调度器与跨进程运行态/执行日志追踪。"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .runtime_store import RuntimeStore
from .store import Store

LOGGER = logging.getLogger("samp.scheduler")


class SamplingScheduler:
    """支持整周期对齐、跨进程防双开租约、按采样点防重入执行与持久化日志追踪。"""

    def __init__(
        self,
        store: Store,
        workers: int = 4,
        align: bool = True,
        max_logs: int = 500,
        mode: str = "web",
        runtime_db: str | Path | None = None,
    ):
        self.store = store
        self.workers = max(1, int(workers))
        self.align = bool(align)
        self.mode = mode
        self._pid = os.getpid()
        runtime_path = runtime_db if runtime_db is not None else Path(store.definition_db).with_name("samp_runtime.db")
        self.runtime_store = RuntimeStore(runtime_path, max_logs=max_logs)

        self._state_lock = threading.RLock()
        self._sample_locks: dict[int, threading.Lock] = {}
        self._running: set[int] = set()
        self._last_slot: dict[int, int] = {}
        self._local_enabled = True
        self._conflict_pid: int | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None

    def _lock_for(self, samp_id: int) -> threading.Lock:
        with self._state_lock:
            lock = self._sample_locks.get(samp_id)
            if lock is None:
                lock = threading.Lock()
                self._sample_locks[samp_id] = lock
            return lock

    def _compute_next_run(self, samp_id: int, interval: int, enabled: bool, now: int) -> int | None:
        if not enabled or interval <= 0:
            return None
        prev = self._last_slot.get(samp_id)
        if self.align:
            current_slot = (now // interval) * interval
            if prev is None:
                return current_slot + interval
            return max(current_slot + interval, prev + interval)
        if prev is None:
            return now
        return prev + interval

    def execute_sample(
        self,
        samp_id: int,
        sampled_at: int | None = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        """线程安全地执行单次采样并持久化单点状态与执行日志。"""
        ts = int(time.time() if sampled_at is None else sampled_at)
        started = time.perf_counter()
        with self._state_lock:
            self._running.add(samp_id)
        try:
            with self._lock_for(samp_id):
                result = self.store.run_sample(samp_id, ts)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            err_text = str(exc)
            now_wall = int(time.time())
            with self._state_lock:
                self._running.discard(samp_id)
            self.runtime_store.record_execution(
                samp_id=samp_id,
                samp_name=f"#{samp_id}",
                trigger=trigger,
                status="error",
                sampled_at=now_wall,
                rows_written=0,
                duration_ms=duration_ms,
                message=err_text,
                error=err_text,
            )
            raise
        else:
            duration_ms = result.get("duration_ms", round((time.perf_counter() - started) * 1000.0, 2))
            with self._state_lock:
                self._running.discard(samp_id)
            tables = result.get("tables") or [result["table"]]
            tbl_label = tables[0] if len(tables) == 1 else f"{tables[0]} (+{len(tables) - 1}表)"
            self.runtime_store.record_execution(
                samp_id=samp_id,
                samp_name=result.get("samp_name", f"#{samp_id}"),
                trigger=trigger,
                status="ok",
                sampled_at=result["sampled_at"],
                rows_written=result["rows_written"],
                duration_ms=duration_ms,
                message=f"{tbl_label} (+{result['rows_written']})",
                error=None,
            )
            return result

    def _due_definitions(
        self,
        definitions: list[dict[str, Any]],
        now: int,
        force_once: bool = False,
    ) -> tuple[list[tuple[int, int]], dict[int, int | None]]:
        due: list[tuple[int, int]] = []
        next_runs: dict[int, int | None] = {}
        active_ids = {item["id"] for item in definitions}
        with self._state_lock:
            for item in definitions:
                samp_id = item["id"]
                interval = int(item.get("interval_seconds", 0))
                enabled = bool(item.get("enabled"))
                if not enabled or interval <= 0:
                    self._last_slot.pop(samp_id, None)
                    next_runs[samp_id] = None
                    continue
                if samp_id in self._running:
                    next_runs[samp_id] = self._compute_next_run(samp_id, interval, True, now)
                    continue
                if force_once:
                    slot = (now // interval) * interval if self.align else now
                    self._last_slot[samp_id] = slot
                    due.append((samp_id, slot))
                    next_runs[samp_id] = slot + interval
                    continue
                previous = self._last_slot.get(samp_id)
                if self.align:
                    slot = (now // interval) * interval
                    if previous is None or slot > previous:
                        self._last_slot[samp_id] = slot
                        due.append((samp_id, slot))
                else:
                    if previous is None or now - previous >= interval:
                        self._last_slot[samp_id] = now
                        due.append((samp_id, now))
                next_runs[samp_id] = self._compute_next_run(samp_id, interval, True, now)
            for stale in set(self._last_slot) - active_ids:
                self._last_slot.pop(stale, None)
        return due, next_runs

    def tick(self, force_once: bool = False) -> bool:
        """执行单轮调度检查；若其他进程正持有活跃租约则跳过自动调度防止重复采样。"""
        claimed, holder_pid = self.runtime_store.try_claim_scheduler(
            self._pid, self.mode, self.align, self.workers
        )
        if not claimed and not force_once:
            if self._conflict_pid != holder_pid:
                self._conflict_pid = holder_pid
                LOGGER.warning("检测到另一活跃调度进程 (pid=%s)，当前进程跳过自动采样", holder_pid)
            return False
        self._conflict_pid = None

        now = int(time.time())
        definitions = self.store.list_definitions()
        due, next_runs = self._due_definitions(definitions, now, force_once=force_once)
        self.runtime_store.update_next_runs(next_runs)
        if not due:
            return True

        pool = self._pool
        if pool is not None:
            futures = {
                pool.submit(self.execute_sample, samp_id, ts, "auto"): samp_id
                for samp_id, ts in due
            }
            for future in as_completed(futures):
                samp_id = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    LOGGER.warning("自动采样失败 samp_id=%s: %s", samp_id, exc)
        else:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="samp") as temp_pool:
                futures = {
                    temp_pool.submit(self.execute_sample, samp_id, ts, "auto"): samp_id
                    for samp_id, ts in due
                }
                for future in as_completed(futures):
                    samp_id = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        LOGGER.warning("自动采样失败 samp_id=%s: %s", samp_id, exc)
        return True

    def _run_loop(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="samp")
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    self.tick(force_once=False)
                except Exception as exc:
                    LOGGER.warning("调度循环异常: %s", exc)
                    self.runtime_store.update_cycle_error(self._pid, str(exc))
                elapsed = time.monotonic() - started
                self._stop_event.wait(timeout=max(0.05, 0.5 - elapsed))
        finally:
            self._pool.shutdown(wait=True)
            self._pool = None
            self.runtime_store.release_scheduler(self._pid)

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._local_enabled = True
            self._thread = threading.Thread(target=self._run_loop, name="samp-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None

    def mark_disabled(self) -> None:
        self._local_enabled = False

    def snapshot_summary(self) -> dict[str, Any]:
        hb = self.runtime_store.get_heartbeat()
        if hb is not None:
            return {
                "enabled": bool(hb["enabled"]),
                "mode": hb["mode"],
                "pid": hb["pid"],
                "align": bool(hb["align"]),
                "workers": int(hb["workers"]),
                "last_cycle_at": hb["last_cycle_at"],
                "last_success_at": hb["last_success_at"],
                "last_error": hb["last_error"],
                "sampled": int(hb["sampled"] or 0),
                "failed": int(hb["failed"] or 0),
            }
        return {
            "enabled": bool(self._local_enabled),
            "mode": self.mode,
            "pid": self._pid,
            "align": self.align,
            "workers": self.workers,
            "last_cycle_at": None,
            "last_success_at": None,
            "last_error": None,
            "sampled": 0,
            "failed": 0,
        }

    def snapshot_definition(self, samp_id: int, interval: int, enabled: bool) -> dict[str, Any]:
        persisted = self.runtime_store.get_definition_state(samp_id)
        return self._merge_def_snapshot(samp_id, interval, enabled, persisted)

    def snapshot_all_definitions(self, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        all_states = self.runtime_store.get_all_definition_states()
        return {
            item["id"]: self._merge_def_snapshot(
                item["id"],
                int(item.get("interval_seconds", 0)),
                bool(item.get("enabled")),
                all_states.get(item["id"]),
            )
            for item in items
        }

    def _merge_def_snapshot(
        self,
        samp_id: int,
        interval: int,
        enabled: bool,
        persisted: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._state_lock:
            is_running = samp_id in self._running
            local_next = self._compute_next_run(samp_id, interval, enabled, int(time.time())) if self._local_enabled else None
        if persisted is None:
            return {
                "last_run_at": None,
                "last_success_at": None,
                "last_status": "idle",
                "last_error": None,
                "last_rows_written": None,
                "last_duration_ms": None,
                "success_count": 0,
                "fail_count": 0,
                "running": is_running,
                "next_run_at": local_next,
            }
        return {
            "last_run_at": persisted["last_run_at"],
            "last_success_at": persisted["last_success_at"],
            "last_status": persisted["last_status"] or "idle",
            "last_error": persisted["last_error"],
            "last_rows_written": persisted["last_rows_written"],
            "last_duration_ms": persisted["last_duration_ms"],
            "success_count": int(persisted["success_count"] or 0),
            "fail_count": int(persisted["fail_count"] or 0),
            "running": is_running,
            "next_run_at": persisted["next_run_at"] if enabled and interval > 0 else None,
        }

    def get_logs(self, limit: int = 100, status: str = "all") -> list[dict[str, Any]]:
        return self.runtime_store.get_logs(limit=limit, status=status)
