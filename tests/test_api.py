import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from api.app import app
from api.query import build_search, levels_at_or_above
from collector.indexer import bulk_body
from collector.parser import parse_lines


def test_levels_at_or_above():
    assert levels_at_or_above("warn") == ["WARN", "ERROR"]


def test_build_search_filters():
    body = build_search(
        size=10, nodes=["node2"], min_level="WARN", q="tombstone",
        since=datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc),
    )
    f = body["query"]["bool"]["filter"]
    assert {"terms": {"node": ["node2"]}} in f
    assert {"terms": {"level": ["WARN", "ERROR"]}} in f
    assert {"range": {"@timestamp": {"gte": "2026-11-10T19:00:00+00:00"}}} in f
    assert body["query"]["bool"]["must"][0]["simple_query_string"]["query"] == "tombstone"
    assert body["sort"][0] == {"@timestamp": "desc"}


def test_no_filters_is_match_all():
    q = build_search()["query"]["bool"]
    assert q["must"] == [{"match_all": {}}] and q["filter"] == []


def test_bulk_body_is_ndjson():
    ev = parse_lines(["WARN  [x] 2026-11-10T19:21:40,244 A.java:1 - hi"], node="node1")
    lines = bulk_body(ev, inode=42).splitlines()
    assert json.loads(lines[0])["index"]["_index"] == "cassandra-logs-2026.11.10"
    assert json.loads(lines[1])["level"] == "WARN"


def fake_opensearch(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "took": 3,
            "hits": {"total": {"value": 1}, "hits": [
                {"_id": "abc", "_source": {"@timestamp": "2026-11-10T19:21:40.244+00:00",
                                           "level": "WARN", "node": "node1", "message": "hi"}}]},
        })
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://os")


def test_logs_endpoint_passes_filters_to_opensearch():
    captured = []
    app.state.os_client = fake_opensearch(captured)
    with TestClient(app) as c:
        r = c.get("/api/logs", params={"node": "node1", "min_level": "WARN", "q": "pause", "size": 5})
    assert r.status_code == 200
    assert r.json()["events"][0]["id"] == "abc"
    sent = captured[0]
    assert sent["size"] == 5
    assert {"terms": {"node": ["node1"]}} in sent["query"]["bool"]["filter"]
    del app.state.os_client


def test_bad_params_are_rejected():
    app.state.os_client = fake_opensearch([])
    with TestClient(app) as c:
        assert c.get("/api/logs", params={"min_level": "LOUD"}).status_code == 422
        assert c.get("/api/logs", params={"size": 5000}).status_code == 422
        r = c.get("/api/logs", params={"since": "2026-11-10T20:00:00Z", "until": "2026-11-10T19:00:00Z"})
        assert r.status_code == 422
    del app.state.os_client
