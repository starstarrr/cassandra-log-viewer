# cassandra-log-viewer

A small lab for distributed database logging: a 3-node **Apache Cassandra 5.0**
cluster in Docker, a Python collector that ships every node's logs into
**OpenSearch**, and a **FastAPI** page to filter them by node, level, time and
text. A script breaks the cluster on purpose (node crash, lost quorum,
tombstones, schema changes) so there is something real to investigate. The
investigations are written up in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Architecture

```
 docker compose (one bridge network)                          host (macOS / Linux)
 ┌─────────────────────────────────────────────┐
 │ cass1 ◄──gossip──► cass2 ◄──gossip──► cass3 │   bind mounts
 │   │ /var/log/cassandra │          │         │ ──────────────►  logs/node1/system.log, debug.log
 └───┼────────────────────┼──────────┼─────────┘                  logs/node2/...
     └────────────────────┴──────────┘                            logs/node3/...
                                                                        │
                                                    collector (Python)  │ tail + parse
                                                    offsets in .collector-state.json
                                                                        │ POST /_bulk (NDJSON)
 ┌──────────────────────────────┐                                       ▼
 │ opensearch :9200 (127.0.0.1) │ ◄──── index template cassandra-logs-*, one index per day
 │ dashboards :5601 (optional)  │
 └──────────────────────────────┘
                ▲  _search (bool filter + simple_query_string, aggregations)
                │
        FastAPI api/app.py :8000  ──►  GET /  (filter page)
                                       GET /api/logs, /api/summary, /api/health
```

## What I measured

All from one run on 2026-11-10 on a MacBook Air (M2, 8 GB RAM), Docker in a
Colima VM with 4 CPUs and 6 GB. Raw outputs are in [`runs/`](runs/).

| What | Result |
|---|---|
| Cluster start, first log line to node3 "Startup complete" | 3 min 40 s (nodes join one at a time) |
| Memory per container after the tests | Cassandra 792–819 MiB of 1 GiB each, OpenSearch 954 MiB of 1 GiB |
| Crashed node (SIGKILL) marked DOWN by the others | about 19.5 s |
| Restarted node seen UP / hints replayed | 9 s / 23 s after `docker start` |
| Schema change reaching the other 2 nodes | 0.15–0.42 s for each of 5 changes |
| Events indexed | 6,340 from 3 nodes (system.log + DEBUG lines of debug.log), 4.5 MB index |
| Index count vs. events parsed straight from the files | equal (6,340 = 6,340) |
| Collector restarted with saved offsets | read only the 1 line written while it was stopped |
| Offsets deleted, every file re-read from byte 0 | index stayed at 6,340: no duplicates |
| Search through the API | `took_ms` 41 ms for a WARN + text filter (121 ms while the 100k-tombstone job was loading the VM) |
| Unit tests | 19 passing (parser, tailer, API), also on Python 3.11 |

## Requirements

