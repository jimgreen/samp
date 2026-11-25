# SAMP Web

旧版 EMS `sampdef`、`samp`/`autoloadrtdb` 与 `sampview` 的独立 Web/Python 实现。

- **采样定义 (`/definitions`)**：高密度主表管理 + 右侧抽屉编辑器，支持源库发现下拉、源表结构与过滤条件实时试跑预览、周期快捷档位、批量启停、JSON 批量导入/导出、复制及改名时同步迁移历史年度结果库。
- **自动采样与运维监控 (`/runtime`)**：统一 `SamplingScheduler` 支持整周期时间对齐、跨进程防双开调度租约与独立 `samp_runtime.db` 运行态/日志持久化（Web `--no-scheduler` 可直接监控独立 `samp.sampler` 进程）、按结果库路径分片并发写入、结果库元数据缓存及孤儿库安全清理（别名 `/storage`）。
- **采样查询 (`/query`)**：交互式 Canvas 2D 多量纲曲线图（支持**同轴 / 归一化 / 分轨堆叠**、缩放、平移、框选放大、十字准星真实值读数、图例开关、`NULL` 断点与表格点击联动定位）、多 `row_key` 对比筛选、SQLite 库内聚合（`avg`/`raw`/`max`/`min`/`last`）与原始点上限保护、实时自动刷新与流式全量 CSV 导出。
- **界面规范**：内置极简明亮（`minimalist-light`）与精密暗黑（`precision-dark`）双主题切换，支持简体中文（`zh-CN`）、英文（`en-US`）、俄文（`ru-RU`）实时多语言切换，零外部 CDN 依赖。

## 启动

```powershell
python -m samp --host 127.0.0.1 --port 8877
```

支持可选参数：

```powershell
python -m samp --db D:\codex\sqldb\realtime\samp\samp_define.db --source-db D:\codex\sqldb\realtime\samp\source.db --result-dir D:\codex\sqldb\realtime\samp --workers 4 --no-scheduler --no-align
```

浏览器打开 <http://127.0.0.1:8877/definitions>。三个独立工作台地址为：`/definitions`、`/runtime`（含 `/storage` 快捷跳转）、`/query`。

### 独立自动采样进程

若希望 Web 与自动采样解耦，可在启动 Web 时传入 `--no-scheduler`，并单独运行：

```powershell
python -m samp.sampler --workers 8
```

## 数据存储与 RTDB 互操作

默认 SQLite 根目录为 `D:\codex\sqldb\realtime\samp`，可通过环境变量 `SAMP_SQLITE_DIR` 修改；可选环境变量 `SAMP_ALLOWED_SOURCE_DIRS` 用于限制允许访问的源库根目录（分号或逗号分隔）。

- 定义库：`samp_define.db`（业务表 `samp_define`）。
- 年度结果库：`samp_采样点名_年份.db`（单业务表同名，按源表列类型 `INTEGER`/`REAL`/`TEXT` 保真建列，内置 `(sample_time, row_key)` 主键及 `(row_key, sample_time)` 索引）。
- 建表与加列在同一事务中同步更新 `rtdb_schema_*` 结构定义，启动时自动为历史库补登记，支持原生 SQLite 布局的 RTDB 只读打开。

## 过滤条件语法

采样定义的 `filter` 支持白名单安全比较与集合过滤，子句可用 `and` 或 `,` 连接：

```text
status=1 and voltage>=200 and code in (10, 20) and label != 'off'
```

## 测试

```powershell
python -m pytest -q
```

API 文档见 `/docs`，完整功能说明见 [`docs/samp功能说明.html`](docs/samp功能说明.html)。
