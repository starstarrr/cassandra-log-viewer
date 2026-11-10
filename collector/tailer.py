"""Restartable tailing of a log file.

Delivery is at-least-once: the byte offset is only committed after OpenSearch
accepted the batch. If the collector dies in between, the same lines are read
again on restart, and because every document id is derived from
(node, file, inode, offset) the re-sent documents overwrite themselves instead
of creating duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .parser import LogEvent, parse_header

MAX_READ = 1 << 20  # read at most 1 MiB per poll


@dataclass
class FileState:
    inode: int = 0
    offset: int = 0


@dataclass
class Batch:
    events: list[LogEvent]
    inode: int
    next_offset: int


def doc_id(ev: LogEvent, inode: int) -> str:
    key = f"{ev.node}|{ev.log_file}|{inode}|{ev.offset}"
    return hashlib.sha1(key.encode()).hexdigest()


class FileTailer:
    def __init__(self, path: Path, node: str, state: FileState, keep_levels: set[str] | None = None):
        self.path = Path(path)
        self.node = node
        self.state = state
        self.keep_levels = keep_levels  # None = keep every level
        self._last_size: int | None = None  # file size seen at the previous poll

    def read(self) -> Batch:
        """Read new complete events since the committed offset.

        The last event in the file is held back until the file stops growing for
        one poll, because a stack trace for it may still be on its way.
        """
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return Batch([], self.state.inode, self.state.offset)

        start = self.state.offset
        if st.st_ino != self.state.inode or st.st_size < start:
            # New file, log rotation (new inode) or truncation: start over.
            start = 0

        idle = self._last_size == st.st_size
        self._last_size = st.st_size

        with open(self.path, "rb") as f:
            f.seek(start)
            data = f.read(MAX_READ)

        cut = data.rfind(b"\n")
        if cut < 0:
            return Batch([], st.st_ino, start)  # no complete line yet
        data = data[: cut + 1]
        hit_read_limit = start + len(data) < st.st_size

        events: list[LogEvent] = []
        pos = start
        for raw in data.splitlines(keepends=True):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            ev = parse_header(line)
            if ev:
                ev.node, ev.log_file, ev.offset = self.node, self.path.name, pos
                events.append(ev)
            elif events and line.strip():
                events[-1].stack.append(line)
            pos += len(raw)

        end = start + len(data)
        # Hold the last event back (commit only up to where it starts) while the
        # file is still growing or this chunk stopped before the end of the file.
        # Exception: a single event bigger than MAX_READ is emitted as is,
        # otherwise the tailer would never move forward.
        hold = bool(events) and (not idle or hit_read_limit)
        if hold and hit_read_limit and events[-1].offset == start:
            hold = False
        if hold:
            end = events[-1].offset
            events = events[:-1]

        if self.keep_levels is not None:
            events = [e for e in events if e.level in self.keep_levels]
        return Batch(events, st.st_ino, end)

    def commit(self, batch: Batch) -> None:
        self.state.inode = batch.inode
        self.state.offset = batch.next_offset


class StateStore:
    """Offsets per file, saved as JSON. Written atomically (tmp file + rename)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.files: dict[str, FileState] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.files = {k: FileState(**v) for k, v in raw.items()}

    def get(self, key: str) -> FileState:
        return self.files.setdefault(key, FileState())

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: vars(v) for k, v in self.files.items()}, indent=2))
        os.replace(tmp, self.path)
