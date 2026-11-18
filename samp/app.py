from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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


class SourceRowsIn(BaseModel):
    source_database: str = str(SQLITE_DIR / "source.db")
    source_table: str
    rows: list[dict[str, Any]]


def create_app(definition_db: str | Path = SQLITE_DIR / "samp_define.db", source_db: str | Path = SQLITE_DIR / "source.db", result_dir: str | Path = SQLITE_DIR) -> FastAPI:
    store = Store(definition_db, source_db, result_dir)
    stop = asyncio.Event()
    last_attempt: dict[int, int] = {}
    running: set[int] = set()
    sample_locks: dict[int, asyncio.Lock] = {}
    scheduler_state: dict[str, Any] = {"last_cycle_at": None, "last_success_at": None, "last_error": None, "sampled": 0, "failed": 0}

    def sample_lock(samp_id: int) -> asyncio.Lock:
        return sample_locks.setdefault(samp_id, asyncio.Lock())

    async def execute_sample(samp_id: int, sampled_at: int | None = None) -> dict[str, Any]:
        async with sample_lock(samp_id):
            return await asyncio.to_thread(store.run_sample, samp_id, sampled_at)

    async def sampler_loop() -> None:
        while not stop.is_set():
            now = int(time.time())
            scheduler_state["last_cycle_at"] = now
            definitions = store.list_definitions()
            active_ids = set()
            due = []
            for item in definitions:
                active_ids.add(item["id"])
                if not item.get("enabled") or item["interval_seconds"] <= 0:
                    last_attempt.pop(item["id"], None)
                    continue
                if item["id"] in running:
                    continue
                previous = last_attempt.get(item["id"])
                if previous is None or now - previous >= item["interval_seconds"]:
                    last_attempt[item["id"]] = now
                    due.append(item["id"])
            for samp_id in set(last_attempt) - active_ids:
                last_attempt.pop(samp_id, None)
            if due:
                running.update(due)
                try:
                    results = await asyncio.gather(
                        *(execute_sample(samp_id, now) for samp_id in due),
                        return_exceptions=True,
                    )
                    for samp_id, result in zip(due, results):
                        if isinstance(result, Exception):
                            scheduler_state["failed"] += 1
                            scheduler_state["last_error"] = f"samp_id={samp_id}: {result}"
                            LOGGER.warning("自动采样失败 samp_id=%s: %s", samp_id, result)
                        else:
                            scheduler_state["sampled"] += 1
                            scheduler_state["last_success_at"] = result["sampled_at"]
                finally:
                    running.difference_update(due)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(sampler_loop())
        yield
        stop.set()
        await task

    app = FastAPI(title="SAMP 采样服务", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/definitions")
    def definitions(): return store.list_definitions()

    @app.post("/api/definitions")
    def create_definition(payload: DefinitionIn):
        try: return store.upsert_definition(payload.model_dump())
        except ValueError as exc: raise HTTPException(400, str(exc))
        except sqlite3.IntegrityError as exc: raise HTTPException(409, f"采样点名已存在: {exc}")

    @app.put("/api/definitions/{samp_id}")
    def update_definition(samp_id: int, payload: DefinitionIn):
        if not store.get_definition(samp_id): raise HTTPException(404, "采样定义不存在")
        try: return store.upsert_definition(payload.model_dump(), samp_id)
        except ValueError as exc: raise HTTPException(400, str(exc))
        except sqlite3.IntegrityError as exc: raise HTTPException(409, f"采样点名已存在: {exc}")

    @app.delete("/api/definitions/{samp_id}")
    def delete_definition(samp_id: int):
        if not store.delete_definition(samp_id): raise HTTPException(404, "采样定义不存在")
        return {"ok": True}

    @app.post("/api/definitions/{samp_id}/enable")
    def enable_definition(samp_id: int, enabled: bool = True):
        value = store.set_enabled(samp_id, enabled)
        if not value: raise HTTPException(404, "采样定义不存在")
        return value

    @app.post("/api/sample/all")
    async def sample_all():
        definitions = [item for item in store.list_definitions() if item.get("enabled")]
        results = await asyncio.gather(*(execute_sample(item["id"]) for item in definitions), return_exceptions=True)
        completed, failed = [], []
        for item, result in zip(definitions, results):
            if isinstance(result, Exception):
                failed.append({"id": item["id"], "name": item["samp_name"], "error": str(result)})
            else:
                completed.append(result)
        return {"requested": len(definitions), "completed": completed, "failed": failed}

    @app.post("/api/sample/{samp_id}")
    async def sample_now(samp_id: int):
        try: return await execute_sample(samp_id)
        except KeyError as exc: raise HTTPException(404, str(exc))
        except ValueError as exc: raise HTTPException(400, str(exc))

    @app.get("/api/definitions/{samp_id}/runtime")
    def definition_runtime(samp_id: int):
        try: return store.runtime_status(samp_id)
        except KeyError as exc: raise HTTPException(404, str(exc))

    @app.get("/api/query/{samp_id}")
    def query(samp_id: int, start: int = Query(...), end: int = Query(...), fields: str = "", limit: int = Query(1000, ge=1, le=10000), offset: int = Query(0, ge=0)):
        if end < start: raise HTTPException(400, "end 不能早于 start")
        try: return store.query_samples(samp_id, start, end, [x for x in fields.split(",") if x] or None, limit, offset)
        except KeyError as exc: raise HTTPException(404, str(exc))
        except ValueError as exc: raise HTTPException(400, str(exc))

    @app.get("/api/source/tables")
    def source_tables(source_database: str | None = None):
        try: return {"tables": store.source_tables(source_database)}
        except sqlite3.Error as exc: raise HTTPException(400, str(exc))

    @app.get("/api/source/{source_table}/fields")
    def source_fields(source_table: str, source_database: str | None = None):
        try: return {"fields": store.source_fields(source_database, source_table)}
        except (ValueError, sqlite3.Error) as exc: raise HTTPException(400, str(exc))

    @app.get("/api/source/inspect")
    def source_inspect(source_database: str, source_table: str, limit: int = 20):
        try: return store.inspect_source(source_database, source_table, limit)
        except (ValueError, sqlite3.Error) as exc: raise HTTPException(400, str(exc))

    @app.post("/api/source/rows")
    def source_rows(payload: SourceRowsIn):
        try: return {"written": store.replace_source_rows(payload.source_table, payload.rows, payload.source_database)}
        except (ValueError, sqlite3.Error) as exc: raise HTTPException(400, str(exc))

    @app.get("/api/status")
    def status():
        value = store.status()
        value["scheduler"] = dict(scheduler_state)
        return value

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "samp", **store.summary(), "scheduler": scheduler_state}

    def render_page(filename: str) -> str:
        return (WEB_DIR / filename).read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    def home(): return RedirectResponse("/definitions")

    @app.get("/definitions", response_class=HTMLResponse, include_in_schema=False)
    def definitions_page(): return render_page("definitions.html")

    @app.get("/runtime", response_class=HTMLResponse, include_in_schema=False)
    def runtime_page(): return render_page("runtime.html")

    @app.get("/query", response_class=HTMLResponse, include_in_schema=False)
    def query_page(): return render_page("query.html")

    @app.get("/favicon.ico")
    def favicon(): return Response(status_code=204)

    return app


