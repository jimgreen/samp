"""Standalone automatic sampler process.

Run with ``python -m samp.sampler`` when the Web process is not used. The
same Store and yearly result layout are used.
"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .store import SQLITE_DIR, Store

LOGGER = logging.getLogger("samp.sampler")

def run(db: str, source_db: str, result_dir: str, once: bool = False, workers: int = 4) -> None:
    store = Store(db, source_db, result_dir)
    last_attempt: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="samp") as pool:
        while True:
            cycle_started = time.monotonic()
            now = int(time.time())
            definitions = store.list_definitions()
            active_ids = {item["id"] for item in definitions}
            due = []
            for definition in definitions:
                interval = definition["interval_seconds"]
                if not definition["enabled"] or interval <= 0:
                    last_attempt.pop(definition["id"], None)
                    continue
                previous = last_attempt.get(definition["id"])
                if once or previous is None or now - previous >= interval:
                    last_attempt[definition["id"]] = now
                    due.append(definition["id"])
            for samp_id in set(last_attempt) - active_ids:
                last_attempt.pop(samp_id, None)
            futures = {pool.submit(store.run_sample, samp_id, now): samp_id for samp_id in due}
            for future in as_completed(futures):
                samp_id = futures[future]
                try:
                    future.result()
                except Exception as exc:  # keep one bad source from stopping other definitions
                    LOGGER.warning("采样失败 samp_id=%s: %s", samp_id, exc)
            if once:
                return
            time.sleep(max(0.05, 1.0 - (time.monotonic() - cycle_started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAMP automatic sampler")
    parser.add_argument("--db", default=str(SQLITE_DIR / "samp_define.db"))
    parser.add_argument("--source-db", default=str(SQLITE_DIR / "source.db"))
    parser.add_argument("--result-dir", default=str(SQLITE_DIR))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--workers", type=int, default=4, help="并行采样线程数")
    args = parser.parse_args()
    run(args.db, args.source_db, args.result_dir, args.once, args.workers)

