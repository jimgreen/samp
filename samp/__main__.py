"""SAMP Web 服务命令行入口。"""

from __future__ import annotations

import argparse

import uvicorn

from .store import SQLITE_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SAMP 采样 Web 服务")
    parser.add_argument("--db", default=str(SQLITE_DIR / "samp_define.db"), help="采样定义 SQLite 数据库路径")
    parser.add_argument("--source-db", default=str(SQLITE_DIR / "source.db"), help="默认源 SQLite 数据库路径")
    parser.add_argument("--result-dir", default=str(SQLITE_DIR), help="年度结果库目录")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8877, help="监听端口")
    parser.add_argument("--workers", type=int, default=4, help="后台自动采样并发线程数")
    parser.add_argument("--no-scheduler", action="store_true", help="禁用 Web 进程内嵌调度器（搭配独立 samp.sampler 使用）")
    parser.add_argument("--no-align", action="store_true", help="禁用整周期时间对齐")
    args = parser.parse_args()
    from .app import create_app

    app = create_app(
        definition_db=args.db,
        source_db=args.source_db,
        result_dir=args.result_dir,
        enable_scheduler=not args.no_scheduler,
        workers=args.workers,
        align=not args.no_align,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
