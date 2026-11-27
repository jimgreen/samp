from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .scheduler import SamplingScheduler
from .store import DEFAULT_DEFINITION_DB, DEFAULT_RESULT_DIR, DEFAULT_SOURCE_DB, ROOT_DIR, SQLITE_DIR, Store

LOGGER = logging.getLogger("samp")
WEB_DIR = Path(__file__).with_name("web")


class SampleItemIn(BaseModel):
    item_id: str = ""
    source_database: str = ""
    source_table: str
    target_table: str = ""
    fields: list[str] = Field(min_length=1)
    filter: str = ""


class DefinitionIn(BaseModel):
    name: str
    source_database: str = ""
    source_table: str = ""
    target_dir: str = ""
    target_name: str = ""
    fields: list[str] = Field(default_factory=list)
    items: list[SampleItemIn] | None = None
    start_mode: str = "periodic"  # periodic | fixed | trigger
    schedule_config: dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int = Field(default=60, ge=0)
    filter: str = ""
    enabled: bool = True
    rename_results: bool = False


class ImportDefinitionsIn(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    mode: str = "skip"


class BatchEnableIn(BaseModel):
    ids: list[int] = Field(min_length=1)
    enabled: bool


class BatchIdsIn(BaseModel):
    ids: list[int] = Field(min_length=1)


class StoragePathIn(BaseModel):
    path: str = Field(min_length=1)


class TriggerSingleIn(BaseModel):
    blocking: bool = True
    sampled_at: int | None = None
    mode: str = "direct"  # direct | flag


class TriggerBatchIn(BaseModel):
    ids: list[int] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    blocking: bool = True
    sampled_at: int | None = None
    mode: str = "direct"  # direct | flag


class TableSampleEntryIn(BaseModel):
    source_table: str
    name: str = ""
    target_table: str = ""
    fields: list[str] = Field(default_factory=lambda: ["*"])
    filter: str = ""
    start_mode: str = "periodic"
    schedule_config: dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int = Field(default=60, ge=0)
    enabled: bool = True


class TableSampleBatchIn(BaseModel):
    source_database: str
    target_dir: str = ""
    target_mode: str = "per_table"  # per_table | single_db
    bundle_name: str = ""
    target_name: str = ""
    conflict_mode: str = "overwrite"  # overwrite | skip
    sample_now: bool = False
    tables: list[TableSampleEntryIn] = Field(min_length=1)


class SourceRowsIn(BaseModel):
    source_database: str = ""
    source_table: str
    rows: list[dict[str, Any]]


def _split_csv_param(raw: str) -> list[str] | None:
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    return items or None


def create_app(
    definition_db: str | Path = DEFAULT_DEFINITION_DB,
    source_db: str | Path = DEFAULT_SOURCE_DB,
    result_dir: str | Path = DEFAULT_RESULT_DIR,
    enable_scheduler: bool = True,
    workers: int = 4,
    align: bool = True,
    root_dir: str | Path | None = None,
) -> FastAPI:
    """构建 SAMP FastAPI 应用，统一托管配置、调度、查询与存储治理接口。"""
    store = Store(definition_db, source_db, result_dir, root_dir=root_dir)
    scheduler = SamplingScheduler(store, workers=workers, align=align)
    if not enable_scheduler:
        scheduler.mark_disabled()

    async def execute_sample(samp_id: int, sampled_at: int | None = None, trigger: str = "manual") -> dict[str, Any]:
        return await asyncio.to_thread(scheduler.execute_sample, samp_id, sampled_at, trigger)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if enable_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if enable_scheduler:
                scheduler.stop()

    app = FastAPI(title="SAMP 采样服务", version="0.2.0", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/config")
    def config(refresh: bool = False):
        return {
            "root_dir": store.root_dir.as_posix(),
            "default_source_db": store.to_relative_path(store.default_source_db),
            "sqlite_dir": store.to_relative_path(store.result_dir),
            "scheduler_enabled": bool(enable_scheduler),
            "align": bool(align),
            "workers": int(workers),
            "directories": store.list_directories(refresh=refresh),
            "source_databases": store.list_source_databases(refresh=refresh),
            "target_dirs": store.list_target_dirs(refresh=refresh),
        }

    @app.get("/api/definitions")
    def definitions():
        return store.list_definitions()

    @app.post("/api/definitions")
    def create_definition(payload: DefinitionIn):
        try:
            return store.upsert_definition(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"采样点名已存在: {exc}")

    @app.get("/api/definitions/export")
    def export_definitions():
        return store.export_definitions()

    @app.post("/api/definitions/import")
    def import_definitions(payload: ImportDefinitionsIn):
        try:
            return store.import_definitions(payload.items, payload.mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.put("/api/definitions/{samp_id}")
    def update_definition(samp_id: int, payload: DefinitionIn, rename_results: bool = False):
        if not store.get_definition(samp_id):
            raise HTTPException(404, "采样定义不存在")
        try:
            return store.upsert_definition(
                payload.model_dump(),
                samp_id,
                rename_results=bool(rename_results or payload.rename_results),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"采样点名已存在: {exc}")

    @app.delete("/api/definitions/{samp_id}")
    def delete_definition(samp_id: int):
        if not store.delete_definition(samp_id):
            raise HTTPException(404, "采样定义不存在")
        return {"ok": True}

    @app.post("/api/definitions/{samp_id}/items")
    def add_definition_item(samp_id: int, payload: SampleItemIn):
        try:
            return store.add_definition_item(samp_id, payload.model_dump())
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.put("/api/definitions/{samp_id}/items/{item_id}")
    def update_definition_item(samp_id: int, item_id: str, payload: SampleItemIn):
        try:
            return store.update_definition_item(samp_id, item_id, payload.model_dump())
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/definitions/{samp_id}/items/{item_id}")
    def delete_definition_item(samp_id: int, item_id: str):
        try:
            return store.delete_definition_item(samp_id, item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/definitions/batch-enable")
    def batch_enable_definitions(payload: BatchEnableIn):
        updated = store.set_enabled_batch(payload.ids, payload.enabled)
        return {"updated": updated, "enabled": payload.enabled}

    @app.post("/api/definitions/batch-delete")
    def batch_delete_definitions(payload: BatchIdsIn):
        deleted = store.delete_definitions_batch(payload.ids)
        return {"deleted": deleted}

    @app.post("/api/definitions/{samp_id}/enable")
    def enable_definition(samp_id: int, enabled: bool = True):
        value = store.set_enabled(samp_id, enabled)
        if not value:
            raise HTTPException(404, "采样定义不存在")
        return value

    @app.post("/api/sample/batch")
    async def sample_batch(payload: BatchIdsIn):
        unique_ids = list(dict.fromkeys(int(i) for i in payload.ids))
        results = await asyncio.gather(
            *(execute_sample(sid, trigger="batch") for sid in unique_ids),
            return_exceptions=True,
        )
        completed, failed = [], []
        for sid, result in zip(unique_ids, results):
            if isinstance(result, Exception):
                failed.append({"id": sid, "error": str(result)})
            else:
                completed.append(result)
        return {"requested": len(unique_ids), "completed": completed, "failed": failed}

    @app.post("/api/sample/all")
    async def sample_all():
        defs = [item for item in store.list_definitions() if item.get("enabled")]
        results = await asyncio.gather(
            *(execute_sample(item["id"], trigger="batch") for item in defs),
            return_exceptions=True,
        )
        completed, failed = [], []
        for item, result in zip(defs, results):
            if isinstance(result, Exception):
                failed.append({"id": item["id"], "name": item["samp_name"], "error": str(result)})
            else:
                completed.append(result)
        return {"requested": len(defs), "completed": completed, "failed": failed}

    @app.post("/api/sample/{samp_id}")
    async def sample_now(samp_id: int):
        try:
            return await execute_sample(samp_id, trigger="manual")
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/definitions/{samp_id}/fields")
    def definition_fields(samp_id: int, item_id: str | None = None):
        try:
            return store.resolve_definition_fields(samp_id, item_id=item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/table-sample")
    async def table_sample(payload: TableSampleBatchIn):
        """整表采样批量创建/更新接口：支持按表独立建点或合并至同一结果库，并可选立即执行采样。"""
        mode = (payload.target_mode or "per_table").lower()
        conflict = (payload.conflict_mode or "overwrite").lower()
        if mode not in ("per_table", "single_db"):
            raise HTTPException(400, "target_mode 仅支持 per_table 或 single_db")
        if conflict not in ("overwrite", "skip"):
            raise HTTPException(400, "conflict_mode 仅支持 overwrite 或 skip")

        existing = {d["samp_name"]: d["id"] for d in store.list_definitions()}
        saved_defs: list[dict[str, Any]] = []
        created, updated, skipped = 0, 0, 0
        errors: list[dict[str, Any]] = []

        if mode == "single_db":
            bundle_name = (payload.bundle_name or "").strip()
            if not bundle_name:
                raise HTTPException(400, "合并到同一结果库时必须指定采样点名")
            items_payload = []
            for idx, entry in enumerate(payload.tables):
                tgt = (entry.target_table or "").strip()
                if not tgt and idx > 0:
                    tgt = entry.source_table
                items_payload.append(
                    {
                        "source_database": payload.source_database,
                        "source_table": entry.source_table,
                        "target_table": tgt,
                        "fields": entry.fields or ["*"],
                        "filter": entry.filter or "",
                    }
                )
            first = payload.tables[0]
            def_payload = {
                "name": bundle_name,
                "target_dir": payload.target_dir,
                "target_name": payload.target_name,
                "start_mode": first.start_mode,
                "schedule_config": first.schedule_config,
                "interval_seconds": first.interval_seconds,
                "enabled": first.enabled,
                "items": items_payload,
            }
            try:
                if bundle_name in existing:
                    if conflict == "skip":
                        skipped += 1
                    else:
                        saved = store.upsert_definition(def_payload, existing[bundle_name])
                        saved_defs.append(saved)
                        updated += 1
                else:
                    saved = store.upsert_definition(def_payload)
                    saved_defs.append(saved)
                    created += 1
            except (ValueError, sqlite3.Error) as exc:
                raise HTTPException(400, str(exc))
        else:
            for idx, entry in enumerate(payload.tables):
                pt_name = (entry.name or entry.source_table).strip()
                def_payload = {
                    "name": pt_name,
                    "source_database": payload.source_database,
                    "source_table": entry.source_table,
                    "target_dir": payload.target_dir,
                    "target_name": "",
                    "fields": entry.fields or ["*"],
                    "filter": entry.filter or "",
                    "start_mode": entry.start_mode,
                    "schedule_config": entry.schedule_config,
                    "interval_seconds": entry.interval_seconds,
                    "enabled": entry.enabled,
                }
                try:
                    if pt_name in existing:
                        if conflict == "skip":
                            skipped += 1
                            continue
                        saved = store.upsert_definition(def_payload, existing[pt_name])
                        saved_defs.append(saved)
                        updated += 1
                    else:
                        saved = store.upsert_definition(def_payload)
                        existing[saved["samp_name"]] = saved["id"]
                        saved_defs.append(saved)
                        created += 1
                except (ValueError, sqlite3.Error) as exc:
                    errors.append({"index": idx, "table": entry.source_table, "name": pt_name, "error": str(exc)})

        sampled: list[dict[str, Any]] = []
        sample_errors: list[dict[str, Any]] = []
        if payload.sample_now and saved_defs:
            results = await asyncio.gather(
                *(execute_sample(d["id"], trigger="manual") for d in saved_defs),
                return_exceptions=True,
            )
            for d, res in zip(saved_defs, results):
                if isinstance(res, Exception):
                    sample_errors.append({"id": d["id"], "name": d["samp_name"], "error": str(res)})
                else:
                    sampled.append(res)

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "definitions": saved_defs,
            "sampled": sampled,
            "sample_errors": sample_errors,
            "rows_written": sum(int(r.get("rows_written", 0)) for r in sampled),
        }

    @app.get("/api/definitions/{samp_id}/runtime")
    def definition_runtime(samp_id: int):
        try:
            item = store.runtime_status(samp_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        item.update(
            scheduler.snapshot_definition(
                samp_id,
                item["interval_seconds"],
                item["enabled"],
                start_mode=item.get("start_mode", "periodic"),
                schedule_config=item.get("schedule_config") or {},
            )
        )
        return item

    # ===================== 外部应用触发采样 API（支持单点/批量、阻塞/非阻塞、直调/标志置位） =====================

    async def _run_trigger_targets(
        targets: list[dict[str, Any]],
        blocking: bool,
        sampled_at: int | None,
        mode: str,
        single: bool = False,
    ):
        norm_mode = (mode or "direct").strip().lower()
        if norm_mode not in ("direct", "flag"):
            raise HTTPException(400, "mode 仅支持 direct 或 flag")
        if norm_mode == "flag":
            flagged = []
            for d in targets:
                store.set_trigger_flag(int(d["id"]), 1)
                flagged.append({"id": int(d["id"]), "name": str(d["samp_name"]), "trigger_flag": 1})
            return JSONResponse(
                status_code=202,
                content={"status": "flagged", "mode": "flag", "requested": len(flagged), "items": flagged},
            )
        if not blocking:
            job = scheduler.submit_async_trigger(targets, sampled_at=sampled_at, trigger="api")
            return JSONResponse(status_code=202, content=job)

        started = time.perf_counter()
        if single:
            target = targets[0]
            try:
                res = await execute_sample(int(target["id"]), sampled_at=sampled_at, trigger="api")
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {"status": "done", "blocking": True, **res}

        results = await asyncio.gather(
            *(execute_sample(int(d["id"]), sampled_at=sampled_at, trigger="api") for d in targets),
            return_exceptions=True,
        )
        completed, failed = [], []
        for d, r in zip(targets, results):
            if isinstance(r, Exception):
                failed.append({"id": int(d["id"]), "name": str(d["samp_name"]), "error": str(r)})
            else:
                completed.append(r)
        status_text = "done" if not failed else ("failed" if not completed else "partial")
        return {
            "status": status_text,
            "blocking": True,
            "requested": len(targets),
            "completed": completed,
            "failed": failed,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }

    @app.get("/api/trigger/jobs")
    def list_trigger_jobs(limit: int = Query(50, ge=1, le=200)):
        return {"jobs": scheduler.list_trigger_jobs(limit=limit)}

    @app.get("/api/trigger/jobs/{job_id}")
    def get_trigger_job(job_id: str):
        job = scheduler.get_trigger_job(job_id)
        if job is None:
            raise HTTPException(404, "触发作业不存在")
        return job

    @app.post("/api/trigger")
    async def trigger_batch_api(payload: TriggerBatchIn):
        """批量（或单点）API 触发采样：支持按 ids / names 定位，支持 blocking=true 阻塞与 blocking=false 非阻塞。"""
        if not payload.ids and not payload.names:
            raise HTTPException(400, "ids 或 names 至少需提供一项")
        try:
            targets = store.resolve_definition_targets(ids=payload.ids, names=payload.names)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        if not targets:
            raise HTTPException(400, "未匹配到有效采样定义")
        return await _run_trigger_targets(
            targets,
            blocking=payload.blocking,
            sampled_at=payload.sampled_at,
            mode=payload.mode,
            single=False,
        )

    @app.post("/api/trigger/{target}")
    async def trigger_single_api(
        target: str,
        blocking: bool | None = None,
        sampled_at: int | None = None,
        mode: str | None = None,
        payload: TriggerSingleIn | None = Body(default=None),
    ):
        """单点 API 触发采样：target 支持数字 ID 或采样点名称，支持阻塞/非阻塞及标志置位模式。"""
        eff_blocking = blocking if blocking is not None else (payload.blocking if payload is not None else True)
        eff_ts = sampled_at if sampled_at is not None else (payload.sampled_at if payload is not None else None)
        eff_mode = mode if mode is not None else (payload.mode if payload is not None else "direct")
        try:
            if target.isdigit():
                targets = store.resolve_definition_targets(ids=[int(target)])
            else:
                targets = store.resolve_definition_targets(names=[target])
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return await _run_trigger_targets(
            targets,
            blocking=eff_blocking,
            sampled_at=eff_ts,
            mode=eff_mode,
            single=True,
        )

    @app.post("/api/definitions/{samp_id}/trigger")
    async def trigger_definition_alias(
        samp_id: int,
        blocking: bool | None = None,
        sampled_at: int | None = None,
        mode: str | None = None,
        payload: TriggerSingleIn | None = Body(default=None),
    ):
        """采样定义单点触发快捷接口（同 /api/trigger/{id}）。"""
        return await trigger_single_api(
            str(samp_id),
            blocking=blocking,
            sampled_at=sampled_at,
            mode=mode,
            payload=payload,
        )

    @app.get("/api/query/{samp_id}/times")
    def query_times(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        limit: int = Query(200, ge=1, le=2000),
        item_id: str | None = None,
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            return store.query_sample_times(samp_id, start, end, limit, item_id=item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/query/{samp_id}/keys")
    def query_keys(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        limit: int = Query(500, ge=1, le=5000),
        item_id: str | None = None,
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            return store.query_keys(samp_id, start, end, limit, item_id=item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/query/{samp_id}/series")
    def query_series(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        fields: str = "",
        row_keys: str = "",
        bucket_seconds: int = Query(0, ge=0),
        agg: str = Query("avg"),
        max_points: int = Query(600, ge=50, le=5000),
        max_raw_points: int = Query(20000, ge=100, le=200000),
        item_id: str | None = None,
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            return store.query_series(
                samp_id,
                start,
                end,
                _split_csv_param(fields),
                _split_csv_param(row_keys),
                bucket_seconds,
                agg,
                max_points,
                max_raw_points,
                item_id=item_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/query/{samp_id}/export")
    def export_query(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        fields: str = "",
        row_keys: str = "",
        order: str = Query("asc"),
        item_id: str | None = None,
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            definition, columns, iterator = store.iter_query_rows(
                samp_id,
                start,
                end,
                _split_csv_param(fields),
                _split_csv_param(row_keys),
                order,
                item_id=item_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        def csv_stream():
            buf = io.StringIO()
            writer = csv.writer(buf)
            # 写入 UTF-8 BOM，确保 Excel 直接打开中文不乱码
            yield "\ufeff"
            writer.writerow(columns)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for row in iterator:
                writer.writerow([row.get(col, "") for col in columns])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        suffix = f"_{item_id}" if item_id else ""
        filename = f"{definition['samp_name']}{suffix}_samples.csv"
        headers = {
            "Content-Disposition": f"attachment; filename=\"samples.csv\"; filename*=UTF-8''{quote(filename)}"
        }
        return StreamingResponse(csv_stream(), media_type="text/csv; charset=utf-8", headers=headers)

    @app.get("/api/query/{samp_id}")
    def query(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        fields: str = "",
        row_keys: str = "",
        order: str = Query("asc"),
        limit: int = Query(1000, ge=1, le=10000),
        offset: int = Query(0, ge=0),
        item_id: str | None = None,
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            return store.query_samples(
                samp_id,
                start,
                end,
                _split_csv_param(fields),
                limit,
                offset,
                _split_csv_param(row_keys),
                order,
                item_id=item_id,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/source/databases")
    def source_databases(refresh: bool = False):
        return {
            "root_dir": store.root_dir.as_posix(),
            "directories": store.list_directories(refresh=refresh),
            "databases": store.list_source_databases(refresh=refresh),
            "default": store.to_relative_path(store.default_source_db),
        }

    @app.get("/api/source/tables/summary")
    def source_tables_summary(source_database: str | None = None):
        try:
            return store.source_tables_summary(source_database)
        except (ValueError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/source/tables")
    def source_tables(source_database: str | None = None):
        try:
            return {"tables": store.source_tables(source_database)}
        except (ValueError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/source/{source_table}/fields")
    def source_fields(source_table: str, source_database: str | None = None):
        try:
            return {"fields": store.source_fields(source_database, source_table)}
        except (ValueError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/source/inspect")
    def source_inspect(source_database: str, source_table: str, limit: int = 20, filter: str = ""):
        try:
            return store.inspect_source(source_database, source_table, limit, filter)
        except (ValueError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/source/rows")
    def source_rows(payload: SourceRowsIn):
        try:
            return {"written": store.replace_source_rows(payload.source_table, payload.rows, payload.source_database)}
        except (ValueError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/storage/databases")
    def storage_databases():
        items = store.list_result_databases()
        return {
            "root_dir": store.root_dir.as_posix(),
            "result_dir": store.to_relative_path(store.result_dir),
            "total": len(items),
            "orphans": sum(1 for item in items if item["is_orphan"]),
            "total_size_bytes": sum(item["size_bytes"] for item in items),
            "items": items,
        }

    @app.get("/api/storage/inspect")
    def inspect_storage_database(
        path: str = Query(...),
        table: str = "",
        limit: int = Query(20, ge=1, le=200),
    ):
        try:
            return store.inspect_result_database(path, table or None, limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except sqlite3.Error as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/storage/vacuum")
    def vacuum_storage_database(payload: StoragePathIn):
        try:
            return store.vacuum_result_database(payload.path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except sqlite3.Error as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/storage/databases/{filename:path}")
    def delete_storage_database(filename: str):
        try:
            removed = store.delete_result_database(filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not removed:
            raise HTTPException(404, "结果库文件不存在")
        return {"ok": True, "filename": filename}

    @app.get("/api/runtime/logs")
    def runtime_logs(limit: int = Query(100, ge=1, le=200), status: str = Query("all")):
        return {"logs": scheduler.get_logs(limit=limit, status=status)}

    @app.get("/api/status")
    def status():
        value = store.status()
        snaps = scheduler.snapshot_all_definitions(value["items"])
        for item in value["items"]:
            item.update(snaps.get(item["id"], {}))
        value["scheduler"] = scheduler.snapshot_summary()
        value["recent_logs"] = scheduler.get_logs(limit=50)
        return value

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "samp", **store.summary(), "scheduler": scheduler.snapshot_summary()}

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    def render_page(filename: str) -> str:
        html = (WEB_DIR / filename).read_text(encoding="utf-8")
        ver = int(max((p.stat().st_mtime for p in WEB_DIR.iterdir() if p.is_file()), default=0))
        return html.replace('.css"', f'.css?v={ver}"').replace('.js"', f'.js?v={ver}"')

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page():
        return render_page("dashboard.html")

    @app.get("/definitions", response_class=HTMLResponse, include_in_schema=False)
    def definitions_page():
        return render_page("definitions.html")

    @app.get("/runtime", response_class=HTMLResponse, include_in_schema=False)
    def runtime_page():
        return render_page("runtime.html")

    @app.get("/storage", include_in_schema=False)
    def storage_page():
        return RedirectResponse("/runtime#storage")

    @app.get("/query", response_class=HTMLResponse, include_in_schema=False)
    def query_page():
        return render_page("query.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    return app


app = create_app()