- Docker with **about 6 GB RAM for the VM**. Each Cassandra node runs with a
  512 MB heap in a 1 GB container, OpenSearch with a 512 MB heap in 1 GB.
  Dashboards adds 768 MB, so it is off unless you ask for it.
  On an 8 GB Mac this works but the host swaps (see TROUBLESHOOTING #5).
  Colima: `colima start --cpu 4 --memory 6`.
- [uv](https://docs.astral.sh/uv/) for Python (3.11+).
- Disk for images: about 2.6 GB (Cassandra 568 MB, OpenSearch 2.0 GB), plus 2.3 GB for Dashboards.

## Run it

```bash
docker compose up -d              # ~4 min until cass3 is healthy
docker compose ps                 # wait for 3x "healthy"
scripts/trouble.sh status         # nodetool status: three "UN" lines

uv sync
uv run python -m collector.main   # leave running in its own terminal

uv run uvicorn api.app:app --port 8000   # another terminal
open http://localhost:8000
```

Optional: `docker compose --profile dashboards up -d` and open
http://localhost:5601 to explore the same `cassandra-logs-*` index in
OpenSearch Dashboards. (Dashboards was not running during the measured run
below; everything was done through the FastAPI page, curl and jq.)

Make trouble (each step prints a UTC timestamp to match against the logs):

```bash
scripts/trouble.sh setup          # keyspace lab, RF=3
scripts/trouble.sh schema         # CREATE + ALTER TABLE
scripts/trouble.sh kill 3         # crash a node
scripts/trouble.sh writes 300     # hints pile up for node3
scripts/trouble.sh quorum         # still works with 2 of 3
scripts/trouble.sh kill 2
scripts/trouble.sh quorum         # Unavailable: 1 of 3 alive
scripts/trouble.sh start 2; scripts/trouble.sh start 3
scripts/trouble.sh tombstones     # tombstone warning
scripts/trouble.sh overwhelm      # tombstone failure, ~8 min
```

Tests: `uv run pytest`. CI runs them on Python 3.11–3.13, plus
`shellcheck` and `docker compose config` (`.github/workflows/ci.yml`).

Clean up: `docker compose down -v` (removes data volumes), `rm -rf logs/node*/*.log`.

## Design notes

**Parsing.** Cassandra 5.0's logback pattern is
`%-5level [%thread] %date{"yyyy-MM-dd'T'HH:mm:ss,SSS", UTC} %F:%L - %msg%n`.
4.x used a space instead of the `T` and local time, so the regex accepts both.
`%F:%L` is the *source file* and line, not the logger name, so `logger` is
the file name without `.java` (`Gossiper.java` → `Gossiper`). Any line that
does not match the header (a stack trace) is attached to the event above it.

**Restartable tailing.** For each file the collector stores the inode and the
byte offset in `.collector-state.json` (written to a temp file, then renamed,
so a crash never leaves half a file). The offset is saved only **after**
OpenSearch accepted the bulk request: at-least-once delivery. Each document
id is `sha1(node, file, inode, offset)`, so if a batch is sent twice the
second write replaces the first instead of adding a duplicate. A new inode or a
file smaller than the offset means logback rotated the file, so reading starts
again at 0. Only complete lines are read, and the last event is held back
until the file stops growing for one poll, because its stack trace may still
be on the way.

**Not indexing everything twice.** `debug.log` repeats every INFO+ line of
`system.log`. The collector takes all of `system.log` but only DEBUG/TRACE
lines from `debug.log`.

**Index mapping** (`opensearch/index-template.json`). `level`, `node`,
`logger`, `thread` are `keyword` (exact filters and aggregations);
`message` is `text` for search with a `message.raw` keyword sub-field;
`dynamic: strict` so a parser bug fails loudly instead of creating random
fields. One index per day (`cassandra-logs-YYYY.MM.DD`) so retention is
"delete old indices". 1 shard, 0 replicas because it is one node.

**Queries** (`api/query.py`). Node, level and time go in the bool `filter`
clause (no scoring, cacheable); the text box uses `simple_query_string`, which
never errors on bad user syntax and supports `"phrases"`, `-exclude` and
`prefix*`. FastAPI validates every parameter (levels are an enum, `size`
1–1000, `since` before `until`), so bad input is a 422 and never reaches
OpenSearch.

**Security.** Lab only: the OpenSearch security plugin is disabled, so
ports are bound to `127.0.0.1`. A real setup would keep TLS and auth on.

## Known limits

- If logback rotates a file between two polls, the lines written just before
  rotation are lost (rotated files are zipped and not read).
- `nodetool listpendinghints` said "no pending hints" while a hints file was on
  disk (TROUBLESHOOTING #1). Not explained yet.
- OpenSearch health shows **yellow** because its built-in `top_queries-*`
  index asks for 1 replica on a 1-node cluster. The `cassandra-logs-*`
  indices are green.
- The collector runs on the host. In a real deployment it would be a sidecar
  per node (or Fluent Bit/Vector), not one process reading bind mounts.

## Layout

```
docker-compose.yml          3 Cassandra nodes, OpenSearch, optional Dashboards
collector/parser.py         log line -> LogEvent
collector/tailer.py         offsets, rotation, multi-line events
collector/indexer.py        _bulk NDJSON + index template
collector/main.py           poll loop
api/app.py, api/query.py    FastAPI endpoints + query builder
api/static/index.html       filter page
scripts/trouble.sh          break the cluster on purpose
tests/                      pytest; fixtures/real_lines.log copied from this cluster
runs/                       raw outputs of every run quoted in the docs
```
