# Troubleshooting log

Five investigations done on this lab cluster on 2026-11-10 (Cassandra 5.0.9,
3 nodes, RF=3, single DC, Docker on a MacBook with 8 GB RAM).
All times are UTC. Every number below comes from the command outputs saved in
[`runs/`](runs/). Nothing here is estimated.

Node and IP map (from `docker inspect`, saved in `runs/ips.txt`):

| node  | container | IP         |
|-------|-----------|------------|
| node1 | cass1     | 172.20.0.3 |
| node2 | cass2     | 172.20.0.4 |
| node3 | cass3     | 172.20.0.5 |

Queries are shown in two forms: the viewer API (`localhost:8000`) and the raw
log files with `grep`/`awk`/`jq`.

---

## 1. A node crashes: how long until the cluster notices, and does it heal?

**What I did** (`runs/02-node-down.txt`, `runs/04-recovery.txt`)

```bash
scripts/trouble.sh writes 200      # 19:25:18, all nodes up
scripts/trouble.sh kill 3          # 19:25:19, SIGKILL: no clean shutdown, no gossip "goodbye"
scripts/trouble.sh writes 300      # 19:25:48, at CONSISTENCY ONE while node3 is down
scripts/trouble.sh start 2; scripts/trouble.sh start 3   # 19:26:42
```

**Query**

```bash
# Gossip timeline, all nodes, sorted by time
grep -H "is now \(DOWN\|UP\)" logs/*/system.log \
  | sed -E 's#logs/(node[0-9])/system.log:INFO +\[[^]]+\] ([^ ]+) .*/([0-9.]+):7000 is now (DOWN|UP)#\2 \1 sees \3 \4#' \
  | sort
```

```
2026-11-10T19:25:38,639 node2 sees 172.20.0.5 DOWN
2026-11-10T19:25:38,890 node1 sees 172.20.0.5 DOWN
2026-11-10T19:26:21,012 node1 sees 172.20.0.4 DOWN
2026-11-10T19:26:51,177 node1 sees 172.20.0.5 UP
2026-11-10T19:26:51,467 node2 sees 172.20.0.5 UP
...
```

Viewer: `http://localhost:8000/?logger=Gossiper&since=2026-11-10T19:25:00`
and `http://localhost:8000/?q="hinted handoff"`.

**What it showed**

- Killed at 19:25:19, marked DOWN at 19:25:38.6 (node2) and 19:25:38.9 (node1):
  about **19.5 s** for the failure detector (phi_convict_threshold=8) to convict
  a node that died without warning. The second kill (node2 at 19:26:02, DOWN
  at 19:26:21.0) took about **19 s** too. The DEBUG line just before it in
  `debug.log` is `Gossiper Convicting /172.20.0.4:7000 with status NORMAL`.
- While node3 was down, node1 (the coordinator) stored hints for it: a 22,734
  byte file `cc7efc43-...-2.hints` in `/var/lib/cassandra/hints/`, named after
  node3's host ID. `nodetool listpendinghints` printed "This node does not have
  any pending hints" at the same moment. I did not find out why; the file on
  disk is the evidence I trust here.
- After `docker start`, both nodes were seen UP at 19:26:51 (9 s), and node1
  logged `Finished hinted handoff of file ... to endpoint /172.20.0.5:7000` and
  deleted the files at 19:27:05 (23 s after start).
- Check that node3 really got the 300 writes it missed: a traced
  `SELECT count(*)` at CONSISTENCY ONE run on node3 returned 300, and every
  `Read N live rows` step in the trace ran on 172.20.0.5 with no requests to
  other nodes, so node3 had all rows locally.
- A second hint file was made for node2 (created 19:26:24, after node2 was
  killed). The only writes in that window were the trace rows from my
  `TRACING ON` query, and `system_traces` uses RF=2, so those are most likely
  what was hinted.

---

## 2. QUORUM read fails: the server logs it at INFO, not WARN

**What I did** (`runs/03-quorum-fail.txt`): with node2 and node3 both down,
`scripts/trouble.sh quorum` (a `SELECT` at CONSISTENCY QUORUM via node1).

Client error:

```
Unavailable ... "Cannot achieve consistency level QUORUM"
info={'consistency': 'QUORUM', 'required_replicas': 2, 'alive_replicas': 1}
```

With one node down (investigation 1) the same QUORUM read worked: the trace
showed node1 reading locally plus from node2 only, 112 ms in total.

**Query**

```bash
curl -s "localhost:8000/api/summary?q=UnavailableException" | jq -c '{total,by_node,by_level}'
# {"total":1,"by_node":{"node1":1},"by_level":{"INFO":1}}
```

**What it showed**

node1 logged one line:
`INFO NoSpamLogger - UnavailableException "Cannot achieve consistency level QUORUM" while executing SELECT * FROM lab.events WHERE id = 1 ...`

Lessons:
- It is **INFO**, through `NoSpamLogger` (rate limited). A "WARN and above"
  filter would hide it, which is why the viewer has both "min level" and a
  free-text search.
- It is only on the **coordinator**. The down replicas logged nothing, of course.

---

## 3. Tombstones: a warning at 1,500 and an aborted read at 100,001

**What I did** (`runs/05-tombstones.txt`, `runs/06-overwhelm.txt`)

```bash
scripts/trouble.sh tombstones 1500   # insert 1500 rows in partition p1, delete each, read p1 at ALL
scripts/trouble.sh overwhelm         # 100,500 row deletes in partition p2, read p2 at ONE
```

The overwhelm run took 8 min 8 s to send through cqlsh (batches of 500).

**Query**

Viewer: `http://localhost:8000/?min_level=WARN&q=tombstone*`

