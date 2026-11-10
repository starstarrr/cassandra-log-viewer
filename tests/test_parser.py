from datetime import datetime, timezone
from pathlib import Path

from collector.parser import parse_header, parse_lines

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_every_real_line():
    # tests/fixtures/real_lines.log was copied from this lab cluster's logs.
    lines = (FIXTURES / "real_lines.log").read_text().splitlines()
    events = parse_lines(lines, node="node1", log_file="system.log")
    assert len(events) == len(lines)
    assert {e.level for e in events} == {"INFO", "WARN", "DEBUG"}


def test_header_fields():
    ev = parse_header(
        "WARN  [GossipTasks:1] 2026-11-10T19:21:40,244 FailureDetector.java:347 - "
        "Not marking nodes down due to local pause of 67206958991ns > 5000000000ns"
    )
    assert ev.level == "WARN"
    assert ev.thread == "GossipTasks:1"
    assert ev.timestamp == datetime(2026, 11, 10, 19, 21, 40, 244000, tzinfo=timezone.utc)
    assert ev.source_file == "FailureDetector.java"
    assert ev.source_line == 347
    assert ev.logger == "FailureDetector"
    assert ev.message.startswith("Not marking nodes down")
    doc = ev.to_doc()
    assert doc["@timestamp"] == "2026-11-10T19:21:40.244+00:00"
    assert doc["thread_pool"] == "GossipTasks"


def test_cassandra4_space_timestamp_also_parses():
    ev = parse_header("INFO  [main] 2024-01-02 03:04:05,678 StorageService.java:123 - Starting")
    assert ev.timestamp == datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)


def test_thread_name_with_brackets_and_unknown_line_number():
    ev = parse_header("ERROR [Pool [x]-2] 2026-11-10T19:00:00,000 Foo.java:? - boom")
    assert ev.thread == "Pool [x]-2"
    assert ev.source_line is None
    assert ev.message == "boom"


def test_non_header_lines_are_not_events():
    assert parse_header("java.lang.RuntimeException: x") is None
    assert parse_header("\tat org.apache.cassandra.Foo.bar(Foo.java:1)") is None
    assert parse_header("") is None


def test_stack_trace_lines_attach_to_previous_event():
    # Hand-written in Cassandra's format to cover multi-line events.
    lines = [
        "ERROR [Native-Transport-Requests-1] 2026-11-10T19:30:00,001 ErrorMessage.java:457 - Unexpected exception during request",
        "java.lang.IllegalStateException: example",
        "\tat org.apache.cassandra.transport.Message$Dispatcher.run(Message.java:1)",
        "\tat java.base/java.lang.Thread.run(Thread.java:840)",
        "INFO  [main] 2026-11-10T19:30:01,000 Foo.java:1 - next",
    ]
    events = parse_lines(lines)
    assert len(events) == 2
    doc = events[0].to_doc()
    assert doc["exception"] == "java.lang.IllegalStateException: example"
    assert doc["stack_trace"].count("\n") == 2
    assert "stack_trace" not in events[1].to_doc()
