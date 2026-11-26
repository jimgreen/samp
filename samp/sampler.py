"""独立自动采样后台进程。

在不启用 Web 内嵌调度器时通过 ``python -m samp.sampler`` 启动，
复用统一的 Store 与 SamplingScheduler。
"""

from __future__ import annotations

import argparse
import logging
import time

from .scheduler import SamplingScheduler
from .store import DEFAULT_DEFINITION_DB, DEFAULT_RESULT_DIR, DEFAULT_SOURCE_DB, ROOT_DIR, Store

LOGGER = logging.getLogger("samp.sampler")


def run(
    db: str,
    source_db: str,
    result_dir: str,
    once: bool = False,
    workers: int = 4,
    align: bool = True,
    root_dir: str | None = None,
) -> None:
    store = Store(db, source_db, result_dir, root_dir=root_dir)
    scheduler = SamplingScheduler(store, workers=workers, align=align, mode="standalone")
    if once:
        scheduler.tick(force_once=True)
        return
    scheduler.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        LOGGER.info("收到退出信号，正在停止独立采样进程…")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAMP 独立自动采样进程")
    parser.add_argument("--root-dir", default=str(ROOT_DIR), help="工作根目录（相对路径解析基准）")
    parser.add_argument("--db", default=str(DEFAULT_DEFINITION_DB), help="采样定义库路径（默认相对根目录）")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB), help="默认源库路径（默认相对根目录）")
    parser.add_argument("--result-dir", default=str(DEFAULT_RESULT_DIR), help="年度结果库存储目录（默认相对根目录）")
    parser.add_argument("--once", action="store_true", help="仅执行一次并退出")
    parser.add_argument("--workers", type=int, default=4, help="并行采样线程数")
    parser.add_argument("--no-align", action="store_true", help="禁用整周期时间对齐")
    args = parser.parse_args()
    run(args.db, args.source_db, args.result_dir, args.once, args.workers, align=not args.no_align, root_dir=args.root_dir)
