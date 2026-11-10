"""Send events to OpenSearch with the _bulk API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from .parser import LogEvent
from .tailer import doc_id

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "opensearch" / "index-template.json"
INDEX_PREFIX = "cassandra-logs-"


class BulkError(RuntimeError):
    pass


def index_name(ev: LogEvent) -> str:
    # One index per day: old days can be deleted cheaply (drop the index).
    return INDEX_PREFIX + ev.timestamp.strftime("%Y.%m.%d")


def bulk_body(events: list[LogEvent], inode: int) -> str:
    """NDJSON body: an action line then a source line per document."""
    lines = []
    for ev in events:
        lines.append(json.dumps({"index": {"_index": index_name(ev), "_id": doc_id(ev, inode)}}))
        lines.append(json.dumps(ev.to_doc()))
    return "\n".join(lines) + "\n"


class OpenSearchIndexer:
    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=base_url, timeout=30)

    def ensure_template(self) -> None:
        body = json.loads(TEMPLATE_PATH.read_text())
        r = self.client.put("/_index_template/cassandra-logs", json=body)
        r.raise_for_status()

    def bulk(self, events: list[LogEvent], inode: int) -> int:
        if not events:
            return 0
        r = self.client.post(
            "/_bulk",
            content=bulk_body(events, inode),
            headers={"Content-Type": "application/x-ndjson"},
        )
        r.raise_for_status()
        result = r.json()
        if result.get("errors"):
            failed = [i["index"] for i in result["items"] if i["index"].get("error")]
            raise BulkError(f"{len(failed)} of {len(events)} docs failed, first: {failed[0]['error']}")
        return len(events)
