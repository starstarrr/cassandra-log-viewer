"""Small FastAPI app to filter Cassandra logs stored in OpenSearch.

    uv run uvicorn api.app:app --reload

GET /                page with a filter form
GET /api/logs        matching log events, newest first
GET /api/summary     counts by node / level / logger and per minute
GET /api/health      OpenSearch cluster health
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .query import build_search, build_summary

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX = "cassandra-logs-*"
STATIC = Path(__file__).parent / "static"

Level = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled HTTP client for the life of the app (tests swap it out).
    if not hasattr(app.state, "os_client"):
        app.state.os_client = httpx.AsyncClient(base_url=OPENSEARCH_URL, timeout=10)
    yield
    await app.state.os_client.aclose()


app = FastAPI(title="Cassandra log viewer", lifespan=lifespan)


def os_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.os_client


Client = Annotated[httpx.AsyncClient, Depends(os_client)]


class Filters:
    """Query parameters shared by /api/logs and /api/summary."""

    def __init__(
        self,
        node: Annotated[list[str] | None, Query(description="node1, node2 or node3; repeatable")] = None,
        level: Annotated[list[Level] | None, Query(description="exact levels, repeatable")] = None,
        min_level: Annotated[Level | None, Query(description="this level and above")] = None,
        since: Annotated[datetime | None, Query(description="ISO 8601, e.g. 2026-11-10T19:00:00Z")] = None,
        until: Annotated[datetime | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=500, description="text search in message/stack trace")] = None,
        logger: Annotated[str | None, Query(description="e.g. Gossiper, ReadCommand")] = None,
    ):
        if since and until and since > until:
            raise HTTPException(422, "since must be before until")
        self.kwargs = dict(nodes=node, levels=level, min_level=min_level,
                           since=since, until=until, q=q, logger=logger)


async def search(client: httpx.AsyncClient, body: dict) -> dict:
    try:
        r = await client.post(f"/{INDEX}/_search", json=body, params={"ignore_unavailable": "true"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"OpenSearch unreachable: {e}") from e
    if r.status_code >= 400:
        raise HTTPException(502, f"OpenSearch error {r.status_code}: {r.text[:300]}")
    return r.json()


@app.get("/api/logs")
async def logs(client: Client, f: Annotated[Filters, Depends()],
               size: Annotated[int, Query(ge=1, le=1000)] = 100):
    res = await search(client, build_search(size=size, **f.kwargs))
    hits = res["hits"]
    return {
        "total": hits["total"]["value"],
        "took_ms": res["took"],
        "events": [h["_source"] | {"id": h["_id"]} for h in hits["hits"]],
    }


@app.get("/api/summary")
async def summary(client: Client, f: Annotated[Filters, Depends()],
                  interval: Annotated[str, Query(pattern=r"^\d+[smhd]$")] = "1m"):
    res = await search(client, build_summary(interval=interval, **f.kwargs))
    aggs = res["aggregations"]

    def buckets(name: str) -> dict:
        return {b["key"]: b["doc_count"] for b in aggs[name]["buckets"]}

    return {
        "total": res["hits"]["total"]["value"],
        "by_node": buckets("by_node"),
        "by_level": buckets("by_level"),
        "top_loggers": buckets("top_loggers"),
        "over_time": [
            {"time": b["key_as_string"], "count": b["doc_count"],
             "by_level": {x["key"]: x["doc_count"] for x in b["by_level"]["buckets"]}}
            for b in aggs["over_time"]["buckets"]
        ],
    }


@app.get("/api/health")
async def health(client: Client):
    try:
        r = await client.get("/_cluster/health")
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"OpenSearch unreachable: {e}") from e


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")
