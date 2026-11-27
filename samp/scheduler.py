"""统一自动采样调度器与跨进程运行态/执行日志追踪（支持周期 / 定点 / 标志位触发 + 异步 API 作业）。"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .runtime_store import RuntimeStore
from .schedule import compute_fixed_next_run, fixed_due_slots
from .store import Store

LOGGER = logging.getLogger("samp.scheduler")


class SamplingScheduler:
    """支持周期对齐、按时/日/周/月秒级定点、数据库标志位触发、跨进程租约及非阻塞触发作业。"""

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
        self._last_flag_matched: dict[int, bool] = {}
        self._last_tick_ts: int | None = None
        self._local_enabled = True
        self._conflict_pid: int | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None

        # 非阻塞 API 触发作业状态表（有界保留最近 200 条）
        self._jobs_lock = threading.RLock()
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_jobs = 200

    def _lock_for(self, samp_id: int) -> threading.Lock:
        with self._state_lock:
            lock = self._sample_locks.get(samp_id)
            if lock is None:
                lock = threading.Lock()
                self._sample_locks[samp_id] = lock
            return lock

    def _compute_next_run_for_def(self, item: dict[str, Any], now: int) -> int | None:
        if not bool(item.get("enabled")):
            return None
        samp_id = int(item["id"])
        mode = str(item.get("start_mode") or "periodic").lower()
        if mode == "fixed":
            return compute_fixed_next_run(item.get("schedule_config") or {}, now)
        if mode == "trigger":
            return None
        interval = int(item.get("interval_seconds", 0))
        return self._compute_next_run(samp_id, interval, True, now)

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

    # ===================== 非阻塞 API 触发作业管理 =====================

    def submit_async_trigger(
        self,
        targets: list[dict[str, Any]],
        sampled_at: int | None = None,
        trigger: str = "api",
    ) -> dict[str, Any]:
        """非阻塞提交单点或批量触发采样作业，立即返回 job_id 与初始受理状态。"""
        job_id = uuid.uuid4().hex[:12]
        created_at = int(time.time())
        ids = [int(d["id"]) for d in targets]
        names = [str(d["samp_name"]) for d in targets]
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "accepted",
            "trigger": trigger,
            "requested": len(targets),
            "ids": ids,
            "names": names,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "completed": [],
            "failed": [],
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
            while len(self._jobs) > self._max_jobs:
                self._jobs.popitem(last=False)

        def runner() -> None:
            with self._jobs_lock:
                job["status"] = "running"
                job["started_at"] = int(time.time())
            completed: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            max_w = max(1, min(self.workers, len(targets)))
            with ThreadPoolExecutor(max_workers=max_w, thread_name_prefix="samp-api") as pool:
                fut_map = {
                    pool.submit(self.execute_sample, int(d["id"]), sampled_at, trigger): d
                    for d in targets
                }
                for fut in as_completed(fut_map):
                    d = fut_map[fut]
                    try:
                        completed.append(fut.result())
                    except Exception as exc:
                        failed.append({"id": int(d["id"]), "name": str(d["samp_name"]), "error": str(exc)})
            final_status = "done" if not failed else ("failed" if not completed else "partial")
            with self._jobs_lock:
                job["completed"] = completed
                job["failed"] = failed
                job["status"] = final_status
                job["finished_at"] = int(time.time())

        t = threading.Thread(target=runner, name=f"samp-job-{job_id}", daemon=True)
        t.start()
        return self.get_trigger_job(job_id) or job

    def get_trigger_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                **job,
                "ids": list(job["ids"]),
                "names": list(job["names"]),
                "completed": list(job["completed"]),
                "failed": list(job["failed"]),
            }

    def list_trigger_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._jobs_lock:
            items = list(reversed(self._jobs.values()))[: max(1, min(int(limit), self._max_jobs))]
            return [
                {
                    **j,
                    "ids": list(j["ids"]),
                    "names": list(j["names"]),
                    "completed": list(j["completed"]),
                    "failed": list(j["failed"]),
                }
                for j in items
            ]

    # ===================== 3 种启动方式统一到期计算 =====================

    def _due_definitions(
        self,
        definitions: list[dict[str, Any]],
        now: int,
        force_once: bool = False,
    ) -> tuple[list[tuple[int, int, str]], dict[int, int | None]]:
        """返回 ([(samp_id, sampled_at, trigger_kind), ...], {samp_id: next_run_at})。"""
        due: list[tuple[int, int, str]] = []
        next_runs: dict[int, int | None] = {}
        active_ids = {int(item["id"]) for item in definitions}

        # 一次性批量拉取全部启用中的内置触发标志位，避免逐条查库
        builtin_trigger_ids = [
            int(item["id"])
            for item in definitions
            if bool(item.get("enabled"))
            and str(item.get("start_mode") or "periodic").lower() == "trigger"
            and str((item.get("schedule_config") or {}).get("source") or "builtin").lower() == "builtin"
        ]
        builtin_flags = self.store.read_builtin_trigger_flags(builtin_trigger_ids) if builtin_trigger_ids else {}

        with self._state_lock:
            prev_tick = self._last_tick_ts
            # 首次 tick 仅看当秒 (now-1, now]，避免服务启动瞬间补采历史定点；后续看 (prev_tick, now]
            fixed_window_start = (now - 1) if prev_tick is None else min(prev_tick, now - 1)
            self._last_tick_ts = now

            for item in definitions:
                samp_id = int(item["id"])
                enabled = bool(item.get("enabled"))
                mode = str(item.get("start_mode") or "periodic").lower()
                cfg = item.get("schedule_config") or {}

                if not enabled:
                    self._last_slot.pop(samp_id, None)
                    self._last_flag_matched.pop(samp_id, None)
                    next_runs[samp_id] = None
                    continue

                if samp_id in self._running:
                    next_runs[samp_id] = self._compute_next_run_for_def(item, now)
                    continue

                # 1) 周期采样
                if mode == "periodic":
                    interval = int(item.get("interval_seconds", 0))
                    if interval <= 0:
                        self._last_slot.pop(samp_id, None)
                        next_runs[samp_id] = None
                        continue
                    if force_once:
                        slot = (now // interval) * interval if self.align else now
                        self._last_slot[samp_id] = slot
                        due.append((samp_id, slot, "auto"))
                        next_runs[samp_id] = slot + interval
                        continue
                    previous = self._last_slot.get(samp_id)
                    if self.align:
                        slot = (now // interval) * interval
                        if previous is None or slot > previous:
                            self._last_slot[samp_id] = slot
                            due.append((samp_id, slot, "auto"))
                    else:
                        if previous is None or now - previous >= interval:
                            self._last_slot[samp_id] = now
                            due.append((samp_id, now, "auto"))
                    next_runs[samp_id] = self._compute_next_run(samp_id, interval, True, now)
                    continue

                # 2) 定点采样（按时/日/周/月，支持多点，秒级精度）
                if mode == "fixed":
                    if force_once:
                        self._last_slot[samp_id] = now
                        due.append((samp_id, now, "fixed"))
                        next_runs[samp_id] = compute_fixed_next_run(cfg, now)
                        continue
                    hits = fixed_due_slots(cfg, fixed_window_start, now)
                    previous = self._last_slot.get(samp_id)
                    if hits:
                        target_slot = hits[-1]
                        if previous is None or target_slot > previous:
                            self._last_slot[samp_id] = target_slot
                            due.append((samp_id, target_slot, "fixed"))
                    next_runs[samp_id] = compute_fixed_next_run(cfg, now)
                    continue

                # 3) 触发采样（内置或外部数据库标志位）
                if mode == "trigger":
                    next_runs[samp_id] = None
                    if force_once:
                        due.append((samp_id, now, "flag"))
                        continue
                    source = str(cfg.get("source") or "builtin").lower()
                    trig_val = str(cfg.get("trigger_value") if cfg.get("trigger_value") not in (None, "") else "1")
                    reset_val = str(cfg.get("reset_value") if cfg.get("reset_value") is not None else "0")
                    auto_reset = bool(cfg.get("auto_reset", True))

                    try:
                        if source == "builtin":
                            raw_val = builtin_flags.get(samp_id)
                        else:
                            raw_val = self.store.read_external_trigger(cfg)
                    except Exception as exc:
                        LOGGER.warning("检测触发标志位失败 samp_id=%s: %s", samp_id, exc)
                        self.runtime_store.record_execution(
                            samp_id=samp_id,
                            samp_name=str(item.get("samp_name") or f"#{samp_id}"),
                            trigger="flag",
                            status="error",
                            sampled_at=now,
                            rows_written=0,
                            duration_ms=0.0,
                            message=str(exc),
                            error=str(exc),
                        )
                        continue

                    matched = self.store._flag_matches(raw_val, trig_val)
                    prev_matched = self._last_flag_matched.get(samp_id, False)
                    should_fire = matched and (auto_reset or not prev_matched)
                    self._last_flag_matched[samp_id] = matched

                    if should_fire:
                        # 命中后优先消费复位标志位，防止下一轮 500ms 重复拉起，同时不丢失执行期间的再次置位
                        try:
                            if source == "builtin":
                                if auto_reset:
                                    self.store.reset_builtin_trigger(samp_id, now, reset_val)
                                    self._last_flag_matched[samp_id] = False
                                else:
                                    self.store.mark_trigger_time(samp_id, now)
                            else:
                                if auto_reset:
                                    self.store.reset_external_trigger(cfg, now, samp_id=samp_id)
                                    self._last_flag_matched[samp_id] = False
                                else:
                                    self.store.mark_trigger_time(samp_id, now)
                        except Exception as exc:
                            LOGGER.warning("复位触发标志位失败 samp_id=%s: %s", samp_id, exc)
                        due.append((samp_id, now, "flag"))
                    continue

            for stale in set(self._last_slot) - active_ids:
                self._last_slot.pop(stale, None)
            for stale in set(self._last_flag_matched) - active_ids:
                self._last_flag_matched.pop(stale, None)
        return due, next_runs

    def tick(self, force_once: bool = False, now_override: int | None = None) -> bool:
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

        now = int(time.time()) if now_override is None else int(now_override)
        definitions = self.store.list_definitions()
        due, next_runs = self._due_definitions(definitions, now, force_once=force_once)
        self.runtime_store.update_next_runs(next_runs)
        if not due:
            return True

        pool = self._pool
        if pool is not None:
            futures = {
                pool.submit(self.execute_sample, samp_id, ts, trig): samp_id
                for samp_id, ts, trig in due
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
                    temp_pool.submit(self.execute_sample, samp_id, ts, trig): samp_id
                    for samp_id, ts, trig in due
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

    def snapshot_definition(
        self,
        samp_id: int,
        interval: int,
        enabled: bool,
        start_mode: str = "periodic",
        schedule_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        persisted = self.runtime_store.get_definition_state(samp_id)
        return self._merge_def_snapshot(
            {
                "id": samp_id,
                "interval_seconds": interval,
                "enabled": enabled,
                "start_mode": start_mode,
                "schedule_config": schedule_config or {},
            },
            persisted,
        )

    def snapshot_all_definitions(self, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        all_states = self.runtime_store.get_all_definition_states()
        return {
            int(item["id"]): self._merge_def_snapshot(item, all_states.get(int(item["id"])))
            for item in items
        }

    def _merge_def_snapshot(
        self,
        item: dict[str, Any],
        persisted: dict[str, Any] | None,
    ) -> dict[str, Any]:
        samp_id = int(item["id"])
        enabled = bool(item.get("enabled"))
        mode = str(item.get("start_mode") or "periodic").lower()
        interval = int(item.get("interval_seconds", 0))
        has_schedule = enabled and ((mode == "periodic" and interval > 0) or mode == "fixed")
        with self._state_lock:
            is_running = samp_id in self._running
            local_next = self._compute_next_run_for_def(item, int(time.time())) if (self._local_enabled and has_schedule) else None
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
        next_at = persisted["next_run_at"] if has_schedule else None
        if has_schedule and next_at is None:
            next_at = local_next
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
            "next_run_at": next_at,
        }

    def get_logs(self, limit: int = 100, status: str = "all") -> list[dict[str, Any]]:
        return self.runtime_store.get_logs(limit=limit, status=status)
