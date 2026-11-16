"""Standalone automatic sampler process.

Run with ``python -m samp.sampler --db data/samp_define.db`` when the Web
process is not used. The same Store and yearly result layout are used.
"""

from __future__ import annotations

import argparse
import time

from .store import Store


def run(db: str, source_db: str, result_dir: str, once: bool = False) -> None:
    store = Store(db, source_db, result_dir)
    while True:
        now = int(time.time())
        for definition in store.list_definitions():
            interval = definition["interval_seconds"]
            if definition["enabled"] and interval > 0 and (once or now % interval == 0):
                store.run_sample(definition["id"], now)
        if once:
            return
        time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAMP automatic sampler")
    parser.add_argument("--db", default="data/samp_define.db")
    parser.add_argument("--source-db", default="data/source.db")
    parser.add_argument("--result-dir", default="data/results")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(args.db, args.source_db, args.result_dir, args.once)

