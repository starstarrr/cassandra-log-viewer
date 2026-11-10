"""Turn filter parameters into OpenSearch query DSL. Pure functions, easy to test."""

from __future__ import annotations

from datetime import datetime

LEVEL_ORDER = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"]


def levels_at_or_above(min_level: str) -> list[str]:
    i = LEVEL_ORDER.index(min_level.upper())
    return LEVEL_ORDER[i:]


def build_filters(
    nodes: list[str] | None = None,
    levels: list[str] | None = None,
    min_level: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    logger: str | None = None,
) -> dict:
    must: list[dict] = []
    filters: list[dict] = []
    if nodes:
        filters.append({"terms": {"node": nodes}})
    if levels:
        filters.append({"terms": {"level": [lv.upper() for lv in levels]}})
    elif min_level:
        filters.append({"terms": {"level": levels_at_or_above(min_level)}})
    if logger:
        filters.append({"term": {"logger": logger}})
    if since or until:
        rng: dict = {}
        if since:
            rng["gte"] = since.isoformat()
        if until:
            rng["lte"] = until.isoformat()
        filters.append({"range": {"@timestamp": rng}})
    if q:
        # simple_query_string never throws on user syntax errors,
        # supports "quoted phrases", -exclude and prefix*.
        must.append({
            "simple_query_string": {
                "query": q,
                "fields": ["message", "stack_trace", "exception"],
                "default_operator": "and",
            }
        })
    # filter clauses are cached and skip scoring; only the text search scores.
    return {"bool": {"must": must or [{"match_all": {}}], "filter": filters}}


def build_search(size: int = 100, **filters) -> dict:
    return {
        "size": size,
        "query": build_filters(**filters),
        "sort": [{"@timestamp": "desc"}, {"offset": "desc"}],
        "track_total_hits": True,
    }


def build_summary(interval: str = "1m", **filters) -> dict:
    return {
        "size": 0,
        "query": build_filters(**filters),
        "aggs": {
            "by_node": {"terms": {"field": "node", "size": 10}},
            "by_level": {"terms": {"field": "level", "size": 10}},
            "top_loggers": {"terms": {"field": "logger", "size": 10}},
            "over_time": {
                "date_histogram": {"field": "@timestamp", "fixed_interval": interval, "min_doc_count": 1},
                "aggs": {"by_level": {"terms": {"field": "level", "size": 5}}},
            },
        },
    }
