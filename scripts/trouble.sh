#!/usr/bin/env bash
# Cause real, explainable events on the lab cluster so the logs have
# something worth finding. Every step prints a UTC timestamp first, so you can
# line it up with the log entries afterwards.
#
#   scripts/trouble.sh setup            keyspace lab (RF=3) + tables
#   scripts/trouble.sh schema           schema change: create + alter a table
#   scripts/trouble.sh kill 3           crash node 3 (SIGKILL, no clean shutdown)
#   scripts/trouble.sh stop 3           clean shutdown of node 3
#   scripts/trouble.sh start 3          bring node 3 back
#   scripts/trouble.sh writes [n]       n writes at ONE (hints pile up if a node is down)
#   scripts/trouble.sh quorum           read at QUORUM, with tracing
#   scripts/trouble.sh tombstones [n]   insert n rows in one partition, delete them, read it
#   scripts/trouble.sh overwhelm [n]    n tombstones in one partition, past the failure threshold
#   scripts/trouble.sh status           nodetool status from node 1
set -euo pipefail

COORD=${COORD:-cass1}   # node that runs cqlsh (the coordinator)

ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
say()  { echo "[$(ts)] $*"; }
cql()  { docker exec -i "$COORD" cqlsh "$@"; }

cmd=${1:-help}; shift || true
case "$cmd" in
  setup)
    say "create keyspace lab (NetworkTopologyStrategy dc1:3) and tables"
    cql <<'CQL'
CREATE KEYSPACE IF NOT EXISTS lab
  WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};
CREATE TABLE IF NOT EXISTS lab.events (id int PRIMARY KEY, body text);
CREATE TABLE IF NOT EXISTS lab.tomb (pk text, ck int, v text, PRIMARY KEY (pk, ck));
CQL
    ;;
  schema)
    name="shots_$(date +%s)"
    say "schema change: CREATE TABLE lab.$name, then ALTER TABLE add column"
    cql -e "CREATE TABLE lab.$name (shot_id uuid PRIMARY KEY, frame int);
            ALTER TABLE lab.$name ADD artist text;"
    ;;
  kill|stop|start)
    n=${1:?node number 1-3}
    say "docker $cmd cass$n"
    docker "$cmd" "cass$n" >/dev/null
    ;;
  writes)
    n=${1:-200}
    say "$n writes to lab.events at CONSISTENCY ONE via $COORD"
    { echo "CONSISTENCY ONE;"
      for i in $(seq 1 "$n"); do echo "INSERT INTO lab.events (id, body) VALUES ($i, 'frame $i');"; done
    } | cql >/dev/null
    say "done"
    ;;
  quorum)
    say "SELECT at CONSISTENCY QUORUM via $COORD (needs 2 of 3 replicas)"
    cql -e "CONSISTENCY QUORUM; TRACING ON; SELECT count(*) FROM lab.events WHERE id IN (1,2,3);" || true
    ;;
  tombstones)
    n=${1:-1500}
    say "insert $n rows into partition pk='p1', then delete each row"
    { for i in $(seq 1 "$n"); do echo "INSERT INTO lab.tomb (pk, ck, v) VALUES ('p1', $i, 'x');"; done
      for i in $(seq 1 "$n"); do echo "DELETE FROM lab.tomb WHERE pk = 'p1' AND ck = $i;"; done
    } | cql >/dev/null
    say "read the partition (should scan $n tombstones, warn threshold is 1000)"
    cql -e "CONSISTENCY ALL; SELECT * FROM lab.tomb WHERE pk = 'p1';"
    ;;
  overwhelm)
    # Past tombstone_failure_threshold (100000) the read is aborted.
    # Deleting rows that never existed still writes tombstones. Deletes are
    # sent in single-partition unlogged batches of 500 to keep this fast.
    n=${1:-100500}
    say "write $n row tombstones into partition pk='p2' (batches of 500)"
    { for ((i = 1; i <= n; i += 500)); do
        echo "BEGIN UNLOGGED BATCH"
        for ((j = i; j < i + 500 && j <= n; j++)); do echo "DELETE FROM lab.tomb WHERE pk = 'p2' AND ck = $j;"; done
        echo "APPLY BATCH;"
      done
    } | cql >/dev/null
    say "read the partition (expect TombstoneOverwhelmingException)"
    cql -e "CONSISTENCY ONE; SELECT * FROM lab.tomb WHERE pk = 'p2';" || true
    ;;
  status)
    docker exec "$COORD" nodetool status
    ;;
  *)
    sed -n '2,18p' "$0"
    ;;
esac
