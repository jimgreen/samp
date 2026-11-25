from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .scheduler import SamplingScheduler
from .store import SQLITE_DIR, Store

LOGGER = logging.getLogger("samp")
WEB_DIR = Path(__file__).with_name("web")


class DefinitionIn(BaseModel):
    name: str
    source_database: str = str(SQLITE_DIR / "source.db")
    source_table: str
    fields: list[str] = Field(min_length=1)
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


class SourceRowsIn(BaseModel):
    source_database: str = str(SQLITE_DIR / "source.db")
    source_table: str
    rows: list[dict[str, Any]]


def _split_csv_param(raw: str) -> list[str] | None:
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    return items or None


def create_app(
    definition_db: str | Path = SQLITE_DIR / "samp_define.db",
    source_db: str | Path = SQLITE_DIR / "source.db",
    result_dir: str | Path = SQLITE_DIR,
    enable_scheduler: bool = True,
    workers: int = 4,
    align: bool = True,
) -> FastAPI:
    """构建 SAMP FastAPI 应用，统一托管配置、调度、查询与存储治理接口。"""
    store = Store(definition_db, source_db, result_dir)
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
    def config():
        return {
            "default_source_db": str(Path(store.default_source_db)).replace("\\", "/"),
            "sqlite_dir": str(store.result_dir).replace("\\", "/"),
            "scheduler_enabled": bool(enable_scheduler),
            "align": bool(align),
            "workers": int(workers),
            "source_databases": store.list_source_databases(),
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

    @app.post("/api/definitions/batch-enable")
    def batch_enable_definitions(payload: BatchEnableIn):
        updated = store.set_enabled_batch(payload.ids, payload.enabled)
        return {"updated": updated, "enabled": payload.enabled}

    @app.post("/api/definitions/{samp_id}/enable")
    def enable_definition(samp_id: int, enabled: bool = True):
        value = store.set_enabled(samp_id, enabled)
        if not value:
            raise HTTPException(404, "采样定义不存在")
        return value

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

    @app.get("/api/definitions/{samp_id}/runtime")
    def definition_runtime(samp_id: int):
        try:
            item = store.runtime_status(samp_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        item.update(scheduler.snapshot_definition(samp_id, item["interval_seconds"], item["enabled"]))
        return item

    @app.get("/api/query/{samp_id}/keys")
    def query_keys(
        samp_id: int,
        start: int = Query(...),
        end: int = Query(...),
        limit: int = Query(500, ge=1, le=5000),
    ):
        if end < start:
            raise HTTPException(400, "end 不能早于 start")
        try:
            return store.query_keys(samp_id, start, end, limit)
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

        filename = f"{definition['samp_name']}_samples.csv"
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
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/source/databases")
    def source_databases():
        return {"databases": store.list_source_databases(), "default": str(Path(store.default_source_db)).replace("\\", "/")}

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
            "result_dir": str(store.result_dir).replace("\\", "/"),
            "total": len(items),
            "orphans": sum(1 for item in items if item["is_orphan"]),
            "total_size_bytes": sum(item["size_bytes"] for item in items),
            "items": items,
        }

    @app.delete("/api/storage/databases/{filename}")
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

    def render_page(filename: str) -> str:
        return (WEB_DIR / filename).read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/definitions")

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