```bash
grep -h -E "^(WARN|ERROR)" logs/*/system.log | awk '{print $1, $4}' \
  | sed -E "s/\.java:[0-9]+//" | sort | uniq -c | sort -rn
```

**What it showed**

- 1,500 tombstones: all three replicas logged
  `WARN ReadCommand - Read 0 live rows and 1500 tombstone cells for query SELECT * FROM lab.tomb WHERE pk = 'p1' ... (see tombstone_warn_threshold)`
  (threshold is 1000). One per node because CONSISTENCY ALL reads all three.
  cqlsh showed the same text as a client warning.
- 100,501 tombstones: the client got `ReadFailure ... READ_TOO_MANY_TOMBSTONES from /172.20.0.4:7000, READ_TOO_MANY_TOMBSTONES from /172.20.0.5:7000`.
  node3 and node2 each logged
  `ERROR NoSpamLogger - Scanned over 100001 tombstones during query ... query aborted`,
  131 ms apart. Even at CONSISTENCY ONE, two replicas were asked: the
  coordinator tried a second one after the first failed.
- The coordinator (node1) logged nothing at WARN or above. Its only trace is
  `DEBUG ReadCallback - Failed; received 0 of 1 responses` in `debug.log`.
  So the root cause is on the replicas, and you only find it if logs from all
  nodes are in one place.
- No stack trace was printed for this error, so the parser's multi-line
  (stack trace) handling is covered by a unit test, not by this run.

---

## 4. Schema change: did every node get it?

**What I did** (`runs/01-schema.txt`): `scripts/trouble.sh setup` (keyspace +
2 tables) and `scripts/trouble.sh schema` (CREATE TABLE then ALTER TABLE ADD).

**Query**

```bash
curl -s 'localhost:9200/cassandra-logs-*/_search' -H 'content-type: application/json' -d '{
  "size": 50, "sort": [{"@timestamp": "asc"}],
  "query": {"bool": {"filter": [
    {"term":  {"logger": "DefaultSchemaUpdateHandler"}},
    {"term":  {"level": "INFO"}},
    {"range": {"@timestamp": {"gte": "2026-11-10T19:24:29Z"}}}]}}}' \
| jq -r '.hits.hits[]._source | "\(.["@timestamp"][11:23]) \(.node) \(.message | capture("SchemaTransformationResult\\{(?<v>[^ ]+ --> [^,}]+)").v)"'
```

```
19:24:29.757 node1 d03783d7-... --> 6513073c-...
19:24:29.903 node3 d03783d7-... --> 6513073c-...
19:24:29.975 node2 d03783d7-... --> 6513073c-...
...
19:24:35.401 node1 36c85ca4-... --> d63c0e83-...
19:24:35.785 node3 36c85ca4-... --> d63c0e83-...
19:24:35.825 node2 36c85ca4-... --> d63c0e83-...
```

**What it showed**

- 5 schema changes, each applied first on node1 (where cqlsh ran) and on the
  other two nodes within 0.15–0.42 s.
- `nodetool describecluster` then listed one schema version,
  `d63c0e83-...`, for all three IPs, matching the last line above.
- At first it looked like node2 and node3 logged every change twice. They did
  not: the second line is a separate DEBUG message (`Schema updated: ...`)
  from `debug.log`. Filtering by level made that clear.

---

## 5. "Not marking nodes down due to local pause of 67206958991ns"

**What I saw**: right after startup, node1 logged a WARN that its own JVM had
not run its gossip task for **67.2 s**.

**Query**

```bash
grep -H "local pause" logs/*/system.log \
  | sed -E 's#logs/(node[0-9])/.* ([0-9T:,-]{23}) .*pause of ([0-9]+)ns.*#\2 \1 \3#' \
  | awk '{printf "%s %s %.1f s\n", $1, $2, $3/1e9}' | sort
```

```
2026-11-10T19:21:40,244 node1 67.2 s
2026-11-10T19:21:41,214 node2 5.3 s
2026-11-10T19:23:03,997 node3 6.3 s
2026-11-10T19:26:51,466 node3 5.0 s
2026-11-10T19:26:52,444 node2 6.0 s
```

**What it showed**

- Not garbage collection: the longest GC pause in node1's `gc.log` for the
  whole run was 224 ms.
- Not clock skew: host, Docker VM and container clocks matched to the second.
- At the time, macOS reported 11 GB of swap in use on this 8 GB machine, with
  6 GB given to the Docker VM. The likely cause is the host pausing or
  swapping the VM, so the JVM threads simply did not run. I did not prove
  this directly.
- The failure detector did the right thing: it ignores its own missed
  heartbeats during a local pause instead of marking every other node DOWN.
- The other pauses happened while a node was starting (19:21, 19:23, 19:26:51).
  Every startup used a full CPU core.

---

## grep / jq cheat sheet

```bash
# WARN/ERROR count per node
grep -c -E "^(WARN|ERROR)" logs/*/system.log

# Which classes produce the warnings
grep -h -E "^(WARN|ERROR)" logs/*/system.log | awk '{print $1, $4}' \
  | sed -E "s/\.java:[0-9]+//" | sort | uniq -c | sort -rn

# Follow one node live, only problems
tail -F logs/node2/system.log | grep --line-buffered -E "^(WARN|ERROR)"

# Lines around an error (stack traces follow the header line)
grep -n -A 20 "^ERROR" logs/node3/system.log

# Same idea through the API: WARN+ grouped by logger
curl -s "localhost:8000/api/logs?min_level=WARN&size=1000" \
  | jq -r '.events | group_by(.logger) | map("\(length) \(.[0].logger)") | .[]' | sort -rn

# Hint replay events as TSV
curl -s 'localhost:8000/api/logs?q=%22hinted%20handoff%22' \
  | jq -r '.events[] | [.["@timestamp"], .node, .message[:90]] | @tsv'
```
