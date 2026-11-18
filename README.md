# SAMP Web

这是旧版 EMS `sampdef`、`samp`/`autoloadrtdb` 和 `sampview` 的独立 Web/Python 实现。

- **采样定义**：创建、编辑、启停采样规则。定义数据库 `D:\codex\sqldb\realtime\samp\samp_define.db` 只有一张 `samp_define` 表；用户只需要选择采样点名、源表、字段和周期。
- **自动采样**：后台线程按周期读取源数据，写入 `D:\codex\sqldb\realtime\samp\samp_采样点名_年份.db`。每个结果库只有一张同名表，例如 `samp_电表A_2026.db` 内的 `samp_电表A_2026`。
- **采样查询**：按采样规则和时间范围查询，返回表格数据并可绘制字段趋势图。

## 启动

```powershell
python -m samp
```

浏览器打开 <http://127.0.0.1:8877>。也可以使用 `uvicorn samp.app:app --host 127.0.0.1 --port 8877 --reload`。

默认 SQLite 根目录为 `D:\codex\sqldb\realtime\samp`，也可以通过环境变量 `SAMP_SQLITE_DIR` 修改。采样定义库为该目录下的 `samp_define.db`，默认源库为 `source.db`，结果库直接写入同一目录。源数据表由采样定义中的 `source_database` 和 `source_table` 指定，采样程序通过 SQLite 元数据动态读取，不依赖固定表名。示例导入接口：

```sql
CREATE TABLE meter (row_key TEXT PRIMARY KEY, voltage REAL, current REAL);
INSERT INTO meter(row_key, voltage, current) VALUES ('A01', 220.1, 3.2);
```

也可以调用 `POST /api/source/rows`，同时传入 `source_database`、`source_table` 和行数据。

查询页面内置最近 1 小时、今天、最近 24 小时快捷范围，支持字段全选/清空、定义搜索，结果按 500 行分页显示并支持当前页 CSV 导出。运行监控支持一次执行全部已启用定义。API 查询支持 `limit`（1-10000）和 `offset` 分页参数，并返回 `total_rows`。

独立采样进程使用按上次执行时间调度的并行采样器，默认 4 个工作线程，可用 `python -m samp.sampler --workers 8` 调整并发数。

详情见 `samp/schema.sql` 和 API 文档 `/docs`。

完整功能说明见 [`docs/samp功能说明.html`](docs/samp功能说明.html)。

