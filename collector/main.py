"""Collector: tail every node's Cassandra logs and index them into OpenSearch.

    uv run python -m collector.main --logs-dir logs --opensearch http://localhost:9200

Layout expected under --logs-dir:  <node>/system.log  and  <node>/debug.log
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from pathlib import Path

import httpx

from .indexer import OpenSearchIndexer
from .tailer import FileTailer, StateStore

log = logging.getLogger("collector")

# system.log has INFO and above; debug.log repeats those and adds DEBUG.
# Taking only DEBUG from debug.log avoids indexing every INFO+ line twice.
FILES = {"system.log": None, "debug.log": {"DEBUG", "TRACE"}}


def discover(logs_dir: Path, state: StateStore, tailers: dict[str, FileTailer]) -> None:
    for node_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
        for name, keep in FILES.items():
            path = node_dir / name
            key = str(path)
            if path.exists() and key not in tailers:
                tailers[key] = FileTailer(path, node_dir.name, state.get(key), keep)
                log.info("tailing %s (from offset %d)", path, tailers[key].state.offset)


def run(logs_dir: Path, opensearch: str, state_path: Path, interval: float, once: bool) -> int:
    state = StateStore(state_path)
    indexer = OpenSearchIndexer(opensearch)
    indexer.ensure_template()
    tailers: dict[str, FileTailer] = {}
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    total = 0
    idle_rounds = 0
    while not stop:
        discover(logs_dir, state, tailers)
        sent = 0
        for tailer in tailers.values():
            batch = tailer.read()
            try:
                sent += indexer.bulk(batch.events, batch.inode)
            except (httpx.HTTPError, RuntimeError) as e:
                # Offset not committed, so this batch is retried next round.
                log.warning("bulk failed for %s: %s", tailer.path, e)
                continue
            tailer.commit(batch)
        state.save()
        total += sent
        if sent:
            log.info("indexed %d events (total %d)", sent, total)
        idle_rounds = idle_rounds + 1 if sent == 0 else 0
        # --once: stop after two quiet rounds, so held-back last events get flushed.
        if once and idle_rounds >= 2:
            break
        time.sleep(interval)
    log.info("stopped, %d events indexed this run", total)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs-dir", type=Path, default=Path("logs"))
    ap.add_argument("--opensearch", default="http://localhost:9200")
    ap.add_argument("--state", type=Path, default=Path(".collector-state.json"))
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    ap.add_argument("--once", action="store_true", help="catch up, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # skip one line per request
    run(args.logs_dir, args.opensearch, args.state, args.interval, args.once)


if __name__ == "__main__":
    main()
