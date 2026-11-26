# SAMP Web

旧版 EMS `sampdef`、`samp`/`autoloadrtdb` 与 `sampview` 的独立 Web/Python 实现。

- **采样定义 (`/definitions`)**：高密度主表管理 + 居中模态弹窗编辑器（含从源库一键批量建点向导），支持统一根目录相对路径、源库自动发现、自定义目标目录与目标库名（按年模板/前缀/单库）、同一采样点挂载多条采集项（一库多表，支持对已定义采样点逐条添加/修改/删除）、源表结构与过滤条件实时试跑预览、周期快捷档位、批量启停、JSON 批量导入/导出、复制及改名/改目标路径时同步迁移已有结果库。
- **自动采样与运维监控 (`/runtime`)**：统一 `SamplingScheduler` 支持整周期时间对齐、跨进程防双开调度租约与独立 `samp_runtime.db` 运行态/日志持久化（Web `--no-scheduler` 可直接监控独立 `samp.sampler` 进程）、按结果库路径分片并发写入、多表结果库元数据缓存及自定义子目录/孤儿库安全清理（别名 `/storage`）。
- **采样查询 (`/query`)**：交互式 Canvas 2D 多量纲曲线图（支持**同轴 / 归一化 / 分轨堆叠**、缩放、平移、框选放大、十字准星真实值读数、图例开关、`NULL` 断点与表格点击联动定位）、同点多采集项/多表切换、多 `row_key` 对比筛选、SQLite 库内聚合（`avg`/`raw`/`max`/`min`/`last`）与原始点上限保护、实时自动刷新与流式全量 CSV 导出。
- **界面规范**：左侧可拖拽调宽/吸附隐藏导航栏，内置全套 8 套官方主题预设（极简明亮 / 精密暗黑 / 便当盒网格 / 毛玻璃拟态 / 编辑杂志 / 科幻电光青 / 科幻赛博紫 / 科幻矩阵绿），支持简体中文（`zh-CN`）、英文（`en-US`）、俄文（`ru-RU`）实时多语言切换，零外部 CDN 依赖。

## 启动

```powershell
python -m samp --host 127.0.0.1 --port 8877
```

支持可选参数（默认以工作根目录 `--root-dir` / 环境变量 `SAMP_ROOT_DIR` 为基准使用相对路径）：

```powershell
python -m samp --root-dir D:\codex\workspace --db samp/samp_define.db --source-db source.db --result-dir samp --workers 4 --no-scheduler --no-align
```

浏览器打开 <http://127.0.0.1:8877/definitions>。三个独立工作台地址为：`/definitions`、`/runtime`（含 `/storage` 快捷跳转）、`/query`。

### 独立自动采样进程

若希望 Web 与自动采样解耦，可在启动 Web 时传入 `--no-scheduler`，并单独运行：

```powershell
python -m samp.sampler --root-dir D:\codex\workspace --workers 8
```

## 数据存储与 RTDB 互操作

默认工作根目录为 `D:\codex\workspace`，可通过环境变量 `SAMP_ROOT_DIR` 或 `--root-dir` 修改；业务路径均保存为相对根目录的 POSIX 相对路径（例如 `samp/samp_define.db`、`source.db`、`realtime/meter.db`、`samp/`），便于跨环境迁移，并兼容历史绝对路径与 `SAMP_SQLITE_DIR`；可选环境变量 `SAMP_ALLOWED_SOURCE_DIRS` 用于限制允许访问的源库根目录（分号或逗号分隔）。

- 定义库：`samp_define.db`（业务表 `samp_define`，含 `target_dir`、`target_name`、`items_json`）。
- 结果库：默认 `samp/samp_采样点名_年份.db`，也支持自定义相对根目录的 `target_dir` 与 `target_name`（前缀、含 `{year}` 模板或固定 `.db` 单库模式）；同一采样点可挂载多条采集项（一库多表：首项默认写库同名表，其余项各自写入独立业务表），按源表列类型 `INTEGER`/`REAL`/`TEXT` 保真建列，内置 `(sample_time, row_key)` 主键及表专属 `(row_key, sample_time)` 索引。
- 建表与加列在同一事务中同步更新 `rtdb_schema_*` 结构定义（覆盖库内全部业务表），启动时自动为历史库补登记，支持原生 SQLite 布局的 RTDB 只读打开。

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
