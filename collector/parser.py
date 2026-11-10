"""Parse Apache Cassandra log lines.

Cassandra's default logback pattern (conf/logback.xml) for system.log and
debug.log is:

    %-5level [%thread] %date{"yyyy-MM-dd'T'HH:mm:ss,SSS", UTC} %F:%L - %msg%n

(Cassandra 5.0; 4.x used %date{ISO8601}, which has a space instead of the T
and is in the JVM's local time zone). For example:

    INFO  [main] 2026-11-10T19:20:01,234 StorageService.java:1234 - Node /172.18.0.2 state jump to NORMAL

A stack trace is printed on the lines after the header line, so any line that
does not match the header belongs to the event above it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR")

# The thread name is matched lazily up to the "] <timestamp>" that follows it,
# so thread names that contain "]" still parse.
HEADER_RE = re.compile(
    r"^(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+"
    r"\[(?P<thread>.*?)\]\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(?P<source>[^\s:]+):(?P<line>\d+|\?)\s+-\s?"
    r"(?P<message>.*)$"
)


@dataclass
class LogEvent:
    timestamp: datetime
    level: str
    thread: str
    source_file: str
    source_line: int | None
    message: str
    node: str = ""
    log_file: str = ""
    offset: int = 0
    stack: list[str] = field(default_factory=list)

    @property
    def logger(self) -> str:
        """Class name taken from the source file, e.g. Gossiper.java -> Gossiper."""
        return self.source_file.rsplit(".", 1)[0]

    def to_doc(self) -> dict:
        doc = {
            "@timestamp": self.timestamp.isoformat(timespec="milliseconds"),
            "level": self.level,
            "node": self.node,
            "thread": self.thread,
            # Pool name without the worker number: "CompactionExecutor:3" -> "CompactionExecutor"
            "thread_pool": re.sub(r"[:\-]\d+$", "", self.thread),
            "logger": self.logger,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "message": self.message,
            "log_file": self.log_file,
            "offset": self.offset,
        }
        if self.stack:
            doc["stack_trace"] = "\n".join(self.stack)
            # First exception line, e.g. "java.io.IOException: Broken pipe"
            doc["exception"] = next(
                (s.strip() for s in self.stack if not s.lstrip().startswith("at ")),
                self.stack[0].strip(),
            )
        return doc


def parse_header(line: str, tz: timezone = timezone.utc) -> LogEvent | None:
    """Return a LogEvent if `line` starts a new log entry, else None.

    The timestamp has no offset in the line. Cassandra 5.0 writes it in UTC;
    4.x wrote local time, which is also UTC in the official Docker image.
    """
    m = HEADER_RE.match(line.rstrip("\r\n"))
    if not m:
        return None
    ts = datetime.strptime(m["ts"].replace("T", " "), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=tz)
    line_no = m["line"]
    return LogEvent(
        timestamp=ts,
        level=m["level"],
        thread=m["thread"],
        source_file=m["source"],
        source_line=int(line_no) if line_no.isdigit() else None,
        message=m["message"],
    )


def parse_lines(lines: list[str], node: str = "", log_file: str = "") -> list[LogEvent]:
    """Group raw lines into events (header line + any continuation lines).

    Handy for tests and for one-off parsing of a whole file. The tailer uses
    the same parse_header() but tracks byte offsets itself.
    """
    events: list[LogEvent] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        ev = parse_header(line)
        if ev:
            ev.node, ev.log_file = node, log_file
            events.append(ev)
        elif events and line.strip():
            events[-1].stack.append(line)
    return events
