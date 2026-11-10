import os

from collector.tailer import FileTailer, StateStore, doc_id

L1 = "INFO  [main] 2026-11-10T19:20:00,000 A.java:1 - first\n"
L2 = "WARN  [main] 2026-11-10T19:20:01,000 B.java:2 - second\n"
ERR = "ERROR [main] 2026-11-10T19:20:02,000 C.java:3 - third\n"
TRACE = ["java.io.IOException: Broken pipe\n", "\tat x.Y.z(Y.java:9)\n"]


def drain(t):
    """Read + commit until nothing new comes out (like the collector loop)."""
    out, quiet = [], 0
    while quiet < 2:  # two empty polls in a row = caught up
        b = t.read()
        t.commit(b)
        out += b.events
        quiet = 0 if b.events else quiet + 1
    return out


def test_holds_last_event_until_file_is_quiet(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + L2)
    st = StateStore(tmp_path / "s.json")
    t = FileTailer(p, "node1", st.get(str(p)))

    b = t.read()
    assert [e.message for e in b.events] == ["first"]  # "second" held back
    t.commit(b)
    assert t.state.offset == len(L1)

    b = t.read()  # file did not grow -> flush
    assert [e.message for e in b.events] == ["second"]
    t.commit(b)
    assert t.state.offset == len(L1 + L2)


def test_stack_trace_written_later_joins_its_event(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + ERR)
    t = FileTailer(p, "n", StateStore(tmp_path / "s.json").get("k"))
    t.commit(t.read())  # emits L1, holds ERR
    with open(p, "a") as f:
        f.writelines(TRACE)
    b = t.read()  # file grew: ERR still held
    assert b.events == []
    t.commit(b)
    b = t.read()  # quiet: ERR comes out with its trace
    assert b.events[0].level == "ERROR"
    assert b.events[0].stack[0] == "java.io.IOException: Broken pipe"


def test_partial_line_is_not_read(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + L2[:20])  # writer is mid-line
    t = FileTailer(p, "n", StateStore(tmp_path / "s.json").get("k"))
    assert [e.message for e in drain(t)] == ["first"]
    with open(p, "a") as f:
        f.write(L2[20:])
    assert [e.message for e in drain(t)] == ["second"]


def test_restart_resumes_from_saved_offset(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + L2)
    sp = tmp_path / "s.json"
    st = StateStore(sp)
    t = FileTailer(p, "n", st.get(str(p)))
    drain(t)
    st.save()

    with open(p, "a") as f:
        f.write(ERR)
    st2 = StateStore(sp)  # "restarted" collector
    t2 = FileTailer(p, "n", st2.get(str(p)))
    assert [e.message for e in drain(t2)] == ["third"]


def test_uncommitted_batch_is_read_again_with_same_ids(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + L2 + ERR)
    t = FileTailer(p, "n", StateStore(tmp_path / "s.json").get("k"))
    first = t.read()  # pretend the bulk request failed: no commit
    again = t.read()  # same offset, so the same lines come back (plus the flushed last one)
    ino = os.stat(p).st_ino
    ids = [doc_id(e, ino) for e in again.events]
    assert [doc_id(e, ino) for e in first.events] == ids[: len(first.events)]
    assert len(set(ids)) == 3


def test_rotation_starts_new_file_from_zero(tmp_path):
    p = tmp_path / "system.log"
    p.write_text(L1 + L2)
    t = FileTailer(p, "n", StateStore(tmp_path / "s.json").get("k"))
    drain(t)
    os.rename(p, tmp_path / "system.log.1")  # logback rolls the file
    p.write_text(ERR)  # new inode, smaller than the old offset
    assert [e.message for e in drain(t)] == ["third"]


def test_keep_levels_filter(tmp_path):
    p = tmp_path / "debug.log"
    p.write_text(L1 + "DEBUG [main] 2026-11-10T19:20:00,500 D.java:4 - dbg\n" + L2)
    t = FileTailer(p, "n", StateStore(tmp_path / "s.json").get("k"), keep_levels={"DEBUG"})
    assert [e.message for e in drain(t)] == ["dbg"]