HTML = """<!doctype html>
<html lang='zh-CN'>
<head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>SAMP 采样服务</title>
<style>
:root{font-family:Inter,system-ui,'Microsoft YaHei',sans-serif;color:#19243a;background:#f3f6fb}*{box-sizing:border-box}body{margin:0}header{background:linear-gradient(120deg,#162846,#2458a8);color:#fff;padding:24px max(22px,calc((100% - 1280px)/2)) 20px}header h1{margin:0 0 6px;font-size:25px}header p{margin:0;color:#d9e6ff}.nav{display:flex;gap:8px;margin-top:20px;max-width:1280px;margin-left:auto;margin-right:auto}.nav button{border:0;background:#ffffff22;color:#fff;padding:10px 18px;border-radius:8px;cursor:pointer}.nav button.active{background:#fff;color:#1654a3}.page{max-width:1280px;margin:24px auto;padding:0 22px}.view{display:none}.view.active{display:block}.grid{display:grid;grid-template-columns:minmax(330px,420px) 1fr;gap:18px}.card{background:#fff;border:1px solid #e1e7f0;border-radius:12px;padding:18px;box-shadow:0 4px 16px #263b5d0b}.toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.toolbar h2,.card h2{margin:0 0 14px;font-size:18px}.toolbar h2{margin:0}.field{display:flex;flex-direction:column;gap:6px;margin:11px 0}.field label{font-size:13px;color:#526079;font-weight:600}.field input,.field select,.field textarea{width:100%;border:1px solid #cbd5e4;border-radius:7px;padding:9px 10px;font:inherit;background:#fff}.field textarea{resize:vertical;min-height:56px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.actions.compact{margin-top:0;margin-bottom:6px}.btn{border:1px solid #c4cfdf;background:#fff;color:#29405f;border-radius:7px;padding:8px 12px;cursor:pointer}.btn.primary{background:#1769e0;color:#fff;border-color:#1769e0}.btn.danger{color:#b42318;border-color:#f0b8b2}.btn.small{font-size:12px;padding:5px 8px}.btn:disabled{opacity:.55;cursor:not-allowed}.quick-search{border:1px solid #cbd5e4;border-radius:7px;padding:8px 10px;width:190px;margin-right:6px;font:inherit}.muted{color:#6c7890;font-size:13px}.status-row{display:flex;gap:12px;flex-wrap:wrap}.metric{background:#f5f8fd;border-radius:9px;padding:13px 16px;min-width:150px}.metric b{display:block;font-size:23px;color:#135fbb}.metric span{font-size:12px;color:#6b7890}.table-wrap{overflow:auto}.data-table{width:100%;border-collapse:collapse;font-size:13px}.data-table th,.data-table td{padding:10px 9px;border-bottom:1px solid #e8edf4;text-align:left;vertical-align:top}.data-table th{background:#f7f9fc;color:#526079;font-weight:650;position:sticky;top:0}.data-table tr:hover td{background:#f8fbff}.badge{display:inline-flex;border-radius:99px;padding:3px 8px;font-size:12px}.badge.on{background:#e2f7eb;color:#137a42}.badge.off{background:#edf0f5;color:#667085}.field-list{display:flex;flex-wrap:wrap;gap:8px;padding:4px 0}.field-check{display:flex;gap:5px;align-items:center;background:#f5f8fc;padding:6px 8px;border-radius:6px;font-size:13px}.empty{padding:28px;text-align:center;color:#78849a}.preview{max-height:320px}.notice{border-radius:8px;padding:10px 12px;margin:10px 0;font-size:13px}.notice.info{background:#edf5ff;color:#17549a}.notice.error{background:#fff1f0;color:#a3271a}.toast{position:fixed;right:22px;bottom:22px;z-index:5;background:#17345f;color:#fff;padding:12px 16px;border-radius:8px;box-shadow:0 6px 20px #13294a44;display:none}.toast.show{display:block}.code{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}.query-options{display:flex;gap:12px;align-items:end;flex-wrap:wrap}.query-options .field{min-width:190px;flex:1}.chart{height:230px;border:1px dashed #c9d5e5;border-radius:8px;margin-top:16px;padding:12px}.bar{height:10px;background:#2b83e6;border-radius:5px;margin:7px 0}.bar-label{font-size:12px;color:#53627b}@media(max-width:900px){.grid{grid-template-columns:1fr}.page{padding:0 12px}.toolbar>div{display:flex;flex-wrap:wrap;gap:6px}.quick-search{width:160px;margin-right:0}}
</style></head>
<body>
<header><h1>SAMP 采样服务</h1><p>采样定义、自动采样运行监控与历史结果查询</p><div class='nav'><button class='active' data-view='definitions'>采样定义</button><button data-view='runtime'>运行监控</button><button data-view='query'>采样查询</button></div></header>
<main class='page'>
<section id='view-definitions' class='view active'>
  <div class='card overview-card'><div id='overview-metrics' class='status-row'><div class='metric'><b>--</b><span>采样定义</span></div><div class='metric'><b>--</b><span>已启用</span></div><div class='metric'><b>--</b><span>结果库</span></div><div class='metric'><b>--</b><span>调度状态</span></div></div></div>
  <div class='toolbar'><h2>采样定义</h2><div><input id='definition-search' class='quick-search' placeholder='搜索采样点或源表'><select id='definition-status-filter' class='quick-filter'><option value='all'>全部状态</option><option value='enabled'>仅启用</option><option value='disabled'>仅停用</option></select><button id='new-definition' class='btn primary'>新建定义</button><button id='refresh-definitions' class='btn'>刷新</button></div></div>
  <div class='grid'>
    <form id='definition-form' class='card'>
      <h2 id='form-title'>新建采样定义</h2><input type='hidden' id='definition-id'>
      <div class='field'><label>采样点名</label><input id='name' required placeholder='例如：电表A'></div>
      <div class='field'><label>源数据库</label><input id='source-database' required value='D:/codex/sqldb/realtime/samp/source.db' placeholder='SQLite 数据库路径'></div>
      <div class='field'><label>源表</label><div style='display:flex;gap:6px'><input id='source-table' required list='source-table-options' placeholder='选择或输入真实表名'><button type='button' id='load-tables' class='btn'>读取表</button></div><datalist id='source-table-options'></datalist></div>
      <div class='actions'><button type='button' id='inspect-source' class='btn'>读取字段和预览</button></div>
      <div class='field'><label>采样字段 <span class='muted'>（可多选）</span></label><div class='actions compact'><button type='button' id='definition-fields-all' class='btn small'>全选</button><button type='button' id='definition-fields-none' class='btn small'>清空</button></div><div id='definition-fields' class='field-list'><span class='muted'>先读取源表字段</span></div></div>
      <div class='field'><label>采样周期（秒，0 表示仅手动）</label><input id='interval' type='number' min='0' value='60' required></div>
      <div class='field'><label>过滤条件（可选，例：status=1 and quality=0）</label><input id='filter' placeholder='留空表示采集全部行'></div>
      <label class='field-check'><input id='enabled' type='checkbox' checked>启用自动采样</label>
      <div class='actions'><button class='btn primary' type='submit'>保存定义</button><button class='btn' type='button' id='cancel-edit'>清空</button></div>
      <div id='form-message' class='notice info'>保存前请读取源表并选择字段。</div>
    </form>
    <div class='card'><h2>源表预览</h2><div id='source-meta' class='muted'>尚未读取源表。</div><div id='source-preview' class='table-wrap preview'></div></div>
  </div>
  <div class='card' style='margin-top:18px'><div class='toolbar'><h2>已定义采样点</h2><span id='definition-count' class='muted'></span></div><div id='definition-table' class='table-wrap'></div></div>
</section>
<section id='view-runtime' class='view'>
  <div class='toolbar'><h2>自动采样运行监控</h2><div><button id='sample-all' class='btn primary'>立即执行已启用</button><button id='refresh-runtime' class='btn'>刷新状态</button></div></div>
  <div id='runtime-metrics' class='status-row'></div>
  <div id='scheduler-info' class='notice info'>正在读取调度器状态…</div>
  <div class='card' style='margin-top:18px'><div id='runtime-table' class='table-wrap'></div></div>
</section>
<section id='view-query' class='view'>
  <div class='toolbar'><h2>采样结果查询</h2><button id='refresh-query' class='btn'>刷新定义</button></div>
  <div class='card'><div class='query-options'><div class='field'><label>采样点</label><select id='query-definition'></select></div><div class='field'><label>开始时间</label><input id='query-start' type='datetime-local'></div><div class='field'><label>结束时间</label><input id='query-end' type='datetime-local'></div><div class='field page-size'><label>每页行数</label><select id='query-limit'><option value='100'>100</option><option value='500' selected>500</option><option value='1000'>1000</option><option value='5000'>5000</option></select></div><button id='run-query' class='btn primary'>查询</button><button id='export-query' class='btn'>导出当前页</button></div><div class='actions'><button id='query-last-hour' class='btn small'>最近 1 小时</button><button id='query-today' class='btn small'>今天</button><button id='query-last-day' class='btn small'>最近 24 小时</button></div><div class='field'><label>查询字段 <span class='muted'>（可多选）</span></label><div class='actions compact'><button type='button' id='query-fields-all' class='btn small'>全选</button><button type='button' id='query-fields-none' class='btn small'>清空</button></div><div id='query-fields' class='field-list'></div></div><div id='query-message' class='muted'>请选择采样点和时间范围。</div></div>
  <div class='card' style='margin-top:18px'><div id='query-table' class='table-wrap'></div><div class='actions'><button id='query-prev' class='btn small'>上一页</button><span id='query-page-info' class='muted'>未查询</span><button id='query-next' class='btn small'>下一页</button></div><div id='query-chart' class='chart'></div></div>
</section>
</main><div id='toast' class='toast'></div>
<script>
const state={definitions:[],source:null,lastQuery:null,queryPage:0,queryLimit:500,queryRequest:null,formEditing:false,definitionFilter:''};
const $=id=>document.getElementById(id);const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const response=await fetch(url,options);let body={};try{body=await response.json()}catch{}if(!response.ok)throw new Error(body.detail||`请求失败（${response.status}）`);return body}
function toast(message,error=false){const node=$('toast');node.textContent=message;node.style.background=error?'#a3271a':'#17345f';node.classList.add('show');setTimeout(()=>node.classList.remove('show'),3200)}
function localDate(value){const date=new Date(value*1000);const pad=n=>String(n).padStart(2,'0');return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`}
function timestamp(value){return Math.floor(new Date(value).getTime()/1000)}
function setDefaultQueryTimes(){const now=Math.floor(Date.now()/1000);$('query-start').value=localDate(now-3600);$('query-end').value=localDate(now)}
function setQueryRange(kind){const now=Math.floor(Date.now()/1000),start=new Date();if(kind==='hour'){$('query-start').value=localDate(now-3600);$('query-end').value=localDate(now);return}if(kind==='day'){$('query-start').value=localDate(now-86400);$('query-end').value=localDate(now);return}start.setHours(0,0,0,0);$('query-start').value=localDate(Math.floor(start.getTime()/1000));$('query-end').value=localDate(now)}
function renderFieldChecks(container,fields,checked){fields=fields.filter(field=>field!=='row_key');container.innerHTML=fields.length?fields.map(field=>`<label class='field-check'><input type='checkbox' value='${esc(field)}' ${checked.includes(field)?'checked':''}>${esc(field)}</label>`).join(''):'<span class="muted">没有可用字段</span>'}
function checkedFields(container){return [...container.querySelectorAll('input[type=checkbox]:checked')].map(input=>input.value)}
function setAllFields(container,checked){container.querySelectorAll('input[type=checkbox]').forEach(input=>{input.checked=checked});container.dispatchEvent(new Event('change'))}
function renderPreview(data){$('source-meta').innerHTML=`<span class='code'>${esc(data.source_database)} : ${esc(data.source_table)}</span> · ${data.fields.length} 个字段 · 显示 ${data.rows.length} 行`;if(!data.rows.length){$('source-preview').innerHTML='<div class="empty">源表没有数据</div>';return}const cols=data.fields;$('source-preview').innerHTML=`<table class='data-table'><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${data.rows.map(row=>`<tr>${cols.map(c=>`<td>${esc(row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`}
async function inspectSource(){const db=$('source-database').value.trim(),table=$('source-table').value.trim();if(!db||!table){toast('请先填写源数据库和源表',true);return}try{const data=await api(`/api/source/inspect?source_database=${encodeURIComponent(db)}&source_table=${encodeURIComponent(table)}`);state.source=data;renderFieldChecks($('definition-fields'),data.fields,checkedFields($('definition-fields')));renderPreview(data);$('form-message').textContent='请选择要采样的字段后保存。';$('form-message').className='notice info'}catch(error){$('source-preview').innerHTML='';$('source-meta').textContent=error.message;toast(error.message,true)}}
async function loadTables(){const db=$('source-database').value.trim();if(!db){toast('请先填写源数据库',true);return}try{const data=await api(`/api/source/tables?source_database=${encodeURIComponent(db)}`);$('source-table-options').innerHTML=data.tables.map(t=>`<option value='${esc(t)}'>`).join('');if(data.tables.length){$('source-table').value=data.tables[0];await inspectSource()}toast(`读取到 ${data.tables.length} 张表`)}catch(error){toast(error.message,true)}}
function resetForm(){ $('definition-id').value='';$('form-title').textContent='新建采样定义';$('name').value='';$('source-database').value='D:/codex/sqldb/realtime/samp/source.db';$('source-table').value='';$('definition-fields').innerHTML='<span class="muted">先读取源表字段</span>';$('source-meta').textContent='尚未读取源表。';$('source-preview').innerHTML='';$('interval').value='60';$('filter').value='';$('enabled').checked=true;$('form-message').textContent='保存前请读取源表并选择字段。'}
function editDefinition(def){$('definition-id').value=def.id;$('form-title').textContent=`编辑采样定义：${def.samp_name}`;$('name').value=def.samp_name;$('source-database').value=def.source_database;$('source-table').value=def.source_table;$('interval').value=def.interval_seconds;$('filter').value=def.filter||'';$('enabled').checked=!!def.enabled;inspectSource().then(()=>renderFieldChecks($('definition-fields'),state.source?.fields||[],def.fields));window.scrollTo({top:0,behavior:'smooth'})}
async function copyDefinition(def){if(!def)return;resetForm();$('name').value=`${def.samp_name}_副本`;$('source-database').value=def.source_database;$('source-table').value=def.source_table;$('interval').value=def.interval_seconds;$('filter').value=def.filter||'';$('enabled').checked=false;await inspectSource();renderFieldChecks($('definition-fields'),state.source?.fields||[],def.fields);$('form-message').textContent='已复制定义内容，请修改采样点名后保存。';$('form-message').className='notice info';window.scrollTo({top:0,behavior:'smooth'})}
function renderDefinitions(){const keyword=state.definitionFilter.trim().toLowerCase();const visible=state.definitions.filter(d=>!keyword||[d.samp_name,d.source_table,d.source_database].some(value=>String(value||'').toLowerCase().includes(keyword)));$('definition-count').textContent=keyword?`显示 ${visible.length} / ${state.definitions.length} 个采样点`:`共 ${state.definitions.length} 个采样点`;if(!visible.length){$('definition-table').innerHTML=`<div class="empty">${keyword?'没有匹配的采样定义。':'暂无采样定义，请先新建。'}</div>`;return}$('definition-table').innerHTML=`<table class='data-table'><thead><tr><th>采样点</th><th>源数据库 / 源表</th><th>字段</th><th>周期</th><th>状态</th><th>操作</th></tr></thead><tbody>${visible.map(d=>`<tr><td><b>${esc(d.samp_name)}</b></td><td><div class='code'>${esc(d.source_database)}</div><div>${esc(d.source_table)}</div></td><td>${d.fields.map(esc).join(', ')}</td><td>${d.interval_seconds?d.interval_seconds+' 秒':'手动'}</td><td><span class='badge ${d.enabled?'on':'off'}'>${d.enabled?'运行中':'已停用'}</span></td><td><div class='actions'><button class='btn small' data-action='edit' data-id='${d.id}'>编辑</button><button class='btn small' data-action='sample' data-id='${d.id}'>立即采样</button><button class='btn small' data-action='toggle' data-id='${d.id}'>${d.enabled?'停用':'启用'}</button><button class='btn small danger' data-action='delete' data-id='${d.id}'>删除</button></div></td></tr>`).join('')}</tbody></table>`}
function renderDefinitions(){const keyword=state.definitionFilter.trim().toLowerCase(),status=$('definition-status-filter').value;const visible=state.definitions.filter(d=>(status==='all'||(status==='enabled'?d.enabled:!d.enabled))&&(!keyword||[d.samp_name,d.source_table,d.source_database].some(value=>String(value||'').toLowerCase().includes(keyword))));$('definition-count').textContent=keyword||status!=='all'?`显示 ${visible.length} / ${state.definitions.length} 个采样点`:`共 ${state.definitions.length} 个采样点`;if(!visible.length){$('definition-table').innerHTML=`<div class="empty">${keyword||status!=='all'?'没有匹配的采样定义。':'暂无采样定义，请先新建。'}</div>`;return}$('definition-table').innerHTML=`<table class='data-table'><thead><tr><th>采样点</th><th>源数据库 / 源表</th><th>字段</th><th>周期</th><th>状态</th><th>操作</th></tr></thead><tbody>${visible.map(d=>`<tr><td><b>${esc(d.samp_name)}</b></td><td><div class='code'>${esc(d.source_database)}</div><div>${esc(d.source_table)}</div></td><td>${d.fields.map(esc).join(', ')}</td><td>${d.interval_seconds?d.interval_seconds+' 秒':'手动'}</td><td><span class='badge ${d.enabled?'on':'off'}'>${d.enabled?'运行中':'已停用'}</span></td><td><div class='actions'><button class='btn small' data-action='edit' data-id='${d.id}'>编辑</button><button class='btn small' data-action='copy' data-id='${d.id}'>复制</button><button class='btn small' data-action='sample' data-id='${d.id}'>立即采样</button><button class='btn small' data-action='toggle' data-id='${d.id}'>${d.enabled?'停用':'启用'}</button><button class='btn small danger' data-action='delete' data-id='${d.id}'>删除</button></div></td></tr>`).join('')}</tbody></table>`}
function renderOverview(data){const scheduler=data.scheduler||{};$('overview-metrics').innerHTML=`<div class='metric'><b>${data.definitions||0}</b><span>采样定义</span></div><div class='metric'><b>${data.enabled||0}</b><span>已启用</span></div><div class='metric'><b>${data.result_databases||0}</b><span>结果库</span></div><div class='metric'><b>${scheduler.failed?'异常':'正常'}</b><span>调度状态</span></div>`}
function renderSchedulerInfo(scheduler){const cycle=scheduler.last_cycle_at?new Date(scheduler.last_cycle_at*1000).toLocaleString():'尚未运行';const success=scheduler.last_success_at?new Date(scheduler.last_success_at*1000).toLocaleString():'暂无成功记录';$('scheduler-info').textContent=`后台调度正常 · 最近检查：${cycle} · 最近成功：${success} · 成功 ${scheduler.sampled||0} 次 · 失败 ${scheduler.failed||0} 次${scheduler.last_error?` · 最近错误：${scheduler.last_error}`:''}`}
async function loadOverview(){try{const data=await api('/api/health');renderOverview(data);renderSchedulerInfo(data.scheduler||{})}catch(error){$('overview-metrics').innerHTML=`<div class='notice error'>总览读取失败：${esc(error.message)}</div>`;$('scheduler-info').textContent='无法读取调度器状态：'+error.message;$('scheduler-info').className='notice error'}}
async function loadDefinitions(){try{state.definitions=await api('/api/definitions');renderDefinitions();renderQueryDefinitions();renderRuntime();}catch(error){toast(error.message,true)}}
async function loadSchedulerStatus(){try{const data=await api('/api/health'),scheduler=data.scheduler||{};const cycle=scheduler.last_cycle_at?new Date(scheduler.last_cycle_at*1000).toLocaleString():'尚未运行';const success=scheduler.last_success_at?new Date(scheduler.last_success_at*1000).toLocaleString():'暂无成功记录';$('scheduler-info').textContent=`后台调度正常 · 最近检查：${cycle} · 最近成功：${success} · 成功 ${scheduler.sampled||0} 次 · 失败 ${scheduler.failed||0} 次${scheduler.last_error?` · 最近错误：${scheduler.last_error}`:''}`}catch(error){$('scheduler-info').textContent='无法读取调度器状态：'+error.message;$('scheduler-info').className='notice error'}}
function renderQueryDefinitions(){const current=$('query-definition').value;const currentFields=checkedFields($('query-fields'));$('query-definition').innerHTML=state.definitions.map(d=>`<option value='${d.id}'>${esc(d.samp_name)}</option>`).join('');const selected=state.definitions.find(d=>String(d.id)===String(current))||state.definitions[0];if(selected){$('query-definition').value=selected.id;const checked=String(selected.id)===String(current)&&currentFields.length?currentFields:selected.fields;renderFieldChecks($('query-fields'),selected.fields,checked)}else{$('query-fields').innerHTML='<span class="muted">暂无采样定义</span>'}}
function renderRuntime(){const enabled=state.definitions.filter(d=>d.enabled).length;$('runtime-metrics').innerHTML=`<div class='metric'><b>${state.definitions.length}</b><span>采样定义</span></div><div class='metric'><b>${enabled}</b><span>已启用</span></div><div class='metric'><b>${state.definitions.filter(d=>d.interval_seconds>0&&d.enabled).length}</b><span>自动采样</span></div>`;if(!state.definitions.length){$('runtime-table').innerHTML='<div class="empty">暂无运行对象</div>';return}$('runtime-table').innerHTML=`<table class='data-table'><thead><tr><th>采样点</th><th>状态</th><th>周期</th><th>源表</th><th>结果年份</th><th>最近结果</th><th>操作</th></tr></thead><tbody>${state.definitions.map(d=>`<tr id='runtime-${d.id}'><td>${esc(d.samp_name)}</td><td>${d.enabled?'<span class="badge on">启用</span>':'<span class="badge off">停用</span>'}</td><td>${d.interval_seconds?d.interval_seconds+' 秒':'手动'}</td><td>${esc(d.source_table)}</td><td class='runtime-years'>读取中</td><td class='runtime-last'>读取中</td><td><button class='btn small' data-action='runtime-sample' data-id='${d.id}'>立即采样</button></td></tr>`).join('')}</tbody></table>`;state.definitions.forEach(loadRuntimeRow)}
async function loadRuntimeRow(def){try{const data=await api(`/api/definitions/${def.id}/runtime`),row=$(`runtime-${def.id}`);if(!row)return;row.querySelector('.runtime-years').textContent=data.result_years.length?data.result_years.join(', '):'暂无';row.querySelector('.runtime-last').textContent=data.last_result_at?new Date(data.last_result_at*1000).toLocaleString():'暂无'}catch(error){}}
async function saveDefinition(event){event.preventDefault();const fields=checkedFields($('definition-fields'));if(!fields.length){toast('至少选择一个采样字段',true);return}const payload={name:$('name').value.trim(),source_database:$('source-database').value.trim(),source_table:$('source-table').value.trim(),fields,interval_seconds:Number($('interval').value),filter:$('filter').value.trim(),enabled:$('enabled').checked};const id=$('definition-id').value;try{await api(id?`/api/definitions/${id}`:'/api/definitions',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});toast(id?'采样定义已更新':'采样定义已创建');resetForm();await loadDefinitions()}catch(error){toast(error.message,true)}}
async function deleteDefinition(id){const def=state.definitions.find(item=>item.id===id);if(!def||!confirm(`确认删除采样点“${def.samp_name}”吗？`))return;try{await api(`/api/definitions/${id}`,{method:'DELETE'});toast('采样定义已删除');await loadDefinitions()}catch(error){toast(error.message,true)}}
async function toggleDefinition(id){const def=state.definitions.find(item=>item.id===id);if(!def)return;try{await api(`/api/definitions/${id}/enable?enabled=${!def.enabled}`,{method:'POST'});toast(def.enabled?'采样已停用':'采样已启用');await loadDefinitions()}catch(error){toast(error.message,true)}}
async function sampleNow(id){try{const result=await api(`/api/sample/${id}`,{method:'POST'});toast(`采样完成，写入 ${result.rows_written} 行`);await loadDefinitions()}catch(error){toast(error.message,true)}}
async function sampleAll(){const button=$('sample-all');button.disabled=true;try{const result=await api('/api/sample/all',{method:'POST'});const message=`批量采样完成：成功 ${result.completed.length} 个，失败 ${result.failed.length} 个`;toast(message,result.failed.length>0);if(result.failed.length)console.warn(result.failed);await loadDefinitions()}catch(error){toast(error.message,true)}finally{button.disabled=false}}
function renderQueryTable(result){const cols=result.columns||[];if(!result.rows.length){$('query-table').innerHTML='<div class="empty">时间范围内没有采样数据。</div>';$('query-chart').innerHTML='<div class="empty">暂无曲线数据</div>';renderQueryPager(result);return}$('query-table').innerHTML=`<table class='data-table'><thead><tr>${cols.map(c=>`<th>${esc(c==='sample_time'?'采样时间':c)}</th>`).join('')}</tr></thead><tbody>${result.rows.map(row=>`<tr>${cols.map(c=>`<td>${esc(c==='sample_time'?new Date(row[c]*1000).toLocaleString():row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;const fields=cols.filter(c=>!['sample_time','row_key'].includes(c));const maxima=fields.map(field=>field+': '+Math.max(...result.rows.map(row=>Number(row[field])||0))).join('；');$('query-chart').textContent='字段最大值：'+(maxima||'没有数值字段可绘制。');renderQueryPager(result)}
function renderQueryTable(result){const cols=result.columns||[];if(!result.rows.length){$('query-table').innerHTML='<div class="empty">时间范围内没有采样数据。</div>';$('query-chart').innerHTML='<div class="empty">暂无统计摘要</div>';renderQueryPager(result);return}$('query-table').innerHTML=`<table class='data-table'><thead><tr>${cols.map(c=>`<th>${esc(c==='sample_time'?'采样时间':c)}</th>`).join('')}</tr></thead><tbody>${result.rows.map(row=>`<tr>${cols.map(c=>`<td>${esc(c==='sample_time'?new Date(row[c]*1000).toLocaleString():row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;const fields=cols.filter(c=>!['sample_time','row_key'].includes(c));const cards=fields.map(field=>{const values=result.rows.map(row=>row[field]).filter(value=>value!==null&&value!==undefined&&value!=='');const numbers=values.map(Number).filter(Number.isFinite);if(numbers.length===values.length&&numbers.length){return `<div class='metric'><b>${esc(Math.max(...numbers))}</b><span>${esc(field)} · 最大</span><div class='muted'>最小 ${esc(Math.min(...numbers))} · 最新 ${esc(values[values.length-1])}</div></div>`}return `<div class='metric'><b>${esc(values[values.length-1]??'暂无')}</b><span>${esc(field)} · 最新</span><div class='muted'>共 ${values.length} 个非空值</div></div>`}).join('');$('query-chart').innerHTML=`<div class='muted' style='margin-bottom:8px'>当前页字段统计（${result.rows.length} 行）</div><div class='status-row'>${cards||'<span class="muted">没有可统计字段。</span>'}</div>`;renderQueryPager(result)}
function renderQueryPager(result){const total=Number(result.total_rows||0),offset=Number(result.offset||0),limit=Number(result.limit||state.queryLimit),page=total?Math.floor(offset/limit)+1:0,pages=total?Math.ceil(total/limit):0;$('query-page-info').textContent=total?`第 ${page} / ${pages} 页（${offset+1}-${Math.min(offset+(result.rows||[]).length,total)}，共 ${total} 行）`:'暂无数据';$('query-prev').disabled=page<=1;$('query-next').disabled=!total||page>=pages}
function queryRequestFromForm(){const id=$('query-definition').value;if(!id){toast('请先创建采样定义',true);return null}const start=timestamp($('query-start').value),end=timestamp($('query-end').value);if(!Number.isFinite(start)||!Number.isFinite(end)||end<start){toast('请检查查询时间范围',true);return null}const fields=checkedFields($('query-fields'));if(!fields.length){toast('至少选择一个查询字段',true);return null}return {id,start,end,fields}}
async function runQuery(page=0,useSavedRequest=false){const request=useSavedRequest?state.queryRequest:queryRequestFromForm();if(!request)return;state.queryPage=Math.max(0,page);if(!useSavedRequest)state.queryRequest=request;try{const result=await api(`/api/query/${request.id}?start=${request.start}&end=${request.end}&fields=${encodeURIComponent(request.fields.join(','))}&limit=${state.queryLimit}&offset=${state.queryPage*state.queryLimit}`);if(result.total_rows&&result.offset>=result.total_rows){const lastPage=Math.max(0,Math.ceil(result.total_rows/state.queryLimit)-1);if(lastPage!==state.queryPage){return runQuery(lastPage,true)}}state.lastQuery=result;renderQueryTable(result);$('query-message').textContent=`查询到 ${result.total_rows} 行，当前显示 ${result.rows.length} 行；结果表：${result.tables.join(', ')||'暂无'}`}catch(error){toast(error.message,true)}}
function exportQuery(){const result=state.lastQuery;if(!result?.rows?.length){toast('没有可导出的查询数据',true);return}const csv=[result.columns,...result.rows.map(row=>result.columns.map(col=>row[col]))].map(row=>row.map(value=>`"${String(value??'').replaceAll('"','""')}"`).join(',')).join('\\n');const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([`\\ufeff${csv}`],{type:'text/csv;charset=utf-8'}));link.download=`${result.definition.samp_name}_采样结果.csv`;link.click();URL.revokeObjectURL(link.href)}
document.querySelectorAll('.nav button').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.nav button').forEach(item=>item.classList.remove('active'));document.querySelectorAll('.view').forEach(item=>item.classList.remove('active'));button.classList.add('active');$(`view-${button.dataset.view}`).classList.add('active');if(button.dataset.view==='runtime')renderRuntime()}));
$('definition-form').addEventListener('submit',saveDefinition);$('inspect-source').addEventListener('click',inspectSource);$('load-tables').addEventListener('click',loadTables);$('new-definition').addEventListener('click',resetForm);$('cancel-edit').addEventListener('click',resetForm);$('refresh-definitions').addEventListener('click',loadDefinitions);$('refresh-runtime').addEventListener('click',loadDefinitions);$('refresh-query').addEventListener('click',loadDefinitions);$('run-query').addEventListener('click',runQuery);$('export-query').addEventListener('click',exportQuery);$('sample-all').addEventListener('click',sampleAll);$('definition-search').addEventListener('input',event=>{state.definitionFilter=event.target.value;renderDefinitions()});$('definition-fields-all').addEventListener('click',()=>setAllFields($('definition-fields'),true));$('definition-fields-none').addEventListener('click',()=>setAllFields($('definition-fields'),false));$('query-fields-all').addEventListener('click',()=>setAllFields($('query-fields'),true));$('query-fields-none').addEventListener('click',()=>setAllFields($('query-fields'),false));$('definition-table').addEventListener('click',event=>{const button=event.target.closest('[data-action]');if(!button)return;const id=Number(button.dataset.id);if(button.dataset.action==='edit')editDefinition(state.definitions.find(item=>item.id===id));if(button.dataset.action==='delete')deleteDefinition(id);if(button.dataset.action==='toggle')toggleDefinition(id);if(button.dataset.action==='sample')sampleNow(id)});$('runtime-table').addEventListener('click',event=>{const button=event.target.closest('[data-action="runtime-sample"]');if(button)sampleNow(Number(button.dataset.id))});
$('query-prev').addEventListener('click',()=>{if(state.queryPage>0)runQuery(state.queryPage-1,true)});$('query-next').addEventListener('click',()=>{if(state.lastQuery&&state.lastQuery.offset+state.lastQuery.rows.length<state.lastQuery.total_rows)runQuery(state.queryPage+1,true)});$('query-last-hour').addEventListener('click',()=>{setQueryRange('hour');runQuery()});$('query-last-day').addEventListener('click',()=>{setQueryRange('day');runQuery()});$('query-today').addEventListener('click',()=>{setQueryRange('today');runQuery()});$('query-definition').addEventListener('change',()=>{state.queryPage=0;state.queryRequest=null;const def=state.definitions.find(item=>String(item.id)===$('query-definition').value);if(def)renderFieldChecks($('query-fields'),def.fields,def.fields)});$('query-limit').addEventListener('change',()=>{state.queryLimit=Number($('query-limit').value)||500;state.queryPage=0;if(state.queryRequest)runQuery()});$('query-fields').addEventListener('change',()=>{state.queryPage=0});
$('definition-status-filter').addEventListener('change',renderDefinitions);
$('definition-table').addEventListener('click',event=>{const button=event.target.closest('[data-action="copy"]');if(button)copyDefinition(state.definitions.find(item=>item.id===Number(button.dataset.id)))});
setDefaultQueryTimes();loadDefinitions();loadOverview();setInterval(()=>{loadDefinitions();loadOverview()},15000);
</script></body></html>"""

app = create_app()
